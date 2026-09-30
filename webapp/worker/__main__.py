"""``python -m webapp.worker`` — run the job worker until interrupted
(``--once`` runs one batch and exits)."""
from __future__ import annotations

import argparse
import os
import socket
import threading
from datetime import datetime, timezone


def main(argv: list[str] | None = None) -> int:
    from webapp.config import Settings
    from webapp.deployment import require_valid_settings
    from webapp.observability import configure_logging
    from webapp.persistence.db import init_db
    from webapp.worker.handlers import default_handlers
    from webapp.worker.runner import Worker
    from webapp.worker.schedule import enqueue_periodic

    parser = argparse.ArgumentParser(prog="webapp.worker")
    parser.add_argument("--once", action="store_true", help="enqueue due periodic jobs, run one batch, exit")
    args = parser.parse_args(argv)
    settings = Settings()
    require_valid_settings(settings)  # hosted mode fails closed exactly like the web process
    configure_logging()
    init_db(settings)
    worker = Worker(settings, default_handlers(settings), clock=lambda: datetime.now(timezone.utc),
                    worker_id=f"{socket.gethostname()}-{os.getpid()}")
    if args.once:
        from webapp.persistence.db import connect
        conn = connect(settings)
        try:
            enqueue_periodic(conn, settings=settings, now=datetime.now(timezone.utc))
            conn.commit()
        finally:
            conn.close()
        print(f"ran {worker.run_once()} job(s)")
        return 0
    stop = threading.Event()
    try:
        worker.run_forever(stop)
    except KeyboardInterrupt:
        stop.set()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
