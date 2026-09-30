"""Bundle 7 deployment modes (spec H1). Validation is fail-closed: a hosted
deployment with any missing or unsafe setting refuses to start, and local
mode refuses to listen on anything but a loopback host."""
from __future__ import annotations

from urllib.parse import urlsplit

from webapp.config import Settings

LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})
DEV_PLAN_CATALOG_NAME = "plan-catalog.dev.json"
DEV_AI_PRICING_NAME = "ai-pricing.dev.json"
MIN_SECRET_KEY_LENGTH = 43  # 32 random bytes, base64url


class DeploymentConfigError(RuntimeError):
    def __init__(self, problems: list[str]) -> None:
        super().__init__("; ".join(problems))
        self.problems = problems


def _origin_is_https_without_path(origin: str | None) -> bool:
    if not origin:
        return False
    parts = urlsplit(origin)
    return (parts.scheme == "https" and bool(parts.netloc) and parts.path in ("", "/")
            and not parts.query and not parts.fragment)


def validate_settings(settings: Settings) -> list[str]:
    if settings.deployment not in ("local", "hosted"):
        return ["JOBSEARCH_DEPLOYMENT must be 'local' or 'hosted'"]
    if not settings.is_hosted:
        return [] if settings.host in LOOPBACK_HOSTS else ["local mode requires a loopback host"]
    problems: list[str] = []
    if not (settings.database_url or "").startswith("postgresql://"):
        problems.append("hosted mode requires a postgresql:// JOBSEARCH_DATABASE_URL")
    if len(settings.secret_key or "") < MIN_SECRET_KEY_LENGTH:
        problems.append(f"hosted mode requires JOBSEARCH_SECRET_KEY of at least {MIN_SECRET_KEY_LENGTH} characters")
    if not _origin_is_https_without_path(settings.public_origin):
        problems.append("hosted mode requires an https JOBSEARCH_PUBLIC_ORIGIN without a path")
    if not settings.extension_ids:
        problems.append("hosted mode requires JOBSEARCH_EXTENSION_IDS")
    if settings.object_store.get("kind") != "s3":
        problems.append("hosted mode requires an s3 JOBSEARCH_OBJECT_STORE")
    if settings.email_provider == "console":
        problems.append("hosted mode requires a real JOBSEARCH_EMAIL_PROVIDER")
    if settings.plan_catalog_path.name == DEV_PLAN_CATALOG_NAME:
        problems.append("hosted mode refuses the development plan catalog")
    if settings.ai_pricing_path.name == DEV_AI_PRICING_NAME:
        problems.append("hosted mode refuses the development AI pricing table")
    return problems


def require_valid_settings(settings: Settings) -> None:
    problems = validate_settings(settings)
    if problems:
        raise DeploymentConfigError(problems)
