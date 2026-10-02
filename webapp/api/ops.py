"""Readiness and metrics (Bundle 7 spec §20.6).

``/ready`` runs each probe and answers 200, or 503 with the names of the
failing checks (never values or secrets). ``/metrics`` renders Prometheus
text behind the ``JOBSEARCH_METRICS_TOKEN`` bearer; with no token configured
the endpoint does not exist (404)."""
from __future__ import annotations

import hmac
import time
from datetime import datetime, timezone
from typing import Any, Callable

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, PlainTextResponse, Response

from webapp.api.route_classes import METRICS as METRICS_ROUTE, PUBLIC

router = APIRouter(tags=["ops"])


# ---- readiness probes: each returns True when healthy (exceptions count as failure) -----------

def _database(settings: Any) -> bool:
    from webapp.persistence.db import connect
    conn = connect(settings)
    try:
        return conn.execute("SELECT 1").fetchone()[0] == 1
    finally:
        conn.close()


def _migrations(settings: Any) -> bool:
    from webapp.persistence.bundle7_migrations import BUNDLE7_MIGRATIONS
    from webapp.persistence.db import connect
    from webapp.persistence.migrations import LEGACY_MIGRATION_IDS
    expected = set(LEGACY_MIGRATION_IDS) | {m.id for m in BUNDLE7_MIGRATIONS}
    conn = connect(settings)
    try:
        applied = {r[0] for r in conn.execute("SELECT id FROM schema_migrations").fetchall()}
    finally:
        conn.close()
    return expected <= applied


def _object_store(settings: Any) -> bool:
    from webapp.storage.object_store import object_store_from_settings
    object_store_from_settings(settings).exists("readiness/probe")  # a working store answers, True or False
    return True


def _catalog(settings: Any) -> bool:
    from product.entitlements import load_catalog
    from webapp.app import _project_path
    load_catalog(_project_path(settings.plan_catalog_path))
    return True


def _email_provider(settings: Any) -> bool:
    if not settings.is_hosted:
        return True
    from webapp.comms.outbox import provider_from_settings
    return settings.email_provider != "console" and provider_from_settings(settings) is not None


def _secret_key(settings: Any) -> bool:
    from webapp.deployment import MIN_SECRET_KEY_LENGTH
    return not settings.is_hosted or len(settings.secret_key or "") >= MIN_SECRET_KEY_LENGTH


READINESS_PROBES: dict[str, Callable[[Any], bool]] = {
    "database": _database, "migrations": _migrations, "object_store": _object_store, "catalog": _catalog,
    "email_provider": _email_provider, "secret_key": _secret_key,
}


@router.get("/ready", dependencies=[Depends(PUBLIC)])
def ready(request: Request):
    settings = request.app.state.settings
    failing = []
    for name, probe in READINESS_PROBES.items():
        try:
            ok = probe(settings)
        except Exception:  # noqa: BLE001 - a failing probe is reported by name only
            ok = False
        if not ok:
            failing.append(name)
    if failing:
        return JSONResponse({"status": "not_ready", "failing": failing}, status_code=503)
    return {"status": "ready"}


# ---- metrics ---------------------------------------------------------------------------------

