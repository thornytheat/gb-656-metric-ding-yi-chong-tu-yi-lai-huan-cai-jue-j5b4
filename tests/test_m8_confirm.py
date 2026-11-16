"""M8 迁移确认：逐下游确认、撤回、绑定失效、并发拒绝、原子与幂等。"""

from __future__ import annotations

import threading

import pytest

from app import errors, store
from app.modules.m8_confirm import refs as refs_mod, service as confirm_service


def _row(api: dict, name: str) -> dict:
    return next(row for row in api["rows"] if row["name"] == name)


def _confirm(client, name: str, version: int, **extra):
    return client.post(f"/api/migration/{name}/confirm",
                       json={"state_version": version, **extra})


def _retract(client, name: str, version: int, **extra):
    return client.post(f"/api/migration/{name}/retract",
                       json={"state_version": version, **extra})


# ---------- 页面与初始状态 ----------

def test_page_renders_with_all_consumers(client, run):
    page = client.get("/migration")
    assert page.status_code == 200
    assert "迁移确认" in page.text
    for consumer in run["refs"]["consumers"]:
        assert consumer["name"] in page.text


def test_everything_starts_pending_and_old_refs_usable(client, run):
    api = client.get("/api/migration").json()
    assert api["summary"]["consumers"] == len(run["refs"]["consumers"])
    assert api["summary"]["pending"] == api["summary"]["consumers"]
    assert api["summary"]["confirmed"] == 0
    assert api["summary"]["blocked_old"] == 0
    # 断链在任何状态下都不可用，数量与影响面一致
    assert api["summary"]["broken"] == 3
    for row in api["rows"]:
        assert row["old_refs_usable"] is True
        assert all(item["reason"] == "REF_BROKEN" for item in row["blocked"])


def test_alias_refs_usable_when_pending(run):
    api = confirm_service.build(run)
    row = _row(api, "dw.mv_order_daily")
    old = [item for item in row["usable"] if item["via"] == "alias"]
    assert [item["ref"] for item in old] == ["dim_order.order_no"]
    assert old[0]["new_name"] == "order_code"


# ---------- 确认与撤回 ----------

def test_confirm_blocks_old_refs_only_for_that_consumer(client):
    before = client.get("/api/migration").json()
    assert before["summary"]["confirmed"] == 0

    response = _confirm(client, "dw.mv_order_daily", 0)
    assert response.status_code == 200
    body = response.json()
    assert body["changed"] is True
    assert body["state"] == "confirmed"
    assert body["state_version"] == 1

    after = client.get("/api/migration").json()
    row = _row(after, "dw.mv_order_daily")
    assert row["state"] == "confirmed"
    assert row["old_refs_usable"] is False
    blocked = {item["ref"]: item for item in row["blocked"]}
    assert blocked["dim_order.order_no"]["reason"] == "OLD_REF_BLOCKED"
    assert blocked["dim_order.order_no"]["new_name"] == "order_code"
    # 直接引用照旧可用
    assert {item["ref"] for item in row["usable"]} == {
        "dim_order.order_amt", "dim_order.order_id"}

    # 其他未确认下游不收影响
    other = _row(after, "dw.mv_order_stat")
    assert other["state"] == "pending"
    assert any(item["ref"] == "dim_order.ord_no" and item["via"] == "alias"
               for item in other["usable"])
    assert after["summary"]["confirmed"] == 1
    assert after["summary"]["blocked_old"] == 1


def test_retract_restores_old_refs_for_that_consumer(client):
    _confirm(client, "dw.mv_order_daily", 0)
    response = _retract(client, "dw.mv_order_daily", 1)
    assert response.status_code == 200
    body = response.json()
    assert body["state"] == "retracted"
    assert body["state_version"] == 2

    api = client.get("/api/migration").json()
    row = _row(api, "dw.mv_order_daily")
    assert row["state"] == "retracted"
    assert row["old_refs_usable"] is True
    assert any(item["ref"] == "dim_order.order_no" and item["via"] == "alias"
               for item in row["usable"])
    assert row["blocked"] == []

    # 另一个已确认的下游仍保新约束
    _confirm(client, "dw.mv_order_stat", 0)
    _retract(client, "dw.mv_order_daily", 2)
    api = client.get("/api/migration").json()
    assert _row(api, "dw.mv_order_stat")["state"] == "confirmed"
    assert any(item["reason"] == "OLD_REF_BLOCKED"
               for item in _row(api, "dw.mv_order_stat")["blocked"])


