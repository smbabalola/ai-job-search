# tests/webapp/conftest.py
from pathlib import Path

import pytest

FIXTURE_PROFILE_ROOT = Path(__file__).parent / "fixtures" / "webapp_profile_root"


@pytest.fixture
def webapp_profile_root():
    return FIXTURE_PROFILE_ROOT


@pytest.fixture
def journey_server(tmp_path, request):
    """Bundle 7 spec §25.4: the release-journey deployment in one process -- the
    real webapp on 127.0.0.1:8420 (the extension test-hook build's origin) with
    real sign-in, the fake billing provider, the console email provider, the
    deterministic AI providers of tests/webapp/fixtures/journey, the loopback
    fixture ATS origin enabled, and the real worker loop (outbox dispatch,
    webhooks, sweeps) in a thread. ``--db postgres`` puts it on PostgreSQL
    (the redirect maps db_path); otherwise SQLite, with a visible note."""
    import socket
    import threading
    import time
    from datetime import datetime, timezone
    from types import SimpleNamespace

    import uvicorn

    from tests.webapp.auth_helpers import publish_legal_documents
    from tests.webapp.fixtures.journey import install_fake_providers
    from webapp.app import create_app
    from webapp.config import Settings
    from webapp.services.autonomy_providers import ProviderSet
    from webapp.worker.handlers import default_handlers
    from webapp.worker.runner import Worker

    if request.config.getoption("--db") != "postgres":
        print("\nNOTE: release journey on SQLite (run with --db postgres for the hosted database)")
    settings = Settings(db_path=tmp_path / "jobsearch.sqlite3", documents_root=tmp_path / "documents",
                        host="127.0.0.1", port=8420, auth_required_in_local=True, cv_quality_v2_enabled=True,
                        autonomy_max_capability="FILL", human_submit_enabled=True,
                        submit_fixture_origins_enabled=True)
    app = create_app(settings)
    install_fake_providers(app.state)
    providers = ProviderSet(app.state.job_understanding_provider, app.state.semantic_adapter,
                            app.state.application_intelligence_provider)
    worker = Worker(settings, default_handlers(settings, providers_factory=lambda: providers,
                                               email_provider=app.state.email_provider),
                    clock=lambda: datetime.now(timezone.utc), worker_id="journey-worker")
    stop = threading.Event()
    worker_thread = threading.Thread(target=worker.run_forever, args=(stop,), kwargs={"poll_seconds": 0.5},
                                     daemon=True)
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=8420, log_level="warning", access_log=False))
    server_thread = threading.Thread(target=server.run, daemon=True)
    server_thread.start()
    deadline = time.monotonic() + 15
    while True:
        try:
            with socket.create_connection(("127.0.0.1", 8420), timeout=0.25):
                break
        except OSError:
            if time.monotonic() > deadline:
                raise RuntimeError("the journey webapp did not start on 127.0.0.1:8420")
            time.sleep(0.05)
    publish_legal_documents(settings)  # after startup: the app's lifespan migrates the database
    worker_thread.start()
    try:
        yield SimpleNamespace(app=app, settings=settings, origin="http://127.0.0.1:8420",
                              mailbox=app.state.email_provider.sent)
    finally:
        stop.set()
        server.should_exit = True
        server_thread.join(timeout=10)
        worker_thread.join(timeout=10)
