"""迁移确认编排：状态回填、绑定校验、确认与撤回、页面与接口共用一份结果。

绑定有两层，任一不符即判确认失效：

- 批次：确认时记下 run_id 与 version_seq。跑出新批次后，旧确认不再代表当前判定。
- 引用内容：确认时记下该下游引用集合的摘要。登记改过（哪怕只是加一处）就失效。

失效只是把该下游退回未确认（旧引用重新可用），并不删除记录，页面上单独标出来。
"""

from __future__ import annotations

import re

from app import config, errors, store
from app.modules.m8_confirm import refs as refs_mod

REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9_.:\-]{1,64}$")
MAX_BATCH = 200


def current_run() -> dict | None:
    return store.latest_run()


def _empty_row(consumer: str) -> dict:
    return {
        "consumer": consumer,
        "state": "pending",
        "run_id": None,
        "version_seq": None,
        "bound_digest": None,
        "refs": [],
        "state_version": 0,
        "confirmed_at": None,
        "retracted_at": None,
        "updated_at": None,
        "last_request_id": None,
        "stored_state": None,
        "binding": {"ok": True, "reason": None, "detail": ""},
    }


def effective_state(stored: dict | None, run: dict | None, digest: str,
                    content: str | None = None) -> dict:
    """把库里的记录、当前批次与当前引用摘要合成一个有效状态。

    先比下游写了哪些引用（refs_digest），再比每条引用落到什么字段（content_digest）：
    前者变了说明登记引用集合改过；后者变了说明引用字段本身换了目标。
    """
    if stored is None:
        base = _empty_row("")
        base["current_digest"] = digest
        base["current_content_digest"] = content
        return base

    base = dict(stored)
    base["bound_digest"] = stored.get("refs_digest")
    base["bound_content_digest"] = stored.get("content_digest")
    base["current_digest"] = digest
    base["current_content_digest"] = content
    base["stored_state"] = stored["state"]
    base["binding"] = {"ok": True, "reason": None, "detail": ""}

    if stored["state"] != "confirmed":
        return base

    if stored.get("refs_digest") != digest:
        base["state"] = "invalid"
        base["binding"] = {
            "ok": False,
            "reason": "CONFIRM_REFS_CHANGED",
            "detail": "登记引用内容与确认时不一致",
        }
        return base
    if content is not None and stored.get("content_digest") not in (None, content):
        base["state"] = "invalid"
        base["binding"] = {
            "ok": False,
            "reason": "CONFIRM_FIELD_CHANGED",
            "detail": "引用解析到的字段与确认时不一致",
        }
        return base
    if run is None:
        base["state"] = "invalid"
        base["binding"] = {"ok": False, "reason": "CONFIRM_RUN_STALE",
                           "detail": "当前没有批次，确认无从核对"}
        return base
    if stored.get("run_id") != run.get("run_id"):
        base["state"] = "invalid"
        base["binding"] = {
            "ok": False,
            "reason": "CONFIRM_RUN_STALE",
            "detail": f"确认绑定 {stored.get('run_id')}，当前批次 {run.get('run_id')}",
        }
        return base
    return base


