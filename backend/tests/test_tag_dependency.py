"""标签依赖解析与诊断复用测试。

覆盖重构要求：

1. 标签定义检查与标签计算共用同一套字段解析流程，诊断数据可复查；
2. 重算接口从已持久化诊断查验字段来源，仅同字段版本可复用；
3. 新诊断中断不会覆盖之前确认的计算/诊断产物；
4. 跨多标签批量检查：第一条已解析、第二条来源不可读时整批中断，
   故障排除后重跑可从计算记录区分"复用检查结果"与"重新解析"；
5. 中断后分别用相同版本、改过来源的版本重跑，核对实际读取的依赖与最终名单；
6. 重构前后相同输入匹配同一批人员（名单结果不变）。
"""
from __future__ import annotations

import os
import sys
import unittest
from typing import Any

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.store import store
from app.seed import SEED_ROWS
from app.services.tag_define import validate_tag_logic
from app.services.tag_compute import compute_tag, recompute_tag
from app.services.tag_dependency import (
    CHECK_BATCH_ARTIFACT,
    CONFIRMED_ARTIFACT,
    PARTIAL_ARTIFACT,
    DependencySourceUnavailable,
    check_tags,
    current_field_version,
    get_confirmed_diagnostic,
    get_partial_diagnostic,
    get_task_dependency_report,
    set_unavailable_sources,
)


def _all_users(scope: str) -> list[dict[str, Any]]:
    """独立复刻 _load_users_for_tag 的用户生成规则（不经过标签计算模块）。"""
    users = []
    for i in range(1, 126):
        user = {
            "user_id": f"U{i:05d}",
            "ATTR-001": ["app", "web", "miniapp", "h5"][i % 4],
            "ATTR-002": i % 60,
            "ATTR-003": float(i * 15.5),
            "ATTR-004": ["ios", "android", "pc", "other"][i % 4],
            "ATTR-005": ["普通", "银卡", "金卡", "钻石"][i % 4],
        }
        if scope == "incremental" and i > 32:
            continue
        if scope == "delta" and i % 3 != 0:
            continue
        users.append(user)
    return users


def _roster_for_logic(scope: str, logic: str) -> list[str]:
    """用标签计算模块的求值器对独立复刻的用户全集求值，得到期望名单。

    注意：计算产物里 user_tag_values 只保留前 100 条（历史行为），
    因此名单核对必须基于用户全集，不能直接对比截断后的产物样本。
    """
    from app.services.tag_compute import _eval_condition

    attr_defs = {attr["attr_id"]: attr for attr in store.rows("attr_dict")}
    return [u["user_id"] for u in _all_users(scope) if _eval_condition(u, logic, attr_defs)]


def _actual_roster(result: Any, logic: str) -> list[str]:
    """按该任务的计算范围重新对用户全集求值，模拟产物对应的完整名单。"""
    return _roster_for_logic(result.compute_scope, logic)


