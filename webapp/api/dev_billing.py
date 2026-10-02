"""The fake provider's own pages (Bundle 7 spec §12.2): a checkout with "Pay
succeeds", "Payment fails" and "Cancel", and a minimal portal. They stand in for
a third-party provider's hosted pages and are mounted only in local mode."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from webapp.api.route_classes import PUBLIC

router = APIRouter()

_OUTCOMES = {"pay": "Pay succeeds", "fail": "Payment fails", "cancel": "Cancel"}


def _provider(request: Request):
    provider = request.app.state.billing_service.provider
    if provider is None or provider.name != "fake":
        raise HTTPException(404)
    return provider


@router.get("/dev/billing/checkout/{session_id}", response_class=HTMLResponse, dependencies=[Depends(PUBLIC)])
def checkout_page(session_id: str, request: Request):
    provider = _provider(request)
    session = provider.session(session_id)
    if session is None:
        raise HTTPException(404)
    plan_id, interval = provider.prices.get(session["price"], ("unknown", "month"))
    return request.app.state.templates.TemplateResponse(
        request, "dev_billing_checkout.html",
        {"session_id": session_id, "session": session, "plan_id": plan_id, "interval": interval,
         "outcomes": _OUTCOMES})


@router.post("/dev/billing/checkout/{session_id}/{outcome}", dependencies=[Depends(PUBLIC)])
def complete_checkout(session_id: str, outcome: str, request: Request):
    if outcome not in _OUTCOMES:
        raise HTTPException(404)
    provider = _provider(request)
    if provider.session(session_id) is None:
        raise HTTPException(404)
    return RedirectResponse(provider.simulate(session_id, outcome), status_code=303)


@router.get("/dev/billing/portal/{customer_id}", response_class=HTMLResponse, dependencies=[Depends(PUBLIC)])
def portal_page(customer_id: str, request: Request, return_to: str = "/settings/billing"):
    _provider(request)
    origin = request.app.state.settings.app_origin
    back = return_to if return_to.startswith(origin + "/") else "/settings/billing"  # never an open redirect
    return request.app.state.templates.TemplateResponse(
        request, "dev_billing_checkout.html", {"portal_customer": customer_id, "return_to": back})
