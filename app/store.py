"""SQLite 读写：批次快照、运行清单、隔离行、键值状态。

只放数据访问，不放判定；调用方拿到的是普通字典。
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
from pathlib import Path

from app import config

_LOCK = threading.Lock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS snapshots (
    schema_id      TEXT PRIMARY KEY,
    version_label  TEXT NOT NULL,
    source_path    TEXT NOT NULL,
    source_sha256  TEXT NOT NULL,
    encoding       TEXT NOT NULL,
    bom            INTEGER NOT NULL,
    field_count    INTEGER NOT NULL,
    blocked_count  INTEGER NOT NULL,
    created_at     TEXT NOT NULL,
    payload_json   TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS runs (
    run_id         TEXT PRIMARY KEY,
    batch_key      TEXT NOT NULL UNIQUE,
    old_schema_id  TEXT NOT NULL,
    new_schema_id  TEXT NOT NULL,
    version_seq    INTEGER NOT NULL,
    input_sha256   TEXT NOT NULL,
    ruleset        TEXT NOT NULL,
    created_at     TEXT NOT NULL,
    effective_from TEXT NOT NULL,
    effective_to   TEXT,
    summary_json   TEXT NOT NULL,
    payload_json   TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS quarantine (
    schema_id     TEXT NOT NULL,
    line          INTEGER,
    table_name    TEXT,
    column_name   TEXT,
    reason        TEXT NOT NULL,
    blocking      INTEGER NOT NULL,
    detail        TEXT,
    raw           TEXT
);
CREATE TABLE IF NOT EXISTS kv (
    k TEXT PRIMARY KEY,
    v TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS migration_confirmations (
    consumer        TEXT PRIMARY KEY,
    state           TEXT NOT NULL,
    run_id          TEXT,
    version_seq     INTEGER,
    refs_digest     TEXT,
    content_digest  TEXT,
    refs_json       TEXT,
    state_version   INTEGER NOT NULL,
    confirmed_at    TEXT,
    retracted_at    TEXT,
    updated_at      TEXT NOT NULL,
    last_request_id TEXT
);
CREATE TABLE IF NOT EXISTS migration_events (
    event_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    consumer        TEXT NOT NULL,
    action          TEXT NOT NULL,
    run_id          TEXT,
    version_seq     INTEGER,
    refs_digest     TEXT,
    state_version   INTEGER NOT NULL,
    request_id      TEXT,
    created_at      TEXT NOT NULL,
    UNIQUE(consumer, request_id)
);
CREATE INDEX IF NOT EXISTS idx_quarantine_schema ON quarantine(schema_id);
CREATE INDEX IF NOT EXISTS idx_migration_events_consumer ON migration_events(consumer);
"""


