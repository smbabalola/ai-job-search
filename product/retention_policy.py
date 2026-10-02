"""The DP-4 retention and timing policy (Bundle 7 spec §20.4, §20.5):
payment grace, deletion cooling-off, export link lifetime and how long each
retention class is kept after a purge. A ``null`` period keeps the records
indefinitely; the readiness tool lists such classes as unresolved."""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any, Mapping

RETENTION_CLASSES = ("BILLING_FINANCIAL", "SECURITY_AUDIT", "CONSENT_PROOF", "SUPPRESSION")
SCHEMA_VERSION = "retention-policy.v1"


class RetentionPolicyError(ValueError):
    pass


@dataclass(frozen=True)
class RetentionPolicy:
    policy_version: str
    payment_grace: timedelta
    deletion_cooling_off: timedelta
    export_link: timedelta
    retained_periods: Mapping[str, timedelta | None]

    def unresolved_classes(self) -> list[str]:
        return [name for name, period in self.retained_periods.items() if period is None]


def _days(value: Any, name: str, *, minimum: int) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
        raise RetentionPolicyError(f"{name} must be an integer of at least {minimum}")
    return value


def parse_retention_policy(doc: Mapping[str, Any]) -> RetentionPolicy:
    expected = {"schema_version", "policy_version", "payment_grace_days", "deletion_cooling_off_days",
                "export_link_days", "retained_periods_days"}
    if set(doc) != expected:
        raise RetentionPolicyError(f"keys must be exactly {sorted(expected)}")
    if doc["schema_version"] != SCHEMA_VERSION:
        raise RetentionPolicyError(f"schema_version must be {SCHEMA_VERSION}")
    if not isinstance(doc["policy_version"], str) or not doc["policy_version"]:
        raise RetentionPolicyError("policy_version is required")
    periods = doc["retained_periods_days"]
    if not isinstance(periods, dict) or set(periods) != set(RETENTION_CLASSES):
        raise RetentionPolicyError(f"retained_periods_days must name exactly {list(RETENTION_CLASSES)}")
    return RetentionPolicy(
        policy_version=doc["policy_version"],
        payment_grace=timedelta(days=_days(doc["payment_grace_days"], "payment_grace_days", minimum=0)),
        deletion_cooling_off=timedelta(days=_days(doc["deletion_cooling_off_days"], "deletion_cooling_off_days",
                                                  minimum=0)),
        export_link=timedelta(days=_days(doc["export_link_days"], "export_link_days", minimum=1)),
        retained_periods={name: None if periods[name] is None
                          else timedelta(days=_days(periods[name], name, minimum=1)) for name in RETENTION_CLASSES},
    )


def load_retention_policy(path: Path | str) -> RetentionPolicy:
    return parse_retention_policy(json.loads(Path(path).read_text(encoding="utf-8")))
