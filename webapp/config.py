from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from product.autonomy_contract import Capability


@dataclass
class Settings:
    db_path: Path = field(default_factory=lambda: Path(".jobsearch/jobsearch.sqlite3"))
    host: str = "127.0.0.1"
    port: int = 8420
    openai_api_key: str | None = field(default_factory=lambda: os.environ.get("OPENAI_API_KEY"))
    profile_root: str = "."
    extensions_dir: Path = field(default_factory=lambda: Path("extensions"))
    documents_root: Path = field(default_factory=lambda: Path("documents"))
    account_id: str | None = None
    handoff_fixtures_dir: Path | None = None
    # CV Quality v2's candidate-facing CV is not yet presentable, so its
    # HTTP/UI entry points stay off unless explicitly enabled.
    cv_quality_v2_enabled: bool = field(
        default_factory=lambda: os.environ.get("JOBSEARCH_ENABLE_CV_QUALITY_V2") == "1"
    )
    # Bundle 6B deployment ceiling (spec §14.1). Operator settings can only
    # LOWER autonomy; they never grant anything beyond the user's explicit
    # authorization. Everything is off by default.
    autonomy_max_capability: str = field(
        default_factory=lambda: os.environ.get("JOBSEARCH_AUTONOMY_MAX_CAPABILITY", "NONE")
    )
    autonomy_submit_capable_adapters: tuple[str, ...] = field(
        default_factory=lambda: tuple(
            a.strip() for a in os.environ.get("JOBSEARCH_AUTONOMY_SUBMIT_ADAPTERS", "").split(",") if a.strip()
        )
    )
    autonomy_live_submit_daily_cap: int = 1
    autonomy_shadow_enabled: bool = field(
        default_factory=lambda: os.environ.get("JOBSEARCH_AUTONOMY_SHADOW") == "1"
    )

    def __post_init__(self) -> None:
        self.db_path = Path(self.db_path)
        self.extensions_dir = Path(self.extensions_dir)
        self.documents_root = Path(self.documents_root)
        if self.handoff_fixtures_dir is not None:
            self.handoff_fixtures_dir = Path(self.handoff_fixtures_dir)

    def autonomy_deployment_ceiling(self) -> Capability:
        try:
            return Capability[self.autonomy_max_capability]
        except KeyError:
            return Capability.NONE  # fail closed on a mistyped setting

    @property
    def autonomy_sentinel_path(self) -> Path:
        return self.db_path.parent / "AUTONOMY_HALT"