def build(run: dict | None) -> dict:
    """整页数据：每个下游的有效状态、实际可用引用与被拒绝原因。"""
    stored = store.list_confirmations()
    consumers = (run or {}).get("refs", {}).get("consumers") or []
    resolution = (run or {}).get("resolution") or {}

    views: list[dict] = []
    states: dict[str, str] = {}
    for consumer in consumers:
        digest = refs_mod.refs_digest(consumer.get("refs") or [])
        content = refs_mod.content_digest(consumer, resolution)
        row = effective_state(stored.get(consumer["name"]), run, digest, content)
        row["consumer"] = consumer["name"]
        state = row["state"]
        view = refs_mod.consumer_view(consumer, resolution, state)
        view.update({
            "state": state,
            "stored_state": row.get("stored_state"),
            "binding": row.get("binding"),
            "state_version": row.get("state_version", 0),
            "run_id": row.get("run_id"),
            "version_seq": row.get("version_seq"),
            "confirmed_at": row.get("confirmed_at"),
            "retracted_at": row.get("retracted_at"),
            "current_digest": digest,
            "bound_digest": row.get("bound_digest"),
            "current_content_digest": content,
            "bound_content_digest": row.get("bound_content_digest"),
        })
        views.append(view)
        states[consumer["name"]] = state

    summary = refs_mod.summarize(views, states)
    return {
        "run_id": (run or {}).get("run_id"),
        "version_seq": (run or {}).get("version_seq"),
        "generated_at": config.now_utc().isoformat(),
        "rows": views,
        "summary": summary,
        "events": store.list_confirmation_events(limit=30),
    }


def _resolve_consumer(run: dict | None, consumer_name: str) -> dict:
    if run is None:
        raise errors.NotFound("还没有批次，先在版本历史页跑一次")
    for consumer in (run.get("refs") or {}).get("consumers") or []:
        if consumer["name"] == consumer_name:
            return consumer
    raise errors.NotFound("没有这个下游节点", detail={"consumer": consumer_name})


def _parse_request_id(payload: dict) -> str | None:
    raw = payload.get("request_id")
    if raw in (None, ""):
        return None
    text = str(raw).strip()
    if not REQUEST_ID_RE.match(text):
        raise errors.BadInput("request_id 含非法字符", detail={"request_id": text})
    return text


def _parse_version(payload: dict) -> int:
    raw = payload.get("state_version")
    if raw is None:
        raise errors.BadInput("确认必须带上 state_version", detail={"field": "state_version"})
    try:
        value = int(raw)
    except (TypeError, ValueError):
        raise errors.BadInput("state_version 必须是整数", detail={"value": repr(raw)})
    if value < 0:
        raise errors.BadInput("state_version 不能为负", detail={"value": value})
    return value


def _confirm_payload(action: str, consumer: dict, payload: dict) -> dict:
    run = current_run()
    if run is None:
        raise errors.NotFound("还没有批次，先在版本历史页跑一次")
    resolution = (run or {}).get("resolution") or {}
    digest = refs_mod.refs_digest(consumer.get("refs") or [])
    content = refs_mod.content_digest(consumer, resolution)
    stored = store.get_confirmation(consumer["name"])
    effective = effective_state(stored, run, digest, content)
    renew = effective["state"] == "invalid" and effective.get("stored_state") == "confirmed"

    result = store.apply_confirmation(
        consumer=consumer["name"],
        action=action,
        expected_version=_parse_version(payload),
        run_id=run["run_id"],
        version_seq=int(run["version_seq"]),
        refs_digest=digest,
        content_digest=content,
        refs=consumer.get("refs") or [],
        request_id=_parse_request_id(payload),
        now=config.now_utc().isoformat(),
        renew_confirmed=renew,
    )
    return result


def confirm(consumer_name: str, payload: dict | None = None) -> dict:
    body = payload or {}
    run = current_run()
    consumer = _resolve_consumer(run, consumer_name)
    result = _confirm_payload("confirm", consumer, body)
    return _response(consumer_name, result, "confirm")


def retract(consumer_name: str, payload: dict | None = None) -> dict:
    body = payload or {}
    run = current_run()
    consumer = _resolve_consumer(run, consumer_name)
    result = _confirm_payload("retract", consumer, body)
    return _response(consumer_name, result, "retract")


def _response(consumer_name: str, result: dict, action: str) -> dict:
    row = result.get("row") or {}
    return {
        "consumer": consumer_name,
        "action": action,
        "changed": bool(result.get("changed")),
        "replayed": bool(result.get("replayed")),
        "event_id": result.get("event_id"),
        "state": row.get("state"),
        "state_version": row.get("state_version", 0),
        "run_id": row.get("run_id"),
        "version_seq": row.get("version_seq"),
        "refs_digest": row.get("refs_digest"),
        "confirmed_at": row.get("confirmed_at"),
        "retracted_at": row.get("retracted_at"),
    }