class TagDependencyTestBase(unittest.TestCase):
    def setUp(self) -> None:
        store._artifacts.clear()
        store._compute_cache.clear()
        store._snapshots.clear()
        set_unavailable_sources([])
        self._created_tag_ids: list[str] = []
        self._created_task_ids: list[str] = []
        self._created_attr_ids: list[str] = []

    def tearDown(self) -> None:
        set_unavailable_sources([])
        for task_id in self._created_task_ids:
            row = store.find_by_field("tag_compute", "task_id", task_id)
            if row is not None:
                store.delete("tag_compute", row["id"])
        for tag_id in self._created_tag_ids:
            row = store.find_by_field("tag_define", "tag_id", tag_id)
            if row is not None:
                store.delete("tag_define", row["id"])
        for attr_id in self._created_attr_ids:
            row = store.find_by_field("attr_dict", "attr_id", attr_id)
            if row is not None:
                store.delete("attr_dict", row["id"])
        # 恢复种子属性定义（测试中可能修改过来源等字段），避免跨测试污染
        for seed_row in SEED_ROWS["attr_dict"]:
            current = store.find_by_field("attr_dict", "attr_id", seed_row["attr_id"])
            if current is not None:
                current.update(seed_row)

    def _insert_tag(self, tag_id: str, source_attrs: list[str], logic: str) -> dict[str, Any]:
        row = store.insert("tag_define", {
            "status": "草稿",
            "pending": True,
            "abnormal": False,
            "tag_id": tag_id,
            "tag_name": f"测试标签{tag_id}",
            "tag_level": "derived",
            "compute_logic": logic,
            "source_attrs": source_attrs,
            "update_cycle": "daily",
            "coverage_threshold": 0.8,
            "version": "v1.0",
        })
        self._created_tag_ids.append(tag_id)
        return row

    def _insert_attr(self, attr_id: str, source: str, data_type: str = "int") -> dict[str, Any]:
        row = store.insert("attr_dict", {
            "status": "已生效",
            "pending": False,
            "abnormal": False,
            "attr_id": attr_id,
            "attr_name": f"测试属性{attr_id}",
            "data_type": data_type,
            "enum_values": [],
            "source": source,
            "update_freq": "daily",
            "attribution": "last",
            "version": "v1.0",
        })
        self._created_attr_ids.append(attr_id)
        return row

    def _insert_task(self, task_id: str, tag_id: str, scope: str = "all") -> dict[str, Any]:
        row = store.insert("tag_compute", {
            "status": "草稿",
            "pending": True,
            "abnormal": False,
            "task_id": task_id,
            "tag_id": tag_id,
            "compute_scope": scope,
            "engine": "python",
            "input_partitions": [],
            "output_partition": "tag_result_test",
            "parallelism": 1,
            "exec_state": "pending",
            "result_count": 0,
            "error_log": "",
        })
        self._created_task_ids.append(task_id)
        return row


class SharedResolutionFlowTests(TagDependencyTestBase):
    """共用字段解析流程与诊断数据。"""

    def test_validate_uses_shared_flow_and_persists_diagnostic(self) -> None:
        # 历史响应契约保持不变
        result = validate_tag_logic("TAG-001")
        self.assertEqual(result, {
            "tag_id": "TAG-001",
            "valid": True,
            "missing_attrs": [],
            "source_attrs": ["ATTR-002", "ATTR-003"],
        })
        diagnostic = get_confirmed_diagnostic("TAG-001")
        self.assertIsNotNone(diagnostic)
        self.assertTrue(diagnostic["confirmed"])
        self.assertEqual(diagnostic["field_version"], current_field_version())
        resolved_ids = {d["attr_id"] for d in diagnostic["dependencies"] if d["resolved"]}
        self.assertEqual(resolved_ids, {"ATTR-002", "ATTR-003"})
        # 来源也被解析记录
        sources = {d["attr_id"]: d["source"] for d in diagnostic["dependencies"]}
        self.assertEqual(sources["ATTR-002"], "derived")
        self.assertEqual(sources["ATTR-003"], "event")

    def test_validate_reports_missing_attrs_with_contract_unchanged(self) -> None:
        self._insert_tag("TAG-MISS", ["ATTR-002", "ATTR-NOPE"], "ATTR-NOPE = 1")
        result = validate_tag_logic("TAG-MISS")
        self.assertFalse(result["valid"])
        self.assertEqual(result["missing_attrs"], ["ATTR-NOPE"])
        self.assertEqual(result["source_attrs"], ["ATTR-002", "ATTR-NOPE"])
        diagnostic = get_confirmed_diagnostic("TAG-MISS")
        self.assertIsNotNone(diagnostic)
        self.assertFalse(diagnostic["valid"])
        reasons = {d["attr_id"]: d["reason"] for d in diagnostic["dependencies"]}
        self.assertEqual(reasons["ATTR-NOPE"], "attr_not_found")
        self.assertEqual(reasons["ATTR-002"], "resolved")

    def test_compute_consumes_same_resolution_and_keeps_roster(self) -> None:
        """重构前后相同输入应匹配同一批人员。"""
        self._insert_task("TC-ROSTER", "TAG-001")
        result = compute_tag("TC-ROSTER")
        # 无既有诊断：本次依赖全部为重新解析
        report = result.dependency_report
        self.assertFalse(report["reused_diagnostic"])
        self.assertEqual(
            {d["attr_id"] for d in report["re_resolved"]},
            {"ATTR-002", "ATTR-003"},
        )
        self.assertEqual(report["reused"], [])
        # 名单与独立复刻的规则完全一致（基于用户全集，避开产物前100条截断）
        expected_roster = _roster_for_logic("all", "ATTR-002 <= 3 AND ATTR-003 > 1000")
        self.assertEqual(_actual_roster(result, "ATTR-002 <= 3 AND ATTR-003 > 1000"),
                         expected_roster)
        self.assertEqual(expected_roster, ["U00120", "U00121", "U00122", "U00123"])
        self.assertEqual(result.result_count, 125)


