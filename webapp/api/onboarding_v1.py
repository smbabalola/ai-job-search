"""Guided onboarding and CV import routes (Bundle 7 spec §15.2-§15.3). Each step
writes through the real services (profile answers, the CV library, user
profile, job families and CV strategy, rules, pairing) and then marks itself
DONE; every step except "about" can be skipped and resumed later."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, ConfigDict

from webapp.api.dependencies import get_account_scope, get_conn, get_documents_root
from webapp.api.route_classes import USER
from webapp.persistence import dbapi
from webapp.services import onboarding_v1 as ob
from webapp.services.ownership import AccountScope

router = APIRouter(dependencies=[Depends(USER)])

STEP_TITLES = {"about": "About you", "cv": "Your CV", "import": "Import from your CV",
               "eligibility": "Work eligibility", "preferences": "What you're looking for",
               "families": "Job families and CVs", "rules": "Rules", "extension": "Browser extension"}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _next(state: dict[str, Any], after: str | None = None) -> str:
    steps = list(ob.ONBOARDING_STEPS)
    start = steps.index(after) + 1 if after in steps else 0
    for step in steps[start:]:
        if state["steps"][step] == "TODO":
            return f"/onboarding/{step}"
    return "/"


def _done(conn: dbapi.Connection, scope: AccountScope, step: str) -> RedirectResponse:
    state = ob.mark_step(conn, scope, step, "DONE", now=_now())
    conn.commit()
    return RedirectResponse(_next(state, step), status_code=303)


@router.get("/api/onboarding")
def get_onboarding(conn: dbapi.Connection = Depends(get_conn),
                   scope: AccountScope = Depends(get_account_scope)) -> dict[str, Any]:
    ready = ob.readiness(conn, scope, now=_now())
    return {"state": ob.onboarding_state(conn, scope),
            "readiness": {"prepare_ok": ready.prepare_ok, "prepare_missing": ready.prepare_missing,
                          "fill_ok": ready.fill_ok, "fill_missing": ready.fill_missing}}


@router.get("/onboarding")
def onboarding_start(conn: dbapi.Connection = Depends(get_conn), scope: AccountScope = Depends(get_account_scope)):
    return RedirectResponse(_next(ob.onboarding_state(conn, scope)), status_code=303)


def _search_workspace(conn: dbapi.Connection, scope: AccountScope) -> dict[str, Any] | None:
    from webapp.persistence.search_workspaces import list_search_workspaces
    return next((w for w in list_search_workspaces(conn, account_id=scope.account_id) if w["status"] == "active"), None)


def _context(conn: dbapi.Connection, scope: AccountScope, step: str) -> dict[str, Any]:
    from webapp.services import cv_library
    context: dict[str, Any] = {"step": step, "title": STEP_TITLES[step], "steps": STEP_TITLES,
                               "state": ob.onboarding_state(conn, scope), "optional": step in ob.OPTIONAL_STEPS,
                               "skippable": step != "about"}
    if step in ("cv", "import", "families"):
        context["items"] = [i for i in cv_library.list_items(conn, account_id=scope.account_id)
                            if i["status"] == "ACTIVE"]
    if step == "import":
        context["runs"] = [dict(r) for r in conn.execute(
            "SELECT * FROM profile_import_runs WHERE account_id = ? ORDER BY created_at DESC LIMIT 1",
            (scope.account_id,))]
        if context["runs"]:
            from webapp.services.cv_import import list_proposals
            context["proposals"] = list_proposals(conn, scope, run_id=context["runs"][0]["id"])
    if step in ("preferences", "families"):
        from webapp.persistence.user_profile import get_current_user_profile
        sw = _search_workspace(conn, scope)
        current = get_current_user_profile(conn, sw["id"], account_id=scope.account_id) if sw else None
        context["profile"] = (current or {}).get("payload") or {}
    if step == "extension":
        context["paired"] = ob.readiness(conn, scope, now=_now()).fill_ok
    return context


@router.get("/onboarding/{step}")
def onboarding_step(step: str, request: Request, conn: dbapi.Connection = Depends(get_conn),
                    scope: AccountScope = Depends(get_account_scope)):
    if step not in ob.ONBOARDING_STEPS:
        raise HTTPException(status_code=404, detail="unknown onboarding step")
    return request.app.state.templates.TemplateResponse(request, f"onboarding/step_{step}.html",
                                                        _context(conn, scope, step))


@router.post("/onboarding/{step}/skip")
def skip_step(step: str, conn: dbapi.Connection = Depends(get_conn), scope: AccountScope = Depends(get_account_scope)):
    try:
        state = ob.mark_step(conn, scope, step, "SKIPPED", now=_now())
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    conn.commit()
    return RedirectResponse(_next(state, step), status_code=303)


def _answer(conn: dbapi.Connection, scope: AccountScope, subject: str, value: Any,
            context: dict[str, Any] | None = None) -> None:
    from product.autonomy_contract import Reach
    from webapp.persistence.autonomy_answers import approve_answer
    approve_answer(conn, account_id=scope.account_id, subject=subject, value=value, reach=Reach.ACCOUNT, scope_id=None,
                   context=context or {}, basis={"kind": "USER_ASSERTION"},
                   approved_by=scope.user_id or scope.account_id, now=_now(), commit=False)


def _set_identity(conn: dbapi.Connection, scope: AccountScope, label: str, value: str) -> None:
    """The contact detail as an Identity line of the candidate profile, where the
    evidence snapshot (and so the review's contact fields and the fill) read it.
    Updates the line with the same label rather than adding a second one."""
    from webapp.services.profile_manager import create_profile_entry, get_profile_manager, update_profile_entry
    root = scope.profile_sources(conn)
    manager = get_profile_manager(conn, root=root, account_id=scope.account_id)
    existing = next((e for e in manager["entries"] if e["kind"] == "identity"
                     and str(e["fields"].get("label", "")).strip().lower() == label.lower()), None)
    fields = {"label": label, "value": value}
    if existing is None:
        create_profile_entry(conn, root=root, expected_revision=manager["revision"], kind="identity", fields=fields,
                             account_id=scope.account_id)
    elif existing["fields"].get("value") != value:
        update_profile_entry(conn, root=root, expected_revision=manager["revision"], entry_id=existing["entry_id"],
                             kind="identity", fields=fields, account_id=scope.account_id)


@router.post("/onboarding/about")
def save_about(display_name: str = Form(...), contact_email: str = Form(""), phone: str = Form(""),
               city: str = Form(""), country: str = Form(""), conn: dbapi.Connection = Depends(get_conn),
               scope: AccountScope = Depends(get_account_scope)):
    name = " ".join(display_name.split())
    if not name or len(name) > 120:
        raise HTTPException(status_code=400, detail="Enter your name.")
    if scope.user_id:
        conn.execute("UPDATE users SET display_name = ? WHERE id = ?", (name, scope.user_id))
        email = contact_email.strip() or conn.execute("SELECT email_normalized FROM users WHERE id = ?",
                                                      (scope.user_id,)).fetchone()[0]
        _answer(conn, scope, "contact.email", email)
        _set_identity(conn, scope, "Email", email)
    if phone.strip():
        _answer(conn, scope, "contact.phone", " ".join(phone.split()))
        _set_identity(conn, scope, "Phone", " ".join(phone.split()))
    if city.strip() or country.strip():
        _answer(conn, scope, "location.current", {"city": " ".join(city.split()), "country": country.strip().upper()})
    return _done(conn, scope, "about")


@router.post("/onboarding/cv")
def save_cv(request: Request, title: str = Form("My CV"), file: UploadFile = File(...),
            conn: dbapi.Connection = Depends(get_conn), documents_root=Depends(get_documents_root),
            scope: AccountScope = Depends(get_account_scope)):
    from webapp.services import cv_library, cv_strategy
    content = file.file.read(cv_library.MAX_UPLOAD_BYTES + 1)
    if len(content) > cv_library.MAX_UPLOAD_BYTES:
        raise cv_library.DocumentRejected("TOO_LARGE")
    metering = request.app.state.metering
    metering.gauge_check(conn, scope, "library.cv_items", adding=1)
    metering.gauge_check(conn, scope, "storage.bytes", adding=len(content))
    cv_library.validate_upload(content, file.filename or "")
    item = cv_library.create_item(conn, scope, title=title or "My CV", now=_now())
    cv_library.add_version(conn, scope, item_id=item["id"], content=content, filename=file.filename or "",
                           media_type_hint=file.content_type, documents_root=documents_root, now=_now())
    strategy, _ = cv_strategy.current_cv_strategy(conn, scope.account_id)
    if strategy["default"].get("mode") == "LATEST_VERSION" and strategy["default"].get("item_id") is None:
        cv_strategy.save_cv_strategy(conn, scope, {"default": {"mode": "LATEST_VERSION", "item_id": item["id"]},
                                                   "by_family": strategy["by_family"]}, now=_now())
    return _done(conn, scope, "cv")


def _extraction_provider(request: Request, scope: AccountScope, document_version_id: str):
    from webapp.services.metered_provider import metered
    provider = getattr(request.app.state, "cv_extraction_provider", None)
    if provider is None:
        from product.openai_cv_extraction_provider import OpenAICvExtractionProvider
        provider = OpenAICvExtractionProvider()
    return metered(provider, scope, "document_version", document_version_id, state=request.app.state,
                   feature="profile.cv_import")


@router.post("/onboarding/import")
def start_import(request: Request, item_id: str = Form(...), conn: dbapi.Connection = Depends(get_conn),
                 documents_root=Depends(get_documents_root), scope: AccountScope = Depends(get_account_scope)):
    from webapp.services import cv_import, cv_library
    version = cv_library.latest_visible_version(conn, account_id=scope.account_id, item_id=item_id)
    if version is None:
        raise HTTPException(status_code=404, detail="CV not found")
    cv_import.import_cv(conn, scope, document_version_id=version["document_version_id"],
                        metering=request.app.state.metering,
                        provider=_extraction_provider(request, scope, version["document_version_id"]),
                        documents_root=documents_root, now=_now())
    return RedirectResponse("/onboarding/import", status_code=303)


@router.post("/onboarding/import/resolve")
async def resolve_import(request: Request, conn: dbapi.Connection = Depends(get_conn),
                         scope: AccountScope = Depends(get_account_scope)):
    from webapp.services import cv_import
    form = await request.form()
    items = []
    for key in form.keys():
        if not key.startswith("resolution_"):
            continue
        proposal_id = key[len("resolution_"):]
        resolution = str(form.get(key))
        if resolution not in cv_import.RESOLUTIONS:
            continue
        fields = None
        if resolution == "EDITED_ACCEPTED":
            prefix = f"field_{proposal_id}_"
            fields = {k[len(prefix):]: str(v) for k, v in form.items() if k.startswith(prefix)}
        items.append((proposal_id, resolution, fields))
    if items:
        try:
            cv_import.resolve_batch(conn, scope, items=items, now=_now())
        except cv_import.ImportError_ as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
    return _done(conn, scope, "import")


@router.post("/onboarding/eligibility")
def save_eligibility(country: str = Form(""), right_to_work: str = Form(""), sponsorship: str = Form(""),
                     notice_period: str = Form(""), start_date: str = Form(""),
                     conn: dbapi.Connection = Depends(get_conn), scope: AccountScope = Depends(get_account_scope)):
    code = country.strip().upper()
    if code and right_to_work in ("yes", "no"):
        _answer(conn, scope, "work_authorization.right_to_work", right_to_work, {"country": code})
    if code and sponsorship in ("yes", "no"):
        _answer(conn, scope, "work_authorization.sponsorship_required", sponsorship, {"country": code})
    if notice_period.strip():
        _answer(conn, scope, "employment.notice_period", " ".join(notice_period.split()))
    if start_date.strip():
        _answer(conn, scope, "employment.availability_start", start_date.strip())
    return _done(conn, scope, "eligibility")


@router.post("/onboarding/preferences")
def save_preferences(target_roles: str = Form(""), locations: str = Form(""), remote_preference: str = Form(
        "no_preference"), conn: dbapi.Connection = Depends(get_conn), scope: AccountScope = Depends(get_account_scope)):
    from product.user_profile import UserProfileValidationError
    from webapp.persistence.user_profile import get_current_user_profile, save_user_profile
    sw = _search_workspace(conn, scope)
    if sw is None:
        raise HTTPException(status_code=409, detail="no active search")
    current = get_current_user_profile(conn, sw["id"], account_id=scope.account_id)
    payload = dict((current or {}).get("payload") or {})
    payload.pop("schema_version", None)
    payload.update({"target_roles": [r.strip() for r in target_roles.split(",") if r.strip()],
                    "locations": [loc.strip() for loc in locations.split(",") if loc.strip()],
                    "remote_preference": remote_preference})
    try:
        save_user_profile(conn, payload, search_workspace_id=sw["id"],
                          expected_revision=current["profile_revision"] if current else 0, account_id=scope.account_id)
    except (UserProfileValidationError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _done(conn, scope, "preferences")


@router.post("/onboarding/families")
async def save_families(request: Request, conn: dbapi.Connection = Depends(get_conn),
                        scope: AccountScope = Depends(get_account_scope)):
    """A suggested family per target role, each mapped to a CV (the /cvs/strategy form fields)."""
    from webapp.api.cv_strategy import save_strategy_page
    await save_strategy_page(request, conn=conn, scope=scope)
    return _done(conn, scope, "families")


@router.post("/onboarding/{step}/done")
def finish_step(step: str, conn: dbapi.Connection = Depends(get_conn), scope: AccountScope = Depends(get_account_scope)):
    """Steps whose writes happen on another page (rules, extension pairing)."""
    if step not in ("rules", "extension"):
        raise HTTPException(status_code=404, detail="unknown step")
    return _done(conn, scope, step)
