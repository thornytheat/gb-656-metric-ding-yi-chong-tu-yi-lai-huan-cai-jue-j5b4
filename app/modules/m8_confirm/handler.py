"""M8 入口：迁移确认页与逐下游确认 / 撤回接口。"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, Response

from app import store, web
from app.modules.m8_confirm import csv_out, service

router = APIRouter()


def _csv(body: str, filename: str) -> Response:
    return Response(
        content=body,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/migration", response_class=HTMLResponse)
def migration_page(request: Request):
    run = store.latest_run()
    payload = service.build(run)
    return web.page_or_404(
        request,
        "migration.html",
        "migration",
        run,
        rows=payload["rows"],
        summary=payload["summary"],
        events=payload["events"],
    )


@router.get("/api/migration")
def migration_api():
    run = store.latest_run()
    if not run:
        return {"run_id": None, "rows": [], "summary": {}, "events": []}
    return service.build(run)


@router.get("/api/migration/events")
def migration_events(consumer: str | None = None, limit: int = 50):
    return {
        "consumer": consumer,
        "total": store.count_confirmation_events(consumer),
        "events": service.events(consumer=consumer, limit=max(1, min(limit, 200))),
    }


@router.get("/api/migration/refs.csv")
def migration_refs_csv():
    run = store.latest_run()
    return _csv(csv_out.refs_csv(service.build(run)), "metricgraph-migration-refs.csv")


@router.get("/api/migration/state.csv")
def migration_state_csv():
    run = store.latest_run()
    return _csv(csv_out.state_csv(service.build(run)), "metricgraph-migration-state.csv")


@router.post("/api/migration/{consumer}/confirm")
def confirm_consumer(consumer: str, payload: dict | None = None):
    return service.confirm(consumer, payload)


@router.post("/api/migration/{consumer}/retract")
def retract_consumer(consumer: str, payload: dict | None = None):
    return service.retract(consumer, payload)


@router.post("/api/migration/confirm")
def confirm_batch(payload: dict | None = None):
    return service.batch("confirm", payload)


@router.post("/api/migration/retract")
def retract_batch(payload: dict | None = None):
    return service.batch("retract", payload)