def test_confirm_requires_known_consumer(client):
    response = _confirm(client, "dw.not_registered", 0)
    assert response.status_code == 404
    assert response.json()["error"] == "NOT_FOUND"


def test_retract_without_confirm_is_rejected(client):
    response = _retract(client, "dw.task_settle", 0)
    assert response.status_code == 409
    assert response.json()["error"] == "CONFIRM_CONFLICT"
    assert response.json()["detail"]["reason"] == "NOT_CONFIRMED"


def test_confirm_requires_state_version(client):
    response = client.post("/api/migration/dw.task_settle/confirm", json={})
    assert response.status_code == 400
    assert response.json()["error"] == "BAD_INPUT"


def test_batch_confirm_is_per_consumer_atomic(client):
    response = client.post("/api/migration/confirm", json={"consumers": [
        {"consumer": "dw.task_settle", "state_version": 0},
        {"consumer": "dw.mv_user_dim", "state_version": 0},
        {"consumer": "dw.not_registered", "state_version": 0},
    ]})
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] == 2 and body["failed"] == 1
    by_name = {item["consumer"]: item for item in body["results"]}
    assert by_name["dw.task_settle"]["ok"] is True
    assert by_name["dw.not_registered"]["error"] == "NOT_FOUND"

    api = client.get("/api/migration").json()
    assert _row(api, "dw.task_settle")["state"] == "confirmed"
    assert _row(api, "dw.mv_user_dim")["state"] == "confirmed"
    assert _row(api, "dw.mv_order_daily")["state"] == "pending"


# ---------- 版本与并发 ----------

def test_stale_version_is_rejected(client):
    assert _confirm(client, "dw.task_settle", 0).status_code == 200
    response = _confirm(client, "dw.task_settle", 0)
    assert response.status_code == 409
    assert response.json()["error"] == "CONFIRM_STALE"
    assert response.json()["detail"]["current"] == 1
    # 版本过期不改变状态
    assert store.get_confirmation("dw.task_settle")["state_version"] == 1


