"""Spec §10.7 allowlist: the only code paths that take the transitional
PostgreSQL writer lock (a ``BEGIN IMMEDIATE`` literal). Module -> count.

Adding a site requires changing this mapping in the same commit with a
comment naming the single-writer invariant it protects. Bundle 7 code must
use per-account locks, row locks or constraints instead.
"""
from __future__ import annotations

WRITER_LOCK_SITES: dict[str, int] = {
    "webapp/persistence/migrations.py": 1,
    "webapp/persistence/workflow.py": 1,
    "webapp/services/application_documents.py": 4,
    "webapp/services/application_pack.py": 2,
    "webapp/services/autonomy_controls.py": 1,  # run_immediate: every 6B/6C gate transaction goes through it
    "webapp/services/cv_generation_basis.py": 1,
    "webapp/services/cv_generation_v2.py": 1,
    "webapp/services/discovery.py": 1,
    "webapp/services/http_api.py": 2,
    "webapp/services/pipeline.py": 1,
    "webapp/services/profile_manager.py": 1,
}