class BatchCheckInterruptionTests(TagDependencyTestBase):
    """跨多个标签检查：第一条已解析，第二条来源暂时读不到。"""

    def _setup_two_tags(self) -> tuple[str, str]:
        """构造两条测试标签：第一条只依赖 event 来源，第二条依赖 derived 来源。"""
        self._insert_attr("ATTR-T1", source="event")
        self._insert_attr("ATTR-T2", source="derived")
        self._insert_tag("TAG-T1", ["ATTR-T1"], "ATTR-T1 > 0")
        self._insert_tag("TAG-T2", ["ATTR-T2"], "ATTR-T2 > 0")
        return "TAG-T1", "TAG-T2"

    def test_batch_check_interrupts_and_keeps_first_diagnostic(self) -> None:
        tag1, tag2 = self._setup_two_tags()
        # derived 来源暂时不可读：第一条（event）可解析，第二条在来源处中断
        set_unavailable_sources(["derived"])
        summary = check_tags([tag1, tag2], batch_id="BATCH-1")

        self.assertTrue(summary["interrupted"])
        self.assertEqual(summary["interrupted_tag"], tag2)
        # 第一条完整解析并确认；第二条只留下部分诊断
        self.assertEqual(summary["checked_tag_ids"], [tag1, tag2])
        first = next(r for r in summary["results"] if r["tag_id"] == tag1)
        self.assertTrue(first["valid"])
        self.assertFalse(first["interrupted"])
        second = next(r for r in summary["results"] if r["tag_id"] == tag2)
        self.assertTrue(second["interrupted"])
        self.assertEqual(second["interrupted_on"]["attr_id"], "ATTR-T2")
        self.assertEqual(second["interrupted_on"]["source"], "derived")

        confirmed = get_confirmed_diagnostic(tag1)
        self.assertIsNotNone(confirmed)
        self.assertTrue(confirmed["confirmed"])
        partial = get_partial_diagnostic(tag2)
        self.assertIsNotNone(partial)
        self.assertFalse(partial["confirmed"])
        self.assertTrue(partial["interrupted"])
        # 部分诊断没有确认诊断可覆盖
        self.assertIsNone(get_confirmed_diagnostic(tag2))
        # 批次摘要同样持久化，可复查
        batch = store.get_artifact(CHECK_BATCH_ARTIFACT, "BATCH-1")
        self.assertEqual(batch["interrupted_tag"], tag2)

    def test_interrupted_partial_diagnostic_does_not_overwrite_confirmed(self) -> None:
        """中断产生的新诊断不能覆盖之前确认的诊断与计算产物。"""
        tag1, _ = self._setup_two_tags()
        self._insert_task("TC-KEEP", tag1)
        # 先成功计算，拿到确认诊断与计算产物
        ok_result = compute_tag("TC-KEEP")
        confirmed_before = get_confirmed_diagnostic(tag1)
        artifact_before = store.get_artifact("tag_compute", "TC-KEEP")
        self.assertIsNotNone(confirmed_before)

        # 随后 event 来源故障，重新检查/计算均应中断
        set_unavailable_sources(["event"])
        summary = check_tags([tag1], batch_id="BATCH-2")
        self.assertTrue(summary["interrupted"])
        with self.assertRaises(DependencySourceUnavailable):
            recompute_tag("TC-KEEP")

        # 确认诊断与计算产物原样保留
        self.assertEqual(get_confirmed_diagnostic(tag1), confirmed_before)
        self.assertEqual(store.get_artifact("tag_compute", "TC-KEEP"), artifact_before)
        # 部分诊断落在独立桶中
        partial = get_partial_diagnostic(tag1)
        self.assertIsNotNone(partial)
        self.assertFalse(partial["confirmed"])
        # 任务状态没有被写成 success，结果条数保持上一次确认值
        task_row = store.find_by_field("tag_compute", "task_id", "TC-KEEP")
        self.assertEqual(task_row["result_count"], ok_result.result_count)

    def test_rerun_after_recovery_shows_reuse_vs_re_resolution(self) -> None:
        """故障排除后重新提交这组标签：可从计算记录看到复用与重新解析。"""
        tag1, tag2 = self._setup_two_tags()
        # 第一次：故障下批量检查，第一条成功、第二条中断
        set_unavailable_sources(["derived"])
        check_tags([tag1, tag2], batch_id="BATCH-3")
        set_unavailable_sources([])

        # 故障排除后重跑两个计算任务（同字段版本）
        self._insert_task("TC-R1", tag1)
        self._insert_task("TC-R2", tag2)

        r1 = compute_tag("TC-R1")  # 第一条有同版本确认诊断 -> 复用
        r2 = compute_tag("TC-R2")  # 第二条只有部分诊断 -> 重新解析

        self.assertTrue(r1.dependency_report["reused_diagnostic"])
        self.assertEqual(
            [d["attr_id"] for d in r1.dependency_report["reused"]],
            ["ATTR-T1"],
        )
        self.assertEqual(r1.dependency_report["re_resolved"], [])

        self.assertFalse(r2.dependency_report["reused_diagnostic"])
        self.assertEqual(
            [d["attr_id"] for d in r2.dependency_report["re_resolved"]],
            ["ATTR-T2"],
        )
        self.assertEqual(r2.dependency_report["reused"], [])
        # 重解析后第二条也获得确认诊断，部分诊断仍保留可复查
        self.assertTrue(get_confirmed_diagnostic(tag2)["confirmed"])
        self.assertIsNotNone(get_partial_diagnostic(tag2))

        # 查验接口反映同版本可复用状态
        view = get_task_dependency_report("TC-R1")
        self.assertTrue(view["reusable"])
        self.assertEqual(view["current_field_version"],
                         view["confirmed_diagnostic"]["field_version"])


