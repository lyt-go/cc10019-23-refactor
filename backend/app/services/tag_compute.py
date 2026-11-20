"""标签计算引擎：执行标签规则的批量计算，支持全量/增量/差量计算。"""
from __future__ import annotations

import time
from typing import Any

from app.schemas import TagComputeResult
from app.services.tag_dependency import prepare_compute_dependencies
from app.store import store


def _eval_condition(user: dict[str, Any], logic: str, attr_defs: dict[str, dict[str, Any]]) -> bool:
    """简化条件求值：支持 AND/OR/NOT 和基本比较。"""
    logic = logic.strip()
    # 处理括号
    if logic.startswith("(") and logic.endswith(")"):
        return _eval_condition(user, logic[1:-1], attr_defs)

    # 处理 NOT
    if logic.upper().startswith("NOT "):
        return not _eval_condition(user, logic[4:], attr_defs)

    # 处理 AND
    if " AND " in logic.upper():
        parts = _split_by_op(logic, "AND")
        return all(_eval_condition(user, p, attr_defs) for p in parts)

    # 处理 OR
    if " OR " in logic.upper():
        parts = _split_by_op(logic, "OR")
        return any(_eval_condition(user, p, attr_defs) for p in parts)

    # 基本比较：field op value
    return _eval_atom(user, logic, attr_defs)


def _split_by_op(logic: str, op: str) -> list[str]:
    """按操作符分割，注意括号嵌套。"""
    parts: list[str] = []
    depth = 0
    current = ""
    tokens = logic.split()
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if tok == "(":
            depth += 1
        elif tok == ")":
            depth -= 1
        if tok.upper() == op and depth == 0:
            if current.strip():
                parts.append(current.strip())
            current = ""
        else:
            current += tok + " "
        i += 1
    if current.strip():
        parts.append(current.strip())
    return parts if len(parts) > 1 else [logic]


def _eval_atom(user: dict[str, Any], logic: str, attr_defs: dict[str, Any]) -> bool:
    """求值原子条件。"""
    logic = logic.strip()
    # 支持 =, !=, <, >, <=, >=, IN, NOT IN
    for op in ["!=", "<=", ">=", "=", "<", ">"]:
        if op in logic:
            parts = logic.split(op, 1)
            if len(parts) == 2:
                left = parts[0].strip()
                right = parts[1].strip()
                left_val = user.get(left)
                # 处理字符串引号
                if right.startswith("'") and right.endswith("'"):
                    right = right[1:-1]
                elif right.startswith('"') and right.endswith('"'):
                    right = right[1:-1]
                # 处理数值
                try:
                    right = float(right)
                    if left_val is not None:
                        try:
                            left_val = float(left_val)
                        except ValueError:
                            pass
                except ValueError:
                    pass
                # 处理布尔
                if right == "True":
                    right = True
                elif right == "False":
                    right = False

                if op == "=":
                    return left_val == right
                elif op == "!=":
                    return left_val != right
                elif op == "<":
                    if left_val is None:
                        return False
                    return left_val < right
                elif op == ">":
                    if left_val is None:
                        return False
                    return left_val > right
                elif op == "<=":
                    if left_val is None:
                        return False
                    return left_val <= right
                elif op == ">=":
                    if left_val is None:
                        return False
                    return left_val >= right
    # 处理 IN
    if " IN " in logic.upper():
        parts = logic.upper().split(" IN ", 1)
        field = logic[:len(parts[0])].strip()
        val_list_str = logic[len(parts[0])+4:].strip()
        if val_list_str.startswith("[") and val_list_str.endswith("]"):
            val_list_str = val_list_str[1:-1]
        vals = [v.strip().strip("'\"").strip() for v in val_list_str.split(",")]
        field_val = str(user.get(field, ""))
        return field_val in vals

    return bool(user.get(logic, False))


def compute_tag(task_id: str) -> TagComputeResult:
    """执行标签计算任务。"""
    task_row = store.find_by_field("tag_compute", "task_id", task_id)
    if task_row is None:
        raise ValueError(f"Task not found: {task_id}")

    tag_id = task_row["tag_id"]
    tag = store.find_by_field("tag_define", "tag_id", tag_id)
    if tag is None:
        raise ValueError(f"Tag not found: {tag_id}")

    start_time = time.time()
    scope = task_row["compute_scope"]

    # 依赖装载统一走共用字段解析流程：同字段版本复用已持久化诊断，
    # 来源暂时不可读时抛出 DependencySourceUnavailable 中断计算，
    # 此前已确认的计算产物不会被覆盖。
    attr_defs, dependency_report = prepare_compute_dependencies(tag)

    # 模拟用户数据（实际应从事件数据生成）
    # 这里使用内存中的示例用户数据
    users = _load_users_for_tag(tag, attr_defs, scope)

    # 计算标签值
    results: list[dict[str, Any]] = []
    for user in users:
        matched = _eval_condition(user, tag["compute_logic"], attr_defs)
        results.append({
            "user_id": user.get("user_id"),
            "tag_id": tag_id,
            "tag_value": matched,
            "compute_time": task_row.get("output_partition", ""),
        })

    # 存储计算结果产物
    result_data = {
        "task_id": task_id,
        "tag_id": tag_id,
        "compute_scope": scope,
        "result_count": len(results),
        "user_tag_values": results[:100],  # 只存储前100条示例
        "output_partition": task_row.get("output_partition", ""),
        "compute_time_ms": int((time.time() - start_time) * 1000),
        "field_version": dependency_report["field_version"],
        "dependency_report": dependency_report,
    }
    store.set_artifact("tag_compute", task_id, result_data)

    # 更新任务状态
    store.update("tag_compute", task_row["id"], {
        "exec_state": "success",
        "result_count": len(results),
        "error_log": "",
    })

    return TagComputeResult(**result_data)


def _load_users_for_tag(tag: dict[str, Any], attr_defs: dict[str, Any], scope: str) -> list[dict[str, Any]]:
    """加载需要计算标签的用户数据。"""
    # 模拟用户数据 - 基于属性定义生成
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
        # 增量模式只处理新用户
        if scope == "incremental" and i > 32:
            continue
        # 差量模式只处理变化用户
        if scope == "delta" and i % 3 != 0:
            continue
        users.append(user)
    return users


def get_compute_result(task_id: str) -> dict[str, Any] | None:
    """获取标签计算结果。"""
    artifact = store.get_artifact("tag_compute", task_id)
    if artifact:
        return artifact
    # 尝试实时计算
    try:
        result = compute_tag(task_id)
        return result.model_dump()
    except ValueError:
        return None


def list_compute_results() -> list[dict[str, Any]]:
    """列出所有标签计算结果。"""
    return store.list_artifacts("tag_compute")


def recompute_tag(task_id: str, new_scope: str | None = None) -> TagComputeResult:
    """重新计算标签，支持切换计算范围。"""
    task_row = store.find_by_field("tag_compute", "task_id", task_id)
    if task_row is None:
        raise ValueError(f"Task not found: {task_id}")

    if new_scope:
        store.update("tag_compute", task_row["id"], {"compute_scope": new_scope})
        # 失效相关缓存
        store.invalidate_compute_cache(f"tag_compute:{task_id}")

    return compute_tag(task_id)