def test_concurrent_confirm_and_retract_rejects_stale_version(client):
    """已确认的下游上，确认与撤回并发带同一版本：只有一个落地，另一个按过期拒绝。"""
    _confirm(client, "dw.task_settle", 0)  # 状态 confirmed，版本 1

    outcomes: list[tuple[str, str]] = []
    barrier = threading.Barrier(2)

    def worker(action: str) -> None:
        barrier.wait()
        try:
            if action == "confirm":
                result = confirm_service.confirm("dw.task_settle", {"state_version": 1})
                outcomes.append((action, "noop" if not result["changed"] else "ok"))
            else:
                result = confirm_service.retract("dw.task_settle", {"state_version": 1})
                outcomes.append((action, "ok" if result["changed"] else "noop"))
        except errors.ConfirmStale:
            outcomes.append((action, "stale"))

    threads = [threading.Thread(target=worker, args=(action,))
               for action in ("retract", "confirm")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    # 至少一个被按过期或无事可做挡住；绝不出现两次真实变更
    assert not any(action == "confirm" and status == "ok" for action, status in outcomes)
    state = store.get_confirmation("dw.task_settle")
    # 确认（首次）一笔 + 撤回至多一笔，不会有第三笔
    assert store.count_confirmation_events("dw.task_settle") <= 2
    assert state["state"] in ("confirmed", "retracted")


def test_concurrent_confirms_one_wins_the_stale_one_is_rejected(client):
    """两个确认同时带 version 0：只能一个生效，另一个按过期拒绝，只记一笔。"""
    outcomes: list[str] = []
    barrier = threading.Barrier(2)

    def worker() -> None:
        barrier.wait()
        try:
            confirm_service.confirm("dw.task_settle", {"state_version": 0})
            outcomes.append("ok")
        except errors.ConfirmStale:
            outcomes.append("stale")

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert sorted(outcomes) == ["ok", "stale"]
    state = store.get_confirmation("dw.task_settle")
    assert state["state"] == "confirmed"
    assert state["state_version"] == 1
    assert store.count_confirmation_events("dw.task_settle") == 1


def test_concurrent_retracts_one_wins_the_stale_one_is_rejected(client):
    _confirm(client, "dw.task_settle", 0)
    outcomes: list[str] = []
    barrier = threading.Barrier(2)

    def worker() -> None:
        barrier.wait()
        try:
            confirm_service.retract("dw.task_settle", {"state_version": 1})
            outcomes.append("ok")
        except (errors.ConfirmStale, errors.ConfirmConflict):
            outcomes.append("rejected")

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert sorted(outcomes) == ["ok", "rejected"]
    state = store.get_confirmation("dw.task_settle")
    assert state["state"] == "retracted"
    assert state["state_version"] == 2
    # 确认一笔 + 撤回一笔，被拒的那次不记账
    assert store.count_confirmation_events("dw.task_settle") == 2


# ---------- 幂等与原子 ----------

def test_same_request_id_does_not_double_book(client):
    first = _confirm(client, "dw.task_settle", 0, request_id="rq-1").json()
    replay = _confirm(client, "dw.task_settle", 0, request_id="rq-1")
    assert replay.status_code == 200
    body = replay.json()
    assert body["replayed"] is True
    assert body["state_version"] == first["state_version"] == 1
    assert store.count_confirmation_events("dw.task_settle") == 1


def test_request_id_cannot_be_reused_for_other_action(client):
    _confirm(client, "dw.task_settle", 0, request_id="rq-2")
    response = _retract(client, "dw.task_settle", 1, request_id="rq-2")
    assert response.status_code == 409
    assert response.json()["error"] == "CONFIRM_CONFLICT"
    assert store.get_confirmation("dw.task_settle")["state"] == "confirmed"


def test_write_failure_rolls_back_and_retry_books_once(client, monkeypatch):
    original = store._record_event

    def boom(*args, **kwargs):
        raise RuntimeError("disk full")

    monkeypatch.setattr(store, "_record_event", boom)
    with pytest.raises(RuntimeError):
        confirm_service.confirm("dw.task_settle", {"state_version": 0})

    # 状态与事件都没落下
    assert store.get_confirmation("dw.task_settle") is None
    assert store.count_confirmation_events("dw.task_settle") == 0

    monkeypatch.setattr(store, "_record_event", original)
    result = confirm_service.confirm("dw.task_settle", {"state_version": 0})
    assert result["state"] == "confirmed"
    assert result["state_version"] == 1
    assert store.count_confirmation_events("dw.task_settle") == 1


def test_repeated_confirm_is_noop(client):
    first = _confirm(client, "dw.task_settle", 0).json()
    again = _confirm(client, "dw.task_settle", first["state_version"]).json()
    assert again["changed"] is False
    assert again["state_version"] == first["state_version"]
    assert store.count_confirmation_events("dw.task_settle") == 1


# ---------- 绑定失效 ----------

def test_new_run_invalidates_confirmations_but_keeps_record(client):
    _confirm(client, "dw.mv_order_daily", 0)
    before = client.get("/api/migration").json()
    assert before["summary"]["confirmed"] == 1

    response = client.post("/api/runs", json={"params": {"wave": "next"}})
    assert response.status_code == 200
    assert response.json()["version_seq"] == 2

    after = client.get("/api/migration").json()
    row = _row(after, "dw.mv_order_daily")
    assert row["state"] == "invalid"
    assert row["stored_state"] == "confirmed"
    assert row["binding"]["reason"] == "CONFIRM_RUN_STALE"
    # 确认失效后旧引用恢复兼容
    assert row["old_refs_usable"] is True
    assert any(item["via"] == "alias" for item in row["usable"])
    assert after["summary"]["confirmed"] == 0
    assert after["summary"]["invalid"] == 1
    # 记录没被删，重新确认走同一条记录升版本
    assert row["run_id"]
    refreshed = _confirm(client, "dw.mv_order_daily", row["state_version"])
    assert refreshed.status_code == 200
    assert refreshed.json()["state"] == "confirmed"
    assert refreshed.json()["run_id"] == after["run_id"]


def test_refs_content_change_invalidates_confirmation():
    """引用内容变化后原确认失效：摘要不同即判 CONFIRM_REFS_CHANGED。"""
    consumer = {"name": "v.a", "kind": "view",
                "refs": ["dim_order.order_no"], "upstream": [], "downstream": []}
    resolution = {"resolved": [
        {"consumer": "v.a", "ref": "dim_order.order_no", "table": "dim_order",
         "column": "order_no", "via": "alias", "field_id": "1002",
         "new_name": "order_code", "detail": ""},
    ], "broken": []}
    run = {"run_id": "fm-1", "version_seq": 1,
           "refs": {"consumers": [consumer]}, "resolution": resolution}

    digest = refs_mod.refs_digest(consumer["refs"])
    content = refs_mod.content_digest(consumer, resolution)
    stored = {"consumer": "v.a", "state": "confirmed", "run_id": "fm-1",
              "version_seq": 1, "refs_digest": digest, "refs": ["dim_order.order_no"],
              "content_digest": content,
              "state_version": 1, "confirmed_at": "2026-09-01T00:00:00+00:00",
              "retracted_at": None, "updated_at": "2026-09-01T00:00:00+00:00",
              "last_request_id": None}

    same = confirm_service.effective_state(stored, run, digest, content)
    assert same["state"] == "confirmed"

    changed = confirm_service.effective_state(
        stored, run, refs_mod.refs_digest(["dim_order.order_no", "dim_order.order_id"]),
        content)
    assert changed["state"] == "invalid"
    assert changed["stored_state"] == "confirmed"
    assert changed["binding"]["reason"] == "CONFIRM_REFS_CHANGED"


def test_referenced_field_change_invalidates_confirmation():
    """同一批引用名，解析到的字段身份变了：按引用字段更新失效。"""
    consumer = {"name": "v.a", "kind": "view",
                "refs": ["dim_order.order_no"], "upstream": [], "downstream": []}

    def _resolution(field_id: str, new_name: str) -> dict:
        return {"resolved": [
            {"consumer": "v.a", "ref": "dim_order.order_no", "table": "dim_order",
             "column": "order_no", "via": "alias", "field_id": field_id,
             "new_name": new_name, "detail": ""},
        ], "broken": []}

    digest = refs_mod.refs_digest(consumer["refs"])
    bound_content = refs_mod.content_digest(consumer, _resolution("1002", "order_code"))
    stored = {"consumer": "v.a", "state": "confirmed", "run_id": "fm-1",
              "version_seq": 1, "refs_digest": digest, "refs": ["dim_order.order_no"],
              "content_digest": bound_content,
              "state_version": 1, "confirmed_at": "2026-09-01T00:00:00+00:00",
              "retracted_at": None, "updated_at": "2026-09-01T00:00:00+00:00",
              "last_request_id": None}
    run = {"run_id": "fm-1", "version_seq": 1,
           "refs": {"consumers": [consumer]}, "resolution": {}}

    # 引用名没变，但落到另一个身份上
    rebound = refs_mod.content_digest(consumer, _resolution("9999", "order_code_v2"))
    verdict = confirm_service.effective_state(stored, run, digest, rebound)
    assert verdict["state"] == "invalid"
    assert verdict["binding"]["reason"] == "CONFIRM_FIELD_CHANGED"


def test_retire_blocked_until_all_old_name_consumers_confirmed(client):
    api = client.get("/api/migration").json()
    assert api["summary"]["retirement"]["can_retire"] is False
    assert api["summary"]["retirement"]["pinned"] == [
        "dw.mv_order_daily", "dw.mv_order_stat"]

    _confirm(client, "dw.mv_order_daily", 0)
    api = client.get("/api/migration").json()
    assert api["summary"]["retirement"]["pinned"] == ["dw.mv_order_stat"]

    _confirm(client, "dw.mv_order_stat", 0)
    api = client.get("/api/migration").json()
    assert api["summary"]["retirement"]["can_retire"] is True
    assert api["summary"]["retirement"]["pinned"] == []


def test_reference_content_change_invalidates_end_to_end(client):
    """换一份下游登记（该下游改用新字段名），原确认按引用变化失效。"""
    _confirm(client, "dw.mv_order_daily", 0)
    before = _row(client.get("/api/migration").json(), "dw.mv_order_daily")
    assert before["state"] == "confirmed"
    assert before["counts"]["blocked_old"] == 1

    response = client.post("/api/runs", json={
        "refs_file": "refs_downstream_v2.json", "params": {"wave": "refs2"}})
    assert response.status_code == 200
    assert response.json()["version_seq"] == 2

    after = _row(client.get("/api/migration").json(), "dw.mv_order_daily")
    assert after["state"] == "invalid"
    assert after["binding"]["reason"] == "CONFIRM_REFS_CHANGED"
    assert after["counts"]["old_refs"] == 0
    assert after["blocked"] == []


# ---------- 记账与展示 ----------

def test_events_are_recorded_and_listed(client):
    _confirm(client, "dw.task_settle", 0)
    _retract(client, "dw.task_settle", 1)
    payload = client.get("/api/migration/events?consumer=dw.task_settle").json()
    assert payload["total"] == 2
    actions = [item["action"] for item in payload["events"]]
    assert actions == ["retract", "confirm"]


def test_impact_page_shows_confirmation_status(client):
    _confirm(client, "dw.mv_order_daily", 0)
    page = client.get("/impact")
    assert page.status_code == 200
    assert "下游迁移确认" in page.text
    api = client.get("/api/impact").json()
    row = next(item for item in api["confirmations"]["rows"]
               if item["name"] == "dw.mv_order_daily")
    assert row["state"] == "confirmed"
    assert row["blocked_old"] == 1


def test_spec_page_shows_confirmation_summary(client):
    _confirm(client, "dw.mv_order_daily", 0)
    page = client.get("/spec")
    assert page.status_code == 200
    assert "下游迁移确认" in page.text


def test_refs_csv_marks_old_refs_blocked_only_after_confirm(client):
    import csv
    import io

    before = list(csv.DictReader(io.StringIO(client.get("/api/migration/refs.csv").text)))
    assert sum(1 for row in before if row["usable"] == "N") == 3
    assert all(row["reason"] == "REF_BROKEN" for row in before if row["usable"] == "N")

    _confirm(client, "dw.mv_order_daily", 0)
    after = list(csv.DictReader(io.StringIO(client.get("/api/migration/refs.csv").text)))
    blocked = [row for row in after if row["usable"] == "N"]
    assert len(blocked) == 4
    old = [row for row in blocked if row["reason"] == "OLD_REF_BLOCKED"]
    assert len(old) == 1
    assert old[0]["ref"] == "dim_order.order_no"
    assert old[0]["new_name"] == "order_code"


def test_state_csv_reports_binding_and_counts(client):
    import csv
    import io

    _confirm(client, "dw.mv_order_daily", 0)
    client.post("/api/runs", json={"params": {"wave": "csv"}})
    rows = {row["consumer"]: row
            for row in csv.DictReader(io.StringIO(client.get("/api/migration/state.csv").text))}
    row = rows["dw.mv_order_daily"]
    assert row["state"] == "invalid"
    assert row["stored_state"] == "confirmed"
    assert row["binding_reason"] == "CONFIRM_RUN_STALE"
    assert row["old_refs"] == "1"
    assert row["blocked_old"] == "0"