def db_path() -> Path:
    raw = os.environ.get("METRICGRAPH_DB", "").strip()
    path = Path(raw) if raw else config.DEFAULT_DB
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(db_path(), timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db() -> None:
    with _LOCK, _connect() as conn:
        conn.executescript(SCHEMA)
        _migrate(conn)


def _migrate(conn: sqlite3.Connection) -> None:
    """给旧库补列：只加缺的，不动已有数据。"""
    columns = {row["name"] for row in
               conn.execute("PRAGMA table_info(migration_confirmations)").fetchall()}
    if columns and "content_digest" not in columns:
        conn.execute("ALTER TABLE migration_confirmations ADD COLUMN content_digest TEXT")


def reset() -> None:
    """测试与重建用：清空全部业务表。"""
    with _LOCK, _connect() as conn:
        conn.executescript(SCHEMA)
        for table in ("snapshots", "runs", "quarantine", "kv",
                      "migration_confirmations", "migration_events"):
            conn.execute(f"DELETE FROM {table}")


def _dumps(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _loads(value):
    if not value:
        return None
    return json.loads(value)


# ---------- 快照 ----------

def save_snapshot(snapshot: dict) -> None:
    with _LOCK, _connect() as conn:
        conn.execute(
            """INSERT INTO snapshots (schema_id, version_label, source_path, source_sha256,
                   encoding, bom, field_count, blocked_count, created_at, payload_json)
               VALUES (?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(schema_id) DO UPDATE SET
                   version_label=excluded.version_label,
                   source_path=excluded.source_path,
                   source_sha256=excluded.source_sha256,
                   encoding=excluded.encoding,
                   bom=excluded.bom,
                   field_count=excluded.field_count,
                   blocked_count=excluded.blocked_count,
                   created_at=excluded.created_at,
                   payload_json=excluded.payload_json""",
            (
                snapshot["schema_id"], snapshot["version_label"], snapshot["source_path"],
                snapshot["source_sha256"], snapshot["encoding"], int(bool(snapshot["bom"])),
                int(snapshot["field_count"]), int(snapshot["blocked_count"]),
                snapshot["created_at"], _dumps(snapshot),
            ),
        )


def load_snapshot(schema_id: str) -> dict | None:
    with _LOCK, _connect() as conn:
        row = conn.execute(
            "SELECT payload_json FROM snapshots WHERE schema_id=?", (schema_id,)
        ).fetchone()
    return _loads(row["payload_json"]) if row else None


def list_snapshots(limit: int = 20) -> list[dict]:
    with _LOCK, _connect() as conn:
        rows = conn.execute(
            """SELECT schema_id, version_label, source_path, source_sha256, encoding, bom,
                      field_count, blocked_count, created_at
               FROM snapshots ORDER BY created_at DESC, schema_id LIMIT ?""",
            (limit,),
        ).fetchall()
    return [dict(row) for row in rows]


def save_quarantine(schema_id: str, rows: list[dict]) -> None:
    with _LOCK, _connect() as conn:
        conn.execute("DELETE FROM quarantine WHERE schema_id=?", (schema_id,))
        conn.executemany(
            """INSERT INTO quarantine (schema_id, line, table_name, column_name, reason,
                   blocking, detail, raw) VALUES (?,?,?,?,?,?,?,?)""",
            [
                (
                    schema_id, int(row.get("line") or 0), row.get("table") or "",
                    row.get("column") or "", row["reason"], int(bool(row.get("blocking"))),
                    row.get("detail") or "", row.get("raw") or "",
                )
                for row in rows
            ],
        )


def list_quarantine(schema_id: str) -> list[dict]:
    with _LOCK, _connect() as conn:
        rows = conn.execute(
            """SELECT line, table_name, column_name, reason, blocking, detail, raw
               FROM quarantine WHERE schema_id=? ORDER BY line, reason""",
            (schema_id,),
        ).fetchall()
    return [
        {
            "line": row["line"],
            "table": row["table_name"],
            "column": row["column_name"],
            "reason": row["reason"],
            "blocking": bool(row["blocking"]),
            "detail": row["detail"],
            "raw": row["raw"],
        }
        for row in rows
    ]


# ---------- 运行清单 ----------

def save_run(run: dict) -> None:
    with _LOCK, _connect() as conn:
        conn.execute(
            """INSERT INTO runs (run_id, batch_key, old_schema_id, new_schema_id, version_seq,
                   input_sha256, ruleset, created_at, effective_from, effective_to,
                   summary_json, payload_json)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                run["run_id"], run["batch_key"], run["old_schema_id"], run["new_schema_id"],
                int(run["version_seq"]), run["input_sha256"], run["ruleset"],
                run["created_at"], run["effective_from"], run["effective_to"],
                _dumps(run["summary"]), _dumps(run),
            ),
        )


def find_run_by_key(batch_key: str) -> dict | None:
    with _LOCK, _connect() as conn:
        row = conn.execute(
            "SELECT payload_json FROM runs WHERE batch_key=?", (batch_key,)
        ).fetchone()
    return _loads(row["payload_json"]) if row else None


def run_by_id(run_id: str) -> dict | None:
    with _LOCK, _connect() as conn:
        row = conn.execute(
            "SELECT payload_json FROM runs WHERE run_id=?", (run_id,)
        ).fetchone()
    return _loads(row["payload_json"]) if row else None


def latest_run() -> dict | None:
    with _LOCK, _connect() as conn:
        row = conn.execute(
            "SELECT payload_json FROM runs ORDER BY version_seq DESC LIMIT 1"
        ).fetchone()
    return _loads(row["payload_json"]) if row else None


def list_runs(limit: int = 30) -> list[dict]:
    with _LOCK, _connect() as conn:
        rows = conn.execute(
            """SELECT summary_json FROM runs ORDER BY version_seq DESC, run_id LIMIT ?""",
            (limit,),
        ).fetchall()
    return [_loads(row["summary_json"]) for row in rows]


def count_runs() -> int:
    with _LOCK, _connect() as conn:
        row = conn.execute("SELECT COUNT(*) AS n FROM runs").fetchone()
    return int(row["n"])


def next_version_seq() -> int:
    with _LOCK, _connect() as conn:
        row = conn.execute("SELECT COALESCE(MAX(version_seq), 0) AS n FROM runs").fetchone()
    return int(row["n"]) + 1


def current_version_seq() -> int:
    with _LOCK, _connect() as conn:
        row = conn.execute("SELECT COALESCE(MAX(version_seq), 0) AS n FROM runs").fetchone()
    return int(row["n"])


def list_windows() -> list[dict]:
    with _LOCK, _connect() as conn:
        rows = conn.execute(
            "SELECT run_id, effective_from, effective_to FROM runs ORDER BY version_seq"
        ).fetchall()
    return [dict(row) for row in rows]


# ---------- 键值 ----------

def set_kv(key: str, value) -> None:
    with _LOCK, _connect() as conn:
        conn.execute(
            "INSERT INTO kv (k, v) VALUES (?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
            (key, _dumps(value)),
        )


def get_kv(key: str, default=None):
    with _LOCK, _connect() as conn:
        row = conn.execute("SELECT v FROM kv WHERE k=?", (key,)).fetchone()
    return _loads(row["v"]) if row else default


# ---------- 下游迁移确认 ----------

CONFIRM_ACTIONS = ("confirm", "retract")

_CONFIRM_FIELDS = (
    "consumer", "state", "run_id", "version_seq", "refs_digest", "content_digest",
    "refs_json", "state_version", "confirmed_at", "retracted_at", "updated_at",
    "last_request_id",
)


def _confirmation_row(row: sqlite3.Row | None) -> dict | None:
    if row is None:
        return None
    return {
        "consumer": row["consumer"],
        "state": row["state"],
        "run_id": row["run_id"],
        "version_seq": int(row["version_seq"]) if row["version_seq"] is not None else None,
        "refs_digest": row["refs_digest"],
        "content_digest": row["content_digest"],
        "refs": _loads(row["refs_json"]) or [],
        "state_version": int(row["state_version"]),
        "confirmed_at": row["confirmed_at"],
        "retracted_at": row["retracted_at"],
        "updated_at": row["updated_at"],
        "last_request_id": row["last_request_id"],
    }


def _connect_immediate() -> sqlite3.Connection:
    """手动控制事务的连接：确认的校验与写入必须在同一个事务里。"""
    conn = sqlite3.connect(db_path(), timeout=15, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def get_confirmation(consumer: str) -> dict | None:
    with _LOCK, _connect() as conn:
        row = conn.execute(
            "SELECT * FROM migration_confirmations WHERE consumer=?", (consumer,)
        ).fetchone()
    return _confirmation_row(row)


def list_confirmations() -> dict[str, dict]:
    with _LOCK, _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM migration_confirmations ORDER BY consumer"
        ).fetchall()
    return {row["consumer"]: _confirmation_row(row) for row in rows}


def list_confirmation_events(consumer: str | None = None, limit: int = 50) -> list[dict]:
    with _LOCK, _connect() as conn:
        if consumer:
            rows = conn.execute(
                """SELECT * FROM migration_events WHERE consumer=?
                   ORDER BY event_id DESC LIMIT ?""",
                (consumer, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM migration_events ORDER BY event_id DESC LIMIT ?", (limit,)
            ).fetchall()
    return [
        {
            "event_id": int(row["event_id"]),
            "consumer": row["consumer"],
            "action": row["action"],
            "run_id": row["run_id"],
            "version_seq": int(row["version_seq"])
            if row["version_seq"] is not None else None,
            "refs_digest": row["refs_digest"],
            "state_version": int(row["state_version"]),
            "request_id": row["request_id"],
            "created_at": row["created_at"],
        }
        for row in rows
    ]


def count_confirmation_events(consumer: str | None = None) -> int:
    with _LOCK, _connect() as conn:
        if consumer:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM migration_events WHERE consumer=?", (consumer,)
            ).fetchone()
        else:
            row = conn.execute("SELECT COUNT(*) AS n FROM migration_events").fetchone()
    return int(row["n"])


def _record_event(conn: sqlite3.Connection, *, consumer: str, action: str,
                  run_id: str, version_seq: int, refs_digest: str,
                  state_version: int, request_id: str | None,
                  created_at: str) -> int:
    """审计账：一次状态变更记一笔；单列成函数便于验证回滚。"""
    cursor = conn.execute(
        """INSERT INTO migration_events
           (consumer, action, run_id, version_seq, refs_digest, state_version,
            request_id, created_at)
           VALUES (?,?,?,?,?,?,?,?)""",
        (consumer, action, run_id, int(version_seq), refs_digest, state_version,
         request_id, created_at),
    )
    return int(cursor.lastrowid)


def apply_confirmation(*, consumer: str, action: str, expected_version: int,
                       run_id: str, version_seq: int, refs_digest: str,
                       content_digest: str, refs: list[str],
                       request_id: str | None,
                       now: str, renew_confirmed: bool = False) -> dict:
    """一次确认 / 撤回：版本校验、状态写入、审计记账在同一事务内。

    - 版本号乐观锁：expected_version 落后于当前 state_version 即拒绝。
    - 幂等：同一 (consumer, request_id) 重试直接返回当时结果，不再记一笔。
    - 原子：状态与事件要么都落库，要么整体回滚回原状态。
    - renew_confirmed：原确认虽在库里但绑定已失效，允许重新绑定并升版本。
    """
    from app import errors

    if action not in CONFIRM_ACTIONS:
        raise errors.BadInput("动作只能是 confirm 或 retract", detail={"action": action})
    with _LOCK:
        conn = _connect_immediate()
        try:
            conn.execute("BEGIN IMMEDIATE")

            if request_id:
                prior = conn.execute(
                    """SELECT * FROM migration_events
                       WHERE consumer=? AND request_id=?""",
                    (consumer, request_id),
                ).fetchone()
                if prior is not None:
                    if prior["action"] != action:
                        raise errors.ConfirmConflict(
                            "该请求号已用于另一动作",
                            detail={"consumer": consumer, "request_id": request_id,
                                    "recorded_action": prior["action"],
                                    "action": action},
                        )
                    row = conn.execute(
                        "SELECT * FROM migration_confirmations WHERE consumer=?",
                        (consumer,),
                    ).fetchone()
                    conn.execute("COMMIT")
                    return {"row": _confirmation_row(row), "changed": False,
                            "replayed": True, "event_id": int(prior["event_id"]),
                            "action": prior["action"],
                            "event_state_version": int(prior["state_version"])}

            current = conn.execute(
                "SELECT * FROM migration_confirmations WHERE consumer=?", (consumer,)
            ).fetchone()
            current_version = int(current["state_version"]) if current else 0
            if int(expected_version) != current_version:
                raise errors.ConfirmStale(
                    f"确认版本 {expected_version} 已过期，当前为 {current_version}",
                    detail={"consumer": consumer, "expected": int(expected_version),
                            "current": current_version, "reason": "CONFIRM_STALE"},
                )

            current_state = current["state"] if current else None
            if action == "confirm":
                if current_state == "confirmed" and not renew_confirmed:
                    conn.execute("COMMIT")
                    return {"row": _confirmation_row(current), "changed": False,
                            "replayed": False, "event_id": None}
                new_state = "confirmed"
                confirmed_at = now
                retracted_at = current["retracted_at"] if current else None
            else:
                if current_state == "retracted":
                    # 已经撤回：重试按幂等处理，不再记一笔
                    conn.execute("COMMIT")
                    return {"row": _confirmation_row(current), "changed": False,
                            "replayed": False, "event_id": None}
                if current_state != "confirmed":
                    raise errors.ConfirmConflict(
                        "该下游当前没有可撤回的确认",
                        detail={"consumer": consumer,
                                "state": current_state or "pending",
                                "reason": "NOT_CONFIRMED"},
                    )
                new_state = "retracted"
                confirmed_at = current["confirmed_at"] if current else None
                retracted_at = now

            new_version = current_version + 1
            payload = _dumps(sorted(refs))
            if current is None:
                conn.execute(
                    """INSERT INTO migration_confirmations
                       (consumer, state, run_id, version_seq, refs_digest,
                        content_digest, refs_json, state_version, confirmed_at,
                        retracted_at, updated_at, last_request_id)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (consumer, new_state, run_id, int(version_seq), refs_digest,
                     content_digest, payload, new_version, confirmed_at, retracted_at,
                     now, request_id),
                )
            else:
                cursor = conn.execute(
                    """UPDATE migration_confirmations
                       SET state=?, run_id=?, version_seq=?, refs_digest=?,
                           content_digest=?, refs_json=?, state_version=?,
                           confirmed_at=?, retracted_at=?, updated_at=?,
                           last_request_id=?
                       WHERE consumer=? AND state_version=?""",
                    (new_state, run_id, int(version_seq), refs_digest, content_digest,
                     payload, new_version, confirmed_at, retracted_at, now, request_id,
                     consumer, current_version),
                )
                if cursor.rowcount != 1:
                    raise errors.ConfirmStale(
                        "确认状态在写入前被并发修改",
                        detail={"consumer": consumer, "expected": current_version,
                                "reason": "CONFIRM_STALE"},
                    )

            event_id = _record_event(
                conn, consumer=consumer, action=action, run_id=run_id,
                version_seq=int(version_seq), refs_digest=refs_digest,
                state_version=new_version, request_id=request_id, created_at=now,
            )
            row = conn.execute(
                "SELECT * FROM migration_confirmations WHERE consumer=?", (consumer,)
            ).fetchone()
            conn.execute("COMMIT")
            return {"row": _confirmation_row(row), "changed": True, "replayed": False,
                    "event_id": event_id}
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                # 事务没起来或已结束：没有可回滚的状态
                pass
            raise
        finally:
            conn.close()