def _database_lines(settings: Any) -> list[str]:
    from webapp.persistence.db import connect
    lines = ["# TYPE jobs gauge"]
    conn = connect(settings)
    try:
        for kind, status, count in conn.execute("SELECT kind, status, COUNT(*) FROM jobs GROUP BY kind, status "
                                                "ORDER BY kind, status").fetchall():
            lines.append(f'jobs{{kind="{kind}",status="{status}"}} {count}')
        lines.append("# TYPE outbox_messages gauge")
        for status, count in conn.execute("SELECT status, COUNT(*) FROM outbound_messages GROUP BY status "
                                          "ORDER BY status").fetchall():
            lines.append(f'outbox_messages{{status="{status}"}} {count}')
        oldest = conn.execute("SELECT MIN(received_at) FROM billing_webhook_events WHERE processed_at IS NULL"
                              ).fetchone()[0]
        lag = 0.0 if oldest is None else max(0.0, (datetime.now(timezone.utc)
                                                   - datetime.fromisoformat(oldest)).total_seconds())
        lines += ["# TYPE billing_webhook_lag_seconds gauge", f"billing_webhook_lag_seconds {lag:.3f}"]
        lines += ["# TYPE ai_calls_total counter", "# TYPE ai_cost_micro_usd_total counter"]
        for provider, calls, cost in conn.execute("SELECT provider, COUNT(*), COALESCE(SUM(cost_micro_usd), 0) FROM "
                                                  "ai_cost_events GROUP BY provider ORDER BY provider").fetchall():
            lines.append(f'ai_calls_total{{provider="{provider}"}} {calls}')
            lines.append(f'ai_cost_micro_usd_total{{provider="{provider}"}} {cost}')
    finally:
        conn.close()
    return lines


def _writer_lock_lines() -> list[str]:
    from webapp.persistence.dbapi import LOCK_STATS
    lines = ["# TYPE db_writer_lock_wait_seconds summary", "# TYPE db_writer_lock_hold_seconds summary",
             "# TYPE db_writer_lock_timeouts_total counter"]
    for site, s in sorted(LOCK_STATS.snapshot().items()):
        label = f'site="{site}"'
        lines += [f"db_writer_lock_wait_seconds_sum{{{label}}} {s['wait_seconds_total']:.6f}",
                  f"db_writer_lock_wait_seconds_count{{{label}}} {int(s['acquisitions'])}",
                  f"db_writer_lock_wait_seconds_max{{{label}}} {s['wait_seconds_max']:.6f}",
                  f"db_writer_lock_hold_seconds_sum{{{label}}} {s['hold_seconds_total']:.6f}",
                  f"db_writer_lock_hold_seconds_count{{{label}}} {int(s['acquisitions'])}",
                  f"db_writer_lock_hold_seconds_max{{{label}}} {s['hold_seconds_max']:.6f}",
                  f"db_writer_lock_timeouts_total{{{label}}} {int(s['timeouts'])}"]
    return lines


def render_metrics(settings: Any) -> str:
    from webapp.observability import METRICS
    lines = ["# TYPE http_requests_total counter", "# TYPE http_request_duration_seconds histogram",
             "# TYPE database_busy_total counter"]
    lines += METRICS.render()
    lines += _writer_lock_lines()
    try:
        lines += _database_lines(settings)
    except Exception:  # noqa: BLE001 - metrics still render when the database is down
        lines.append("metrics_database_scrape_failed 1")
    lines.append(f"metrics_scrape_timestamp_seconds {int(time.time())}")
    return "\n".join(lines) + "\n"


@router.get("/metrics", dependencies=[Depends(METRICS_ROUTE)])
def metrics(request: Request):
    token = request.app.state.settings.metrics_token
    if not token:
        return Response(status_code=404)
    presented = request.headers.get("authorization", "")
    if not presented.lower().startswith("bearer ") or not hmac.compare_digest(presented[7:].strip(), token):
        return Response(status_code=401, headers={"WWW-Authenticate": "Bearer"})
    return PlainTextResponse(render_metrics(request.app.state.settings), media_type="text/plain; version=0.0.4")


def writer_lock_summary(*, hours: int = 24, top: int = 5) -> dict[str, Any]:
    """For the admin dashboard: lock timeouts in the last ``hours`` and the busiest sites by wait."""
    from webapp.persistence.dbapi import LOCK_STATS
    timeouts = LOCK_STATS.timeouts_since(time.time() - hours * 3600)
    sites = sorted(LOCK_STATS.snapshot().items(), key=lambda item: item[1]["wait_seconds_total"], reverse=True)
    return {"timeouts": sum(timeouts.values()), "timeouts_by_site": timeouts,
            "top_sites": [{"site": site, "wait_seconds": round(s["wait_seconds_total"], 3),
                           "acquisitions": int(s["acquisitions"])} for site, s in sites[:top]]}
