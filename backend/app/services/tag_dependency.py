"""标签依赖解析：标签定义检查与标签计算共用的字段解析流程。

两处历史上各自读取依赖：

- ``services/tag_define.validate_tag_logic`` 只检查 ``source_attrs`` 里的属性是否存在；
- ``services/tag_compute.compute_tag`` 独立装载整份属性字典用于类型转换。

本模块把"标签依赖了哪些字段、字段来源是否可解析、属于哪个字段版本"的逻辑收敛到
一处，并生成可复查的标签依赖诊断数据（持久化在 store 数据产物中）。

诊断数据分两个桶持久化，互不覆盖：

- ``tag_dependency_diagnostic``：完整跑完并确认的诊断（confirmed），可被重算复用；
- ``tag_dependency_diagnostic_partial``：因字段来源暂时不可读而中断的检查留下的
  部分诊断，仅供排查，**不允许**覆盖已确认的计算/诊断产物，也不允许被复用。

字段版本（field_version）由当前 attr_dict 中全部属性的关键定义快照生成：任一属性的
来源（source）或版本（version）等发生变化，字段版本即变化；重算只复用同字段版本的
诊断记录。
"""
from __future__ import annotations

import copy
import hashlib
import json
import re
from typing import Any

from app.store import store

# ========== 持久化键 ==========
CONFIRMED_ARTIFACT = "tag_dependency_diagnostic"
PARTIAL_ARTIFACT = "tag_dependency_diagnostic_partial"
CHECK_BATCH_ARTIFACT = "tag_dependency_check_batch"

# 比较/逻辑操作符，token 提取时需要剔除
_LOGIC_KEYWORDS = {"AND", "OR", "NOT", "IN"}
# 形如 ATTR-002 / TAG-001 的字段编号
_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9]*-\d+")


class DependencySourceUnavailable(RuntimeError):
    """字段来源暂时不可读（区别于属性未定义），检查/计算应中断而非降级。"""

    def __init__(self, attr_id: str, source: str) -> None:
        super().__init__(f"字段 {attr_id} 的来源 {source} 暂时无法读取")
        self.attr_id = attr_id
        self.source = source


# 故障注入：标记当前暂时读不到的来源（如 event/derived/import）。
# 正常运行时为空；测试和故障演练时通过 set_unavailable_sources 切换。
_unavailable_sources: set[str] = set()


def set_unavailable_sources(sources: list[str] | set[str] | tuple[str, ...]) -> None:
    """设置暂时不可读的字段来源集合（用于故障排除演练/测试）。"""
    _unavailable_sources.clear()
    _unavailable_sources.update(sources)


def get_unavailable_sources() -> list[str]:
    """返回当前暂时不可读的字段来源。"""
    return sorted(_unavailable_sources)


def _reset_unavailable_sources() -> None:
    _unavailable_sources.clear()


