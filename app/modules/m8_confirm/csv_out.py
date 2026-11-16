"""确认账导出：每个下游的状态、绑定与逐条引用的可用性。

引用一行一条，`usable` 列写 Y/N，`reason` 给出拒绝原因码，便于和引用账并排核对。
"""

from __future__ import annotations

import csv
import io

HEADER = [
    "consumer", "kind", "state", "stored_state", "state_version", "run_id",
    "version_seq", "confirmed_at", "ref", "via", "usable", "new_name", "reason",
]

STATE_HEADER = [
    "consumer", "state", "stored_state", "state_version", "binding_reason",
    "run_id", "version_seq", "confirmed_at", "retracted_at",
    "refs", "usable", "blocked_old", "broken", "old_refs",
]


def _buffer() -> tuple[io.StringIO, "csv.writer"]:
    buffer = io.StringIO()
    return buffer, csv.writer(buffer, lineterminator="\n")


def refs_csv(confirmations: dict) -> str:
    buffer, writer = _buffer()
    writer.writerow(HEADER)
    for row in confirmations.get("rows") or []:
        usable = {item["ref"]: item for item in row["usable"]}
        blocked = {item["ref"]: item for item in row["blocked"]}
        for ref in row["refs"]:
            if ref in blocked:
                item = blocked[ref]
                writer.writerow([
                    row["name"], row["kind"], row["state"], row.get("stored_state") or "",
                    row["state_version"], row.get("run_id") or "",
                    row.get("version_seq") or "", row.get("confirmed_at") or "",
                    ref, "", "N", item.get("new_name") or "", item["reason"],
                ])
            elif ref in usable:
                item = usable[ref]
                writer.writerow([
                    row["name"], row["kind"], row["state"], row.get("stored_state") or "",
                    row["state_version"], row.get("run_id") or "",
                    row.get("version_seq") or "", row.get("confirmed_at") or "",
                    ref, item["via"], "Y", item.get("new_name") or "", "",
                ])
    return buffer.getvalue()


def state_csv(confirmations: dict) -> str:
    buffer, writer = _buffer()
    writer.writerow(STATE_HEADER)
    for row in confirmations.get("rows") or []:
        binding = row.get("binding") or {}
        counts = row["counts"]
        writer.writerow([
            row["name"], row["state"], row.get("stored_state") or "",
            row["state_version"], binding.get("reason") or "",
            row.get("run_id") or "", row.get("version_seq") or "",
            row.get("confirmed_at") or "", row.get("retracted_at") or "",
            counts["refs"], counts["usable"], counts["blocked_old"],
            counts["broken"], counts["old_refs"],
        ])
    return buffer.getvalue()