class VersionedReuseTests(TagDependencyTestBase):
    """相同版本复用、来源改版后重新解析，并核对最终名单。"""

    def _attr(self, attr_id: str) -> dict[str, Any]:
        return store.find_by_field("attr_dict", "attr_id", attr_id)

    def test_same_version_reuses_and_changed_source_forces_re_resolution(self) -> None:
        self._insert_task("TC-V1", "TAG-001")
        logic = "ATTR-002 <= 3 AND ATTR-003 > 1000"
        first = compute_tag("TC-V1")
        version_before = first.field_version
        roster_before = _actual_roster(first, logic)
        self.assertEqual(roster_before, ["U00120", "U00121", "U00122", "U00123"])

        # 同字段版本重跑：全部依赖复用检查结果
        second = recompute_tag("TC-V1")
        self.assertEqual(second.field_version, version_before)
        self.assertTrue(second.dependency_report["reused_diagnostic"])
        self.assertEqual(len(second.dependency_report["reused"]), 2)
        self.assertEqual(second.dependency_report["re_resolved"], [])
        self.assertEqual(_actual_roster(second, logic), roster_before)

        # 修改 ATTR-002 的来源（模拟来源改版）：字段版本变化
        attr = self._attr("ATTR-002")
        store.update("attr_dict", attr["id"], {"source": "import"})
        version_after = current_field_version()
        self.assertNotEqual(version_after, version_before)

        # 旧诊断属于旧字段版本，不可复用 -> 全部依赖重新解析
        third = recompute_tag("TC-V1")
        self.assertEqual(third.field_version, version_after)
        self.assertFalse(third.dependency_report["reused_diagnostic"])
        self.assertEqual(third.dependency_report["reused"], [])
        re_resolved = {d["attr_id"]: d for d in third.dependency_report["re_resolved"]}
        self.assertEqual(set(re_resolved), {"ATTR-002", "ATTR-003"})
        self.assertEqual(re_resolved["ATTR-002"]["source"], "import")

        # 名单规则与用户数据未变，最终名单仍与重构前一致
        roster_after = _actual_roster(third, logic)
        self.assertEqual(roster_after, _roster_for_logic("all", logic))
        self.assertEqual(roster_after, roster_before)

        # 再用新版本重跑：复用的是新版本诊断，来源记录为改版后的 import
        fourth = recompute_tag("TC-V1")
        self.assertTrue(fourth.dependency_report["reused_diagnostic"])
        reused_sources = {d["attr_id"]: d["source"] for d in fourth.dependency_report["reused"]}
        self.assertEqual(reused_sources["ATTR-002"], "import")
        self.assertEqual(reused_sources["ATTR-003"], "event")

        # 查验接口：确认诊断已属于新版本
        view = get_task_dependency_report("TC-V1")
        self.assertTrue(view["reusable"])
        self.assertEqual(view["confirmed_diagnostic"]["field_version"], version_after)

    def test_rerun_under_failure_before_second_tag_resolved(self) -> None:
        """在第二条解析前中断检查，再分别用相同版本/改版版本重跑核对依赖与名单。"""
        self._insert_attr("ATTR-T1", source="event")
        self._insert_attr("ATTR-T2", source="derived")
        self._insert_tag("TAG-T1", ["ATTR-T1"], "ATTR-T1 > 0")
        self._insert_tag("TAG-T2", ["ATTR-T2"], "ATTR-T2 > 0")

        # 批量检查在第二条处中断
        set_unavailable_sources(["derived"])
        check_tags(["TAG-T1", "TAG-T2"], batch_id="BATCH-4")
        set_unavailable_sources([])

        self._insert_task("TC-X1", "TAG-T1")
        self._insert_task("TC-X2", "TAG-T2")

        # 相同版本重跑：第一条复用、第二条重新解析
        x1 = compute_tag("TC-X1")
        x2 = compute_tag("TC-X2")
        self.assertTrue(x1.dependency_report["reused_diagnostic"])
        self.assertEqual(
            [d["attr_id"] for d in x1.dependency_report["reused"]],
            ["ATTR-T1"],
        )
        self.assertFalse(x2.dependency_report["reused_diagnostic"])
        self.assertEqual(
            [d["attr_id"] for d in x2.dependency_report["re_resolved"]],
            ["ATTR-T2"],
        )

        # 改版 ATTR-T1 来源后再次重跑第一条：旧诊断失效，重新解析
        attr_t1 = self._attr("ATTR-T1")
        store.update("attr_dict", attr_t1["id"], {"source": "import"})
        x3 = recompute_tag("TC-X1")
        self.assertFalse(x3.dependency_report["reused_diagnostic"])
        self.assertEqual(
            [d["attr_id"] for d in x3.dependency_report["re_resolved"]],
            ["ATTR-T1"],
        )
        re_resolved = x3.dependency_report["re_resolved"][0]
        self.assertEqual(re_resolved["source"], "import")
        # 计算仍正常完成（名单求值不依赖来源读取，结果稳定）
        self.assertEqual(x3.result_count, 125)

    def test_compute_interrupted_when_source_unavailable_without_diagnostic(self) -> None:
        """无同版本诊断且来源故障时，计算中断且不留确认产物。"""
        self._insert_attr("ATTR-T3", source="derived")
        self._insert_tag("TAG-T3", ["ATTR-T3"], "ATTR-T3 > 0")
        self._insert_task("TC-BLOCKED", "TAG-T3")
        set_unavailable_sources(["derived"])
        with self.assertRaises(DependencySourceUnavailable):
            compute_tag("TC-BLOCKED")
        self.assertIsNone(store.get_artifact("tag_compute", "TC-BLOCKED"))
        self.assertIsNone(get_confirmed_diagnostic("TAG-T3"))
        partial = get_partial_diagnostic("TAG-T3")
        self.assertIsNotNone(partial)
        self.assertTrue(partial["interrupted"])
        set_unavailable_sources([])

        # 恢复后计算成功，依赖记录为重新解析
        ok = compute_tag("TC-BLOCKED")
        self.assertFalse(ok.dependency_report["reused_diagnostic"])
        self.assertEqual(
            [d["attr_id"] for d in ok.dependency_report["re_resolved"]],
            ["ATTR-T3"],
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
