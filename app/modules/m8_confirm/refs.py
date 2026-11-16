"""下游引用可用性：确认状态决定旧引用还能不能用。

三种写法分开算：

- 直接引用：写的名字就是新版本里的名字，任何时候都可用。
- 旧引用：写的是旧名字，要靠别名才落到新字段。未确认时可用；该下游一旦确认迁移，
  旧引用必须被拒绝，防止改回旧名字继续跑。
- 断链：身份换了、字段删了或别名不安全，任何状态下都不可用。
"""

from __future__ import annotations

import hashlib
import json

OLD_REF = "old_ref"
DIRECT_REF = "direct_ref"
BROKEN_REF = "broken_ref"

USABLE_STATES = ("pending", "retracted", "invalid")


def refs_digest(refs: list[str]) -> str:
    """引用内容的稳定摘要：只认排序后的引用集合，与登记顺序无关。"""
    payload = json.dumps(sorted(refs), ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _index(resolution: dict) -> dict[str, dict[str, dict]]:
    """consumer -> ref -> 解析结果。"""
    out: dict[str, dict[str, dict]] = {}
    for item in (resolution.get("resolved") or []) + (resolution.get("broken") or []):
        out.setdefault(item["consumer"], {})[item["ref"]] = item
    return out


def content_digest(consumer: dict, resolution: dict) -> str:
    """引用绑定的字段内容摘要：引用名 → 落到哪个身份、哪个新名字、经不经别名。

    与 refs_digest 的分工：refs_digest 只看下游写了哪些引用；这里再看每条引用解析到
    什么。同一个引用名换了目标字段时，两边摘要都会变，但原因要分开报。
    """
    index = _index(resolution).get(consumer["name"], {})
    entries = []
    for ref in sorted(consumer.get("refs") or []):
        item = index.get(ref) or {}
        kind = classify(item)
        entries.append([
            ref,
            kind,
            item.get("field_id"),
            item.get("new_name"),
        ])
    payload = json.dumps(entries, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def classify(item: dict | None) -> str:
    if item is None or item.get("field_id") is None:
        return BROKEN_REF
    target = (item.get("new_name") or "").lower()
    written = (item.get("column") or "").lower()
    if target and written == target:
        return DIRECT_REF
    return OLD_REF


def consumer_view(consumer: dict, resolution: dict, state: str) -> dict:
    """一个下游在当前状态下的实际可用引用与拒绝原因。"""
    index = _index(resolution).get(consumer["name"], {})
    usable: list[dict] = []
    blocked: list[dict] = []
    old_refs = 0

    for ref in sorted(consumer.get("refs") or []):
        item = index.get(ref)
        kind = classify(item)
        if kind == OLD_REF:
            old_refs += 1

        if kind == BROKEN_REF:
            blocked.append({
                "ref": ref,
                "reason": "REF_BROKEN",
                "detail": (item or {}).get("detail") or "引用解析不到字段",
                "new_name": None,
                "field_id": None,
            })
            continue

        if kind == OLD_REF and state == "confirmed":
            blocked.append({
                "ref": ref,
                "reason": "OLD_REF_BLOCKED",
                "detail": f"已确认迁移，旧引用需改为 {item['new_name']}",
                "new_name": item["new_name"],
                "field_id": item["field_id"],
            })
            continue

        usable.append({
            "ref": ref,
            "via": "direct" if kind == DIRECT_REF else "alias",
            "field_id": item["field_id"],
            "new_name": item["new_name"],
        })

    broken_count = sum(1 for row in blocked if row["reason"] == "REF_BROKEN")
    return {
        "name": consumer["name"],
        "kind": consumer.get("kind") or "task",
        "refs": sorted(consumer.get("refs") or []),
        "usable": usable,
        "blocked": blocked,
        "counts": {
            "refs": len(consumer.get("refs") or []),
            "usable": len(usable),
            "old_refs": old_refs,
            "blocked": len(blocked),
            "blocked_old": len(blocked) - broken_count,
            "broken": broken_count,
            "via_alias": sum(1 for row in usable if row["via"] == "alias"),
        },
        "old_refs_usable": state in USABLE_STATES,
    }


def summarize(views: list[dict], states: dict[str, str]) -> dict:
    counts = {"refs": 0, "usable": 0, "blocked": 0, "blocked_old": 0,
              "broken": 0, "via_alias": 0, "old_refs": 0}
    for view in views:
        for key in counts:
            counts[key] += view["counts"][key]
    state_counts: dict[str, int] = {}
    for state in states.values():
        state_counts[state] = state_counts.get(state, 0) + 1

    # 撤场条件：仍在用旧名字、又还没确认迁移的下游，一个都不能剩
    old_name_consumers = [view["name"] for view in views if view["counts"]["old_refs"]]
    pinned = [name for name in old_name_consumers if states[name] != "confirmed"]
    return {
        "consumers": len(views),
        "by_state": state_counts,
        "confirmed": state_counts.get("confirmed", 0),
        "pending": state_counts.get("pending", 0),
        "retracted": state_counts.get("retracted", 0),
        "invalid": state_counts.get("invalid", 0),
        **counts,
        "retirement": {
            "old_name_consumers": sorted(old_name_consumers),
            "pinned": sorted(pinned),
            "can_retire": not pinned,
        },
    }