def _normalize_entries(payload: dict) -> list[dict]:
    """consumers 里每项可以是名字，也可以是 {consumer, state_version}。

    带上各自的版本号，批内并发才不会互相顶掉别人的正确版本。
    """
    raw = payload.get("consumers")
    if raw is None:
        single = payload.get("consumer")
        raw = [single] if single else []
    if not isinstance(raw, list) or not raw:
        raise errors.BadInput("需要给出 consumers 列表", detail={"field": "consumers"})
    if len(raw) > MAX_BATCH:
        raise errors.BadInput("一次最多处理 200 个下游", detail={"count": len(raw)})

    entries: list[dict] = []
    seen: set[str] = set()
    for item in raw:
        if isinstance(item, dict):
            name = str(item.get("consumer") or item.get("name") or "").strip()
            version = item.get("state_version")
        else:
            name = str(item or "").strip()
            version = None
        if not name:
            raise errors.BadInput("下游名字不能为空", detail={"field": "consumers"})
        if name in seen:
            raise errors.BadInput("下游名字重复", detail={"consumer": name})
        seen.add(name)
        entries.append({"consumer": name, "state_version": version})
    return entries


def batch(action: str, payload: dict | None = None) -> dict:
    """批量确认 / 撤回：逐个独立提交，失败的记录原因，不影响其他下游。

    批内每个下游各自带 state_version；并发下只有一个能赢，其余按过期拒绝。
    """
    body = payload or {}
    if action not in ("confirm", "retract"):
        raise errors.BadInput("动作只能是 confirm 或 retract", detail={"action": action})
    entries = _normalize_entries(body)
    run = current_run()
    if run is None:
        raise errors.NotFound("还没有批次，先在版本历史页跑一次")

    base_request = _parse_request_id(body)
    default_version = body.get("state_version")
    results: list[dict] = []
    for entry in entries:
        name = entry["consumer"]
        per_payload = {
            "state_version": entry["state_version"]
            if entry["state_version"] is not None else default_version,
            "request_id": f"{base_request}:{name}" if base_request else None,
        }
        try:
            consumer = _resolve_consumer(run, name)
            result = _confirm_payload(action, consumer, per_payload)
            results.append(dict(_response(name, result, action), ok=True))
        except errors.FieldmapError as exc:
            results.append({
                "consumer": name,
                "action": action,
                "ok": False,
                "error": exc.code,
                "label": exc.label,
                "message": exc.message,
                "detail": exc.detail,
            })
    return {
        "action": action,
        "consumers": len(entries),
        "ok": sum(1 for item in results if item["ok"]),
        "failed": sum(1 for item in results if not item["ok"]),
        "results": results,
    }


def run_view(run: dict) -> dict:
    """给已算好的批次补一份确认快照：不改动批次里存下的判定，只叠加现状。"""
    payload = build(run)
    return {
        "run_id": payload["run_id"],
        "summary": payload["summary"],
        "rows": [
            {
                "name": row["name"],
                "kind": row["kind"],
                "state": row["state"],
                "stored_state": row["stored_state"],
                "binding": row["binding"],
                "state_version": row["state_version"],
                "run_id": row["run_id"],
                "version_seq": row["version_seq"],
                "confirmed_at": row["confirmed_at"],
                "refs": row["counts"]["refs"],
                "usable": row["counts"]["usable"],
                "blocked_old": row["counts"]["blocked_old"],
                "broken": row["counts"]["broken"],
                "old_refs": row["counts"]["old_refs"],
                "blocked": row["blocked"],
            }
            for row in payload["rows"]
        ],
    }

def events(consumer: str | None = None, limit: int = 50) -> list[dict]:
    return store.list_confirmation_events(consumer=consumer, limit=limit)
