"""Provider webhooks (Bundle 7 spec §12.1, §21.2): verified, stored once, then
processed. Processing runs right after the commit until the worker (Task 18)
takes over; a failure there leaves the event pending for retry, never lost."""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request

from webapp.api.errors import error_response
from webapp.api.route_classes import WEBHOOK
from webapp.persistence import dbapi
from webapp.api.dependencies import get_conn
from webapp.services.billing_webhooks import WebhookRejected

log = logging.getLogger("webapp.billing")
router = APIRouter()


@router.post("/webhooks/billing/{provider_name}", dependencies=[Depends(WEBHOOK)])
async def billing_webhook(provider_name: str, request: Request, conn: dbapi.Connection = Depends(get_conn)):
    webhooks = request.app.state.billing_webhooks
    if webhooks is None or webhooks.provider.name != provider_name:
        raise HTTPException(404)
    body = await request.body()
    now = datetime.now(timezone.utc)
    try:
        webhooks.ingest(conn, headers=dict(request.headers), body=body, now=now)
    except WebhookRejected:
        conn.commit()
        return error_response("WEBHOOK_SIGNATURE_INVALID", "The webhook signature did not verify.", 400)
    conn.commit()
    try:
        webhooks.process_pending(conn, now=now)
        conn.commit()
    except Exception:  # noqa: BLE001 - the stored event is retried later
        conn.rollback()
        log.exception("billing_webhook_processing_deferred")
    return {"received": True}
