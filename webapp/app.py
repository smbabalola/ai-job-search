from __future__ import annotations

import logging
import os

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from webapp.api.applications import router as applications_router
from webapp.api.auth import AuthContextMiddleware, router as auth_router
from webapp.api.extension_auth import router as extension_auth_router
from webapp.api.review_pages import router as review_pages_router
from webapp.api.review_approval import router as review_approval_router
from webapp.api.profile import router as profile_router
from webapp.api.autonomy import router as autonomy_router
from webapp.api.application_documents import router as application_documents_router, reusable_router
from webapp.api.cv_generation_v2 import router as cv_generation_v2_router
from webapp.api.discovery import router as discovery_router
from webapp.api.handoff import router as handoff_router
from webapp.api.fill_app import router as fill_app_router
from webapp.api.fill_extension import router as fill_extension_router
from webapp.api.submit_app import router as submit_app_router
from webapp.api.submit_extension import router as submit_extension_router
from webapp.api.onboarding import router as onboarding_router
from webapp.api.review import router as review_router
from webapp.api.search_workspaces import router as search_workspaces_router
from webapp.api.status import router as status_router
from webapp.api.user_profile import router as user_profile_router
from webapp.api.workspaces import router as workspaces_router
from webapp.api.views import router as views_router
from webapp.config import Settings
from webapp.deployment import require_valid_settings
from webapp.observability import LogErrorReporter, RequestContextMiddleware, configure_logging, unhandled_error_handler
from webapp.security_middleware import SecurityHeadersMiddleware
from webapp.api.errors import csrf_failed_handler, rate_limited_handler, scope_refused_handler
from webapp.api.route_classes import PUBLIC, ScopeRefused
from webapp.services.csrf import CsrfFailed, require_csrf
from webapp.services.rate_limit import RateLimited
from webapp.persistence.db import init_db
from product.onboarding_walkthroughs import register_default_walkthroughs


def _start_autonomy_driver(app: FastAPI, settings: Settings):
    """Bundle 6C in-app driver: runs only while JOBSEARCH_AUTONOMY_SCHEDULER
    is on. It shares the engine with the CLI worker and never overlaps its own
    ticks; leases make both drivers safe together."""
    import random
    import threading
    from datetime import datetime, timezone

    app.state.autonomy_driver = {"running": False}
    # Hosted mode runs the 6C driver only inside the worker process (spec O1).
    if settings.is_hosted or not settings.autonomy_scheduler_enabled:
        return None, None
    from webapp.services.autonomy_providers import providers_from_app_state
    from webapp.services.autonomy_scheduler import run_driver
    try:
        providers = providers_from_app_state(app.state)
    except Exception:  # e.g. production providers without credentials: no driver
        logging.getLogger(__name__).exception("autonomy driver not started")
        return None, None
    stop = threading.Event()
    app.state.autonomy_driver["running"] = True
    thread = threading.Thread(
        target=run_driver, args=(settings, providers), daemon=True, name="autonomy-driver",
        kwargs={"stop": stop, "clock": lambda: datetime.now(timezone.utc), "rng": random.Random(),
                "worker_id": f"app-{os.getpid()}", "status": app.state.autonomy_driver})
    thread.start()
    return stop, thread


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()
    require_valid_settings(settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        register_default_walkthroughs()
        init_db(settings.db_path)
        stop, thread = _start_autonomy_driver(app, settings)
        try:
            yield
        finally:
            if thread is not None:
                stop.set()
                thread.join(timeout=settings.autonomy_step_timeout + 5)

    app = FastAPI(title="Job Application Workspace", lifespan=lifespan, dependencies=[Depends(require_csrf)])
    app.state.settings = settings
    # Bundle 7 spec §20.6/§20.7: request ids, JSON logs, safe 500s, strict CSP.
    configure_logging()
    app.state.error_reporter = LogErrorReporter()
    app.add_exception_handler(Exception, unhandled_error_handler)
    app.add_exception_handler(CsrfFailed, csrf_failed_handler)
    app.add_exception_handler(RateLimited, rate_limited_handler)
    app.add_exception_handler(ScopeRefused, scope_refused_handler)
    app.add_middleware(AuthContextMiddleware, settings=settings)
    app.add_middleware(SecurityHeadersMiddleware, hosted=settings.is_hosted)
    app.add_middleware(RequestContextMiddleware)  # outermost: every response carries the request id
    app.state.templates = Jinja2Templates(directory=str(Path(__file__).with_name("templates")))
    app.mount("/static", StaticFiles(directory=str(Path(__file__).with_name("static"))), name="static")
    if settings.handoff_fixtures_dir is not None:
        app.mount(
            "/test-fixtures/handoff",
            StaticFiles(directory=str(settings.handoff_fixtures_dir)),
            name="handoff_fixtures",
        )

    @app.get("/health", dependencies=[Depends(PUBLIC)])
    def health() -> dict[str, str]:
        return {"status": "ok"}

    app.include_router(auth_router)
    app.include_router(extension_auth_router)
    app.include_router(profile_router)
    app.include_router(application_documents_router)
    app.include_router(reusable_router)
    app.include_router(cv_generation_v2_router)
    app.include_router(discovery_router)
    app.include_router(handoff_router)
    app.include_router(onboarding_router)
    app.include_router(user_profile_router)
    app.include_router(search_workspaces_router)
    app.include_router(workspaces_router)
    app.include_router(review_router)
    app.include_router(status_router)
    app.include_router(autonomy_router)
    app.include_router(review_approval_router)
    app.include_router(fill_extension_router)
    app.include_router(fill_app_router)
    app.include_router(submit_extension_router)
    app.include_router(submit_app_router)
    app.include_router(applications_router)
    app.include_router(review_pages_router)
    app.include_router(views_router)

    return app
