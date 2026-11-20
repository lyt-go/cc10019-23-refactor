"""标签定义路由：管理标签规则的定义与依赖校验。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request

from app.schemas import ActionPayload, ActionResult
from app.services.tag_define import (
    list_tags, get_tag, create_tag, update_tag, delete_tag, get_tags_by_level, validate_tag_logic,
)
from app.services.tag_dependency import check_tags
from app.store import store, query_filters

router = APIRouter(prefix="/api/tag-define", tags=["tag_define"])


@router.get("")
def index(request: Request, page: int = 1, size: int = 20) -> dict[str, Any]:
    rows = store.rows("tag_define")
    # query contract: filters=query_filters(request)
    filters = query_filters(request)
    if filters:
        rows = [r for r in rows if all(str(r.get(k)) == str(v) for k, v in filters.items())]
    total = len(rows)
    start = (page - 1) * size
    return {"items": rows[start : start + size], "total": total, "page": page, "size": size}


@router.get("/{entry_id}")
def detail(entry_id: int) -> dict[str, Any]:
    row = store.find("tag_define", entry_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Not found")
    return row


@router.post("")
def create(payload: dict[str, Any]) -> ActionResult:
    row = create_tag(payload)
    return ActionResult(ok=True, message="Created", entry=row)


@router.put("/{entry_id}")
def update(entry_id: int, payload: dict[str, Any]) -> ActionResult:
    row = store.find("tag_define", entry_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Not found")
    updated = update_tag(row["tag_id"], payload)
    return ActionResult(ok=True, message="Updated", entry=updated)


@router.delete("/{entry_id}")
def remove(entry_id: int) -> ActionResult:
    row = store.find("tag_define", entry_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Not found")
    delete_tag(row["tag_id"])
    return ActionResult(ok=True, message="Deleted")


@router.get("/by-level/{level}")
def by_level(level: str) -> list[dict[str, Any]]:
    return get_tags_by_level(level)


@router.post("/dependency-check")
def dependency_check(payload: dict[str, Any]) -> dict[str, Any]:
    """跨多个标签的标签依赖检查（共用字段解析流程）。

    请求体：``{"tag_ids": ["TAG-001", "TAG-002"], "batch_id": "可选"}``。
    任一标签的字段来源暂时不可读时整批中断，已解析标签的诊断保留，
    部分诊断单独持久化且不覆盖已确认产物。
    """
    tag_ids = payload.get("tag_ids") or []
    if not isinstance(tag_ids, list) or not tag_ids:
        raise HTTPException(status_code=400, detail="tag_ids must be a non-empty list")
    batch_id = payload.get("batch_id")
    return check_tags([str(tag_id) for tag_id in tag_ids], batch_id=batch_id)


@router.get("/{tag_id}/validate")
def validate(tag_id: str) -> dict[str, Any]:
    return validate_tag_logic(tag_id)


@router.post("/{entry_id}/actions")
def run_action(entry_id: int, payload: ActionPayload) -> ActionResult:
    row = store.find("tag_define", entry_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Not found")
    action = payload.action
    row["status"] = action
    row["pending"] = False
    return ActionResult(ok=True, message=action, entry=row)
