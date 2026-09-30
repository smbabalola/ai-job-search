"""Email-provider webhooks (Bundle 7 spec §18.1): a generic endpoint. The
configured adapter verifies the request and normalizes it into events
(``verify_webhook``); each event is stored once and bounces/complaints
become suppressions. Adapters without webhooks (console, SMTP) have none."""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request

from webapp.api.dependencies import get_conn
from webapp.api.errors import error_response
from webapp.api.route_classes import WEBHOOK
from webapp.comms.outbox import record_email_event
from webapp.persistence import dbapi

log = logging.getLogger("webapp.comms")
router = APIRouter()


@router.post("/webhooks/email/{provider_name}", dependencies=[Depends(WEBHOOK)])
async def email_webhook(provider_name: str, request: Request, conn: dbapi.Connection = Depends(get_conn)):
    provider = getattr(request.app.state, "email_provider", None)
    verify = getattr(provider, "verify_webhook", None)
    if provider is None or verify is None or provider.name != provider_name:
        raise HTTPException(404)
    body = await request.body()
    now = datetime.now(timezone.utc)
    try:
        events = verify(headers=dict(request.headers), body=body, now=now)
    except Exception:  # noqa: BLE001 - any verification failure is a refusal, never a 500
        log.warning("email_webhook_rejected provider=%s", provider_name)
        return error_response("WEBHOOK_SIGNATURE_INVALID", "The webhook signature did not verify.", 400)
    for event in events:
        record_email_event(conn, provider=provider_name, provider_event_id=event.provider_event_id, kind=event.kind,
                           address=event.address, payload={"address": event.address}, now=now)
    conn.commit()
    return {"received": len(events)}
