from __future__ import annotations

import logging
import os

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from webapp.api.profile import router as profile_router
from webapp.api.autonomy import router as autonomy_router
from webapp.api.application_documents import router as application_documents_router, reusable_router
from webapp.api.cv_generation_v2 import router as cv_generation_v2_router
from webapp.api.discovery import router as discovery_router
from webapp.api.handoff import router as handoff_router
from webapp.api.onboarding import router as onboarding_router
from webapp.api.review import router as review_router
from webapp.api.search_workspaces import router as search_workspaces_router
from webapp.api.status import router as status_router
from webapp.api.user_profile import router as user_profile_router
from webapp.api.workspaces import router as workspaces_router
from webapp.api.views import router as views_router
from webapp.config import Settings
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
    if not settings.autonomy_scheduler_enabled:
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

    app = FastAPI(title="Job Application Workspace", lifespan=lifespan)
    app.state.settings = settings
    app.state.templates = Jinja2Templates(directory=str(Path(__file__).with_name("templates")))
    app.mount("/static", StaticFiles(directory=str(Path(__file__).with_name("static"))), name="static")
    if settings.handoff_fixtures_dir is not None:
        app.mount(
            "/test-fixtures/handoff",
            StaticFiles(directory=str(settings.handoff_fixtures_dir)),
            name="handoff_fixtures",
        )

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

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
    app.include_router(views_router)

    return app
