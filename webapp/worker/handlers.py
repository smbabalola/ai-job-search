"""Worker handlers (Bundle 7 spec §20.2). Each is idempotent. Kinds whose
domain arrives in later tasks (outbox, notifications, discovery schedules,
purge, export) register their handlers there."""
from __future__ import annotations

import logging
import random
from datetime import datetime, timedelta
from typing import Any, Callable

from webapp.persistence import dbapi
from webapp.services.autonomy_scheduler import tick_once
from webapp.worker.runner import Handler, JobContext, PermanentFailure, RetryLater, ts

logger = logging.getLogger("webapp.worker")

RATE_LIMIT_BUCKET_RETENTION = timedelta(days=1)

# (table, expiry column) — rows past expiry can never be used again.
_EXPIRING = (
    ("pairing_codes", "expires_at"),
    ("handoff_tickets", "expires_at"),
    ("extension_access_tokens", "expires_at"),
    ("extension_refresh_tokens", "expires_at"),
    ("email_tokens", "expires_at"),
    ("web_sessions", "absolute_expires_at"),
)


def _iso(moment: datetime) -> str:
    # the token modules store datetime.isoformat(); comparing in that form keeps text order correct
    return moment.isoformat()


def sweep_tokens(conn: dbapi.Connection, *, now: datetime) -> int:
    """Delete expired credentials and stale rate-limit windows (no commit)."""
    deleted = 0
    for table, column in _EXPIRING:
        deleted += conn.execute(f"DELETE FROM {table} WHERE {column} < ?", (_iso(now),)).rowcount
    deleted += conn.execute("DELETE FROM rate_limit_buckets WHERE window_start < ?",
                            (_iso(now - RATE_LIMIT_BUCKET_RETENTION),)).rowcount
    return deleted


def _usage_sweep(ctx: JobContext, payload: dict) -> None:
    from webapp.services.usage import sweep_expired
    conn = ctx.connect()
    try:
        released = sweep_expired(conn, ctx.clock())
        conn.commit()
        if released:
            logger.info("usage_sweep_released count=%s", released)
    finally:
        conn.close()


def _tokens_sweep(ctx: JobContext, payload: dict) -> None:
    conn = ctx.connect()
    try:
        sweep_tokens(conn, now=ctx.clock())
        conn.commit()
    finally:
        conn.close()


def _billing_webhook_handler(webhooks: Any) -> Handler:
    def handle(ctx: JobContext, payload: dict) -> None:
        row_id = payload.get("event_row_id")
        if not isinstance(row_id, str):
            raise PermanentFailure("billing.webhook.process needs event_row_id")
        conn = ctx.connect()
        try:
            webhooks.process_event(conn, row_id, now=ctx.clock())
            conn.commit()
            row = conn.execute("SELECT processed_at FROM billing_webhook_events WHERE id = ?", (row_id,)).fetchone()
        finally:
            conn.close()
        if row is None:
            raise PermanentFailure(f"billing webhook event {row_id} does not exist")
        if row[0] is None:
            raise RetryLater("billing webhook event is not applicable yet (e.g. its checkout is not recorded)")
    return handle


def _autonomy_tick_handler(providers_factory: Callable[[], Any]) -> Handler:
    providers: list[Any] = []

    def handle(ctx: JobContext, payload: dict) -> None:
        if not providers:
            providers.append(providers_factory())
        conn = ctx.connect()
        try:
            tick_once(conn, ctx.settings, providers[0], clock=ctx.clock, rng=random.Random(),
                      worker_id=ctx.worker_id)
        finally:
            conn.close()
    return handle


def _outbox_dispatch_handler(settings: Any, email_provider: Any) -> Handler:
    from webapp.comms.outbox import dispatch_batch, provider_from_settings
    providers: list[Any] = [email_provider] if email_provider is not None else []

    def handle(ctx: JobContext, payload: dict) -> None:
        if not providers:
            providers.append(provider_from_settings(settings))
        conn = ctx.connect()
        try:
            dispatch_batch(conn, provider=providers[0], now=ctx.clock(), app_origin=settings.app_origin, limit=20)
        finally:
            conn.close()
    return handle


def _email_webhook_handler(ctx: JobContext, payload: dict) -> None:
    """Events are applied as they are stored; this re-applies any left unprocessed."""
    from webapp.comms.outbox import suppress
    conn = ctx.connect()
    try:
        rows = conn.execute("SELECT id, kind, payload_json FROM email_provider_events WHERE processed_at IS NULL "
                            "ORDER BY received_at LIMIT 100").fetchall()
        for row in rows:
            import json
            address = json.loads(row["payload_json"]).get("address")
            if row["kind"] in ("HARD_BOUNCE", "COMPLAINT") and address:
                suppress(conn, address, reason=row["kind"], now=ctx.clock())
            conn.execute("UPDATE email_provider_events SET processed_at = ? WHERE id = ?",
                         (ts(ctx.clock()), row["id"]))
        conn.commit()
    finally:
        conn.close()


