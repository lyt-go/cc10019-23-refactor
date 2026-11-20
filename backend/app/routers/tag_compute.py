"""标签计算路由：执行标签计算任务并获取结果。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request

from app.schemas import ActionPayload, ActionResult
from app.services.tag_compute import compute_tag, get_compute_result, list_compute_results, recompute_tag
from app.services.tag_dependency import (
    DependencySourceUnavailable,
    get_task_dependency_report,
)
from app.store import store

router = APIRouter(prefix="/api/tag-compute", tags=["tag_compute"])


@router.get("")
def index(page: int = 1, size: int = 20) -> dict[str, Any]:
    rows = store.rows("tag_compute")
    total = len(rows)
    start = (page - 1) * size
    return {"items": rows[start : start + size], "total": total, "page": page, "size": size}




@router.get("/export")
def export_entries() -> dict[str, Any]:
    rows = store.rows("tag_compute")
    return {"items": rows, "total": len(rows)}
@router.get("/{entry_id}")
def detail(entry_id: int) -> dict[str, Any]:
    row = store.find("tag_compute", entry_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Not found")
    return row


@router.post("/{task_id}/run")
def run(task_id: str) -> ActionResult:
    try:
        result = compute_tag(task_id)
        return ActionResult(ok=True, message="Computed", entry=result.model_dump())
    except DependencySourceUnavailable as e:
        raise HTTPException(status_code=409, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.get("/{task_id}/result")
def result(task_id: str) -> dict[str, Any]:
    try:
        res = get_compute_result(task_id)
    except DependencySourceUnavailable as e:
        raise HTTPException(status_code=409, detail=str(e))
    if res is None:
        raise HTTPException(status_code=404, detail="Result not found")
    return res


@router.get("/{task_id}/dependencies")
def dependencies(task_id: str) -> dict[str, Any]:
    """从已持久化的标签依赖诊断数据查验字段来源解析与同版本复用情况。"""
    report = get_task_dependency_report(task_id)
    if report is None:
        raise HTTPException(status_code=404, detail="Task not found")
    return report


@router.get("/artifacts/all")
def artifacts() -> list[dict[str, Any]]:
    return list_compute_results()


@router.post("/{task_id}/recompute")
def recompute(task_id: str, payload: dict[str, Any] | None = None) -> ActionResult:
    scope = payload.get("scope") if payload else None
    try:
        result = recompute_tag(task_id, scope)
        return ActionResult(ok=True, message="Recomputed", entry=result.model_dump())
    except DependencySourceUnavailable as e:
        raise HTTPException(status_code=409, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.post("/{entry_id}/actions")
def run_action(entry_id: int, payload: ActionPayload) -> ActionResult:
    row = store.find("tag_compute", entry_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Not found")
    action = payload.action
    row["status"] = action
    row["pending"] = False
    return ActionResult(ok=True, message=action, entry=row)