# ========== 字段版本 ==========
def current_field_version() -> str:
    """根据当前属性字典快照计算字段版本。

    任一属性的来源、版本、数据类型或枚举定义变化都会产生新版本号，
    从而保证"改过来源的版本"无法复用旧诊断。
    """
    snapshot = []
    for attr in store.rows("attr_dict"):
        snapshot.append({
            "attr_id": attr.get("attr_id"),
            "attr_name": attr.get("attr_name"),
            "data_type": attr.get("data_type"),
            "enum_values": list(attr.get("enum_values", [])),
            "source": attr.get("source"),
            "update_freq": attr.get("update_freq"),
            "attribution": attr.get("attribution"),
            "version": attr.get("version"),
        })
    snapshot.sort(key=lambda item: str(item["attr_id"]))
    digest = hashlib.sha256(
        json.dumps(snapshot, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()[:16]
    return f"fv-{digest}"


# ========== 字段提取与解析 ==========
def extract_field_tokens(compute_logic: str, source_attrs: list[str]) -> list[str]:
    """汇总标签依赖的字段编号：显式声明 + 计算逻辑中实际引用，去重且保持顺序。"""
    ordered: list[str] = []
    seen: set[str] = set()

    def _add(token: str) -> None:
        if token not in seen:
            seen.add(token)
            ordered.append(token)

    for attr_id in source_attrs or []:
        _add(attr_id)
    for match in _TOKEN_RE.findall(compute_logic or ""):
        _add(match)
    return ordered


def _load_attr_def(attr_id: str) -> dict[str, Any] | None:
    """读取单个属性定义；来源被标记为不可读时抛出中断异常。"""
    attr = store.find_by_field("attr_dict", "attr_id", attr_id)
    if attr is not None:
        source = attr.get("source", "")
        if source in _unavailable_sources:
            raise DependencySourceUnavailable(attr_id, source)
    return attr


def resolve_tag_dependencies(tag: dict[str, Any]) -> dict[str, Any]:
    """对单个标签执行共用字段解析流程（不做持久化）。

    返回可复查的诊断记录，包含：

    - field_version：本次解析所基于的字段版本；
    - dependencies：逐字段的解析结果（resolved/missing/来源等）；
    - missing_attrs：未能在属性字典中找到的字段；
    - valid：所有字段均已解析且来源可读；
    - interrupted：是否因来源暂时不可读而中断。
    """
    tag_id = tag["tag_id"]
    compute_logic = tag.get("compute_logic", "")
    source_attrs = list(tag.get("source_attrs", []))
    field_version = current_field_version()
    tokens = extract_field_tokens(compute_logic, source_attrs)

    dependencies: list[dict[str, Any]] = []
    missing: list[str] = []
    interrupted_on: dict[str, Any] | None = None

    for token in tokens:
        try:
            attr = _load_attr_def(token)
        except DependencySourceUnavailable as exc:
            dependencies.append({
                "attr_id": token,
                "resolved": False,
                "source": exc.source,
                "source_available": False,
                "attr_version": None,
                "reason": "source_unavailable",
            })
            interrupted_on = {"attr_id": exc.attr_id, "source": exc.source}
            # 来源读不到：中断本次解析，后续字段不再尝试读取
            break

        if attr is None:
            missing.append(token)
            dependencies.append({
                "attr_id": token,
                "resolved": False,
                "source": None,
                "source_available": None,
                "attr_version": None,
                "reason": "attr_not_found",
            })
        else:
            dependencies.append({
                "attr_id": token,
                "resolved": True,
                "source": attr.get("source"),
                "source_available": True,
                "attr_version": attr.get("version"),
                "attr_name": attr.get("attr_name"),
                "data_type": attr.get("data_type"),
                "reason": "resolved",
            })

    return {
        "tag_id": tag_id,
        "tag_version": tag.get("version", ""),
        "compute_logic": compute_logic,
        "declared_source_attrs": source_attrs,
        "field_version": field_version,
        "dependencies": dependencies,
        "missing_attrs": missing,
        "valid": len(missing) == 0 and interrupted_on is None,
        "interrupted": interrupted_on is not None,
        "interrupted_on": interrupted_on,
    }


# ========== 诊断持久化 ==========
def _persist_confirmed(tag_id: str, diagnostic: dict[str, Any]) -> None:
    """持久化完整确认的诊断（仅完整解析成功时调用）。"""
    record = copy.deepcopy(diagnostic)
    record["confirmed"] = True
    store.set_artifact(CONFIRMED_ARTIFACT, tag_id, record)


def _persist_partial(tag_id: str, diagnostic: dict[str, Any], batch_id: str | None) -> None:
    """持久化中断检查的部分诊断；绝不触碰已确认产物。"""
    record = copy.deepcopy(diagnostic)
    record["confirmed"] = False
    record["batch_id"] = batch_id
    store.set_artifact(PARTIAL_ARTIFACT, tag_id, record)


def get_confirmed_diagnostic(tag_id: str) -> dict[str, Any] | None:
    """读取某标签已确认的依赖诊断。"""
    return store.get_artifact(CONFIRMED_ARTIFACT, tag_id)


def get_partial_diagnostic(tag_id: str) -> dict[str, Any] | None:
    """读取某标签最近一次中断检查留下的部分诊断。"""
    return store.get_artifact(PARTIAL_ARTIFACT, tag_id)


def list_confirmed_diagnostics() -> list[dict[str, Any]]:
    """列出全部已确认的标签依赖诊断。"""
    return store.list_artifacts(CONFIRMED_ARTIFACT)


# ========== 对外流程：单标签检查 / 跨标签批量检查 ==========
def resolve_and_persist(tag: dict[str, Any], *, batch_id: str | None = None) -> dict[str, Any]:
    """共用字段解析流程 + 诊断持久化。

    - 完整解析：写入 confirmed 桶，可被同字段版本的重算复用；
    - 来源中断：写入 partial 桶，不覆盖任何已确认产物。
    """
    diagnostic = resolve_tag_dependencies(tag)
    if diagnostic["interrupted"]:
        _persist_partial(tag["tag_id"], diagnostic, batch_id)
    else:
        _persist_confirmed(tag["tag_id"], diagnostic)
    return diagnostic


def check_tags(tag_ids: list[str], *, batch_id: str | None = None) -> dict[str, Any]:
    """跨多个标签执行依赖检查。

    按提交顺序逐个解析；第一条完整解析并持久化后，若第二条来源暂时读不到，
    则中断整批检查（后续标签不再读取），已完成的诊断保留、部分诊断单独落盘，
    已确认的计算产物不受影响。
    """
    results: list[dict[str, Any]] = []
    interrupted = False
    interrupted_tag: str | None = None
    checked: list[str] = []

    for tag_id in tag_ids:
        tag = store.find_by_field("tag_define", "tag_id", tag_id)
        if tag is None:
            results.append({
                "tag_id": tag_id,
                "valid": False,
                "interrupted": False,
                "missing_attrs": [],
                "field_version": current_field_version(),
                "error": "Tag not found",
            })
            continue

        diagnostic = resolve_and_persist(tag, batch_id=batch_id)
        checked.append(tag_id)
        results.append({
            "tag_id": tag_id,
            "valid": diagnostic["valid"],
            "interrupted": diagnostic["interrupted"],
            "interrupted_on": diagnostic["interrupted_on"],
            "missing_attrs": diagnostic["missing_attrs"],
            "field_version": diagnostic["field_version"],
            "dependencies": diagnostic["dependencies"],
        })
        if diagnostic["interrupted"]:
            interrupted = True
            interrupted_tag = tag_id
            break

    summary = {
        "batch_id": batch_id,
        "field_version": current_field_version(),
        "tag_ids": list(tag_ids),
        "checked_tag_ids": checked,
        "results": results,
        "interrupted": interrupted,
        "interrupted_tag": interrupted_tag,
        "completed": not interrupted,
    }
    if batch_id:
        store.set_artifact(CHECK_BATCH_ARTIFACT, batch_id, copy.deepcopy(summary))
    return summary


# ========== 标签计算侧：复用同版本诊断 ==========
def prepare_compute_dependencies(
    tag: dict[str, Any],
) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    """标签计算装载依赖的统一入口。

    先按当前字段版本查验已持久化的确认诊断：

    - 诊断属于本次字段版本且字段仍可解析 → 直接复用（``reused``），不再重读来源；
    - 无同版本诊断 → 走共用解析流程重新解析（``re_resolved``）并持久化；
    - 来源暂时不可读 → 抛出 :class:`DependencySourceUnavailable`，中断计算，
      且不会覆盖之前确认的计算产物。

    返回 ``(attr_defs, report)``：``attr_defs`` 与历史 ``compute_tag`` 中构建的
    属性定义映射结构保持一致，保证名单计算行为不变。
    """
    tag_id = tag["tag_id"]
    field_version = current_field_version()
    confirmed = get_confirmed_diagnostic(tag_id)

    report: dict[str, Any] = {
        "tag_id": tag_id,
        "field_version": field_version,
        "reused": [],
        "re_resolved": [],
        "unavailable": [],
        "reused_diagnostic": False,
    }
    attr_defs: dict[str, dict[str, Any]] = {}

    if confirmed is not None and confirmed.get("field_version") == field_version:
        # 同字段版本：诊断可复用。逐条核验来源当前是否仍可读，
        # 若来源恰好故障，则中断而不是静默重算。
        for dep in confirmed.get("dependencies", []):
            attr_id = dep["attr_id"]
            if not dep.get("resolved"):
                report["unavailable"].append({
                    "attr_id": attr_id,
                    "reason": dep.get("reason", "unresolved"),
                })
                continue
            attr = _load_attr_def(attr_id)  # 可能抛 DependencySourceUnavailable
            if attr is not None:
                attr_defs[attr_id] = attr
                report["reused"].append({
                    "attr_id": attr_id,
                    "source": dep.get("source"),
                    "attr_version": dep.get("attr_version"),
                    "origin": "reused_diagnostic",
                })
        report["reused_diagnostic"] = True
        return attr_defs, report

    # 无同版本诊断：重新执行共用字段解析流程
    diagnostic = resolve_tag_dependencies(tag)
    if diagnostic["interrupted"]:
        _persist_partial(tag_id, diagnostic, batch_id=None)
        blocked = diagnostic["interrupted_on"] or {}
        report["unavailable"].append({
            "attr_id": blocked.get("attr_id"),
            "source": blocked.get("source"),
            "reason": "source_unavailable",
        })
        raise DependencySourceUnavailable(
            blocked.get("attr_id", ""), blocked.get("source", "")
        )

    _persist_confirmed(tag_id, diagnostic)
    for dep in diagnostic["dependencies"]:
        attr_id = dep["attr_id"]
        if dep.get("resolved"):
            attr = store.find_by_field("attr_dict", "attr_id", attr_id)
            if attr is not None:
                attr_defs[attr_id] = attr
        report["re_resolved"].append({
            "attr_id": attr_id,
            "source": dep.get("source"),
            "attr_version": dep.get("attr_version"),
            "origin": "re_resolved",
            "resolved": dep.get("resolved", False),
            "reason": dep.get("reason"),
        })
    return attr_defs, report


def get_task_dependency_report(task_id: str) -> dict[str, Any] | None:
    """从已持久化诊断查看某计算任务对应标签的依赖复用查验情况。"""
    task_row = store.find_by_field("tag_compute", "task_id", task_id)
    if task_row is None:
        return None
    tag_id = task_row["tag_id"]
    field_version = current_field_version()
    confirmed = get_confirmed_diagnostic(tag_id)
    partial = get_partial_diagnostic(tag_id)
    return {
        "task_id": task_id,
        "tag_id": tag_id,
        "current_field_version": field_version,
        "confirmed_diagnostic": confirmed,
        "reusable": bool(
            confirmed is not None and confirmed.get("field_version") == field_version
        ),
        "partial_diagnostic": partial,
    }