def _notify_digest(ctx: JobContext, payload: dict) -> None:
    from webapp.services.notifications import send_digests
    conn = ctx.connect()
    try:
        send_digests(conn, now=ctx.clock())
        conn.commit()
    finally:
        conn.close()


def _approval_expiry_scan(ctx: JobContext, payload: dict) -> None:
    from webapp.services.notifications import scan_expiring_approvals
    conn = ctx.connect()
    try:
        scan_expiring_approvals(conn, settings=ctx.settings, now=ctx.clock())
        conn.commit()
    finally:
        conn.close()


def _scheduled_discovery_handler(runner_factory: Callable[[Any], Any]) -> Handler:
    """§20.3. The periodic job (no payload) fans out one job per due schedule
    (deduplicated per schedule and slot); a per-schedule job runs it."""
    def handle(ctx: JobContext, payload: dict) -> None:
        from webapp.services import search_schedules
        from webapp.worker.runner import enqueue
        conn = ctx.connect()
        try:
            search_workspace_id = payload.get("search_workspace_id")
            if search_workspace_id is None:
                for schedule in search_schedules.due_schedules(conn, now=ctx.clock()):
                    enqueue(conn, kind="discovery.scheduled_run", account_id=schedule["account_id"],
                            payload={"search_workspace_id": schedule["search_workspace_id"]},
                            dedupe_key=f"schedule:{schedule['search_workspace_id']}:{schedule['next_run_at']}",
                            now=ctx.clock())
                conn.commit()
                return
            search_schedules.run_scheduled(conn, settings=ctx.settings, runner=runner_factory(ctx.settings),
                                           search_workspace_id=search_workspace_id, now=ctx.clock())
        finally:
            conn.close()
    return handle


def _policy(settings: Any) -> Any:
    from product.retention_policy import load_retention_policy
    from webapp.app import _project_path
    return load_retention_policy(_project_path(settings.retention_policy_path))


def _account_purge(ctx: JobContext, payload: dict) -> None:
    """§20.4. With an account id: purge it (revalidated; idempotent). The
    periodic run (no payload) expires retained records past their period."""
    from webapp.storage.object_store import object_store_from_settings
    if payload.get("account_id"):
        from webapp.services.purge import purge_account
        report = purge_account(ctx.connect, account_id=payload["account_id"],
                               object_store=object_store_from_settings(ctx.settings), now=ctx.clock(),
                               settings=ctx.settings)
        if report.status == "not_due":
            raise RetryLater("the cooling-off period has not passed")
        return
    from webapp.services.purge import expire_retained
    conn = ctx.connect()
    try:
        expire_retained(conn, policy=_policy(ctx.settings), now=ctx.clock())
    finally:
        conn.close()


def _account_export(ctx: JobContext, payload: dict) -> None:
    from webapp.services.export import build_export
    from webapp.storage.object_store import object_store_from_settings
    conn = ctx.connect()
    try:
        build_export(conn, export_id=payload["export_id"], object_store=object_store_from_settings(ctx.settings),
                     now=ctx.clock(), settings=ctx.settings, policy=_policy(ctx.settings))
    finally:
        conn.close()


def _cli_discovery_runner(settings: Any) -> Any:
    from pathlib import Path
    from product.discovery_search import CliDiscoveryPortalRunner
    return CliDiscoveryPortalRunner(Path(settings.profile_root).resolve())


def _billing_webhooks(settings: Any) -> Any:
    from product.entitlements import load_catalog
    from webapp.billing.registry import provider_for
    from webapp.services.billing_webhooks import BillingWebhooks
    from webapp.app import _project_path
    catalog = load_catalog(_project_path(settings.plan_catalog_path))
    provider = provider_for(settings, catalog)
    return None if provider is None else BillingWebhooks(provider, catalog_version=catalog.catalog_version,
                                                              settings=settings)


def default_handlers(settings: Any, *, providers_factory: Callable[[], Any] | None = None,
                     billing_webhooks: Any = None, email_provider: Any = None,
                     discovery_runner: Callable[[Any], Any] | None = None) -> dict[str, Handler]:
    if providers_factory is None:
        from webapp.services.autonomy_providers import default_providers
        providers_factory = default_providers
    handlers: dict[str, Handler] = {
        "usage.sweep": _usage_sweep,
        "tokens.sweep": _tokens_sweep,
        "autonomy.tick": _autonomy_tick_handler(providers_factory),
        "outbox.dispatch": _outbox_dispatch_handler(settings, email_provider),
        "email.webhook.process": _email_webhook_handler,
        "notify.digest": _notify_digest,
        "notify.approval_expiry_scan": _approval_expiry_scan,
        "discovery.scheduled_run": _scheduled_discovery_handler(discovery_runner or _cli_discovery_runner),
        "account.purge": _account_purge,
        "account.export": _account_export,
    }
    webhooks = billing_webhooks if billing_webhooks is not None else _billing_webhooks(settings)
    if webhooks is not None:
        handlers["billing.webhook.process"] = _billing_webhook_handler(webhooks)
    return handlers


__all__ = ["default_handlers", "sweep_tokens", "ts"]
