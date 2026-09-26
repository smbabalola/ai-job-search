"""CLI driver for the Bundle 6C scheduler:

    python -m webapp.autonomy_worker --once | --loop [--db PATH]

Obeys JOBSEARCH_AUTONOMY_SCHEDULER exactly like the in-app driver; with the
gate off it does nothing and exits 0. Works whether or not the web app runs."""
from __future__ import annotations

import argparse
import os
import random
import threading
from datetime import datetime, timezone
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    from webapp.config import Settings
    from webapp.persistence.db import connect, init_db
    parser = argparse.ArgumentParser(prog="webapp.autonomy_worker")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--once", action="store_true")
    mode.add_argument("--loop", action="store_true")
    parser.add_argument("--db", type=Path, default=None)
    args = parser.parse_args(argv)
    settings = Settings(db_path=args.db) if args.db else Settings()
    if not settings.autonomy_scheduler_enabled:
        print("autonomy scheduler is disabled (set JOBSEARCH_AUTONOMY_SCHEDULER=1)")
        return 0
    from webapp.services.autonomy_providers import default_providers
    from webapp.services.autonomy_scheduler import run_driver, run_tick
    init_db(settings.db_path)
    providers = default_providers()
    worker_id = f"cli-{os.getpid()}"
    clock = lambda: datetime.now(timezone.utc)  # noqa: E731
    if args.once:
        conn = connect(settings.db_path)
        try:
            report = run_tick(conn, settings=settings, providers=providers, now=clock(), rng=random.Random(),
                              worker_id=worker_id, clock=clock)
        finally:
            conn.close()
        print(f"processed {len(report.processed)} item(s)")
        return 0
    stop = threading.Event()
    try:
        run_driver(settings, providers, stop=stop, clock=clock, rng=random.Random(), worker_id=worker_id)
    except KeyboardInterrupt:
        stop.set()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
