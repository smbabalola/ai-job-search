"""Versioned email templates (Bundle 7 spec §18.3), rendered in the Jinja
sandbox; HTML bodies autoescape. Every body extends a layout that carries the
account-settings link, the preference link on PRODUCT mail and the
service-message line on SERVICE mail."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from jinja2 import FileSystemLoader, StrictUndefined, select_autoescape
from jinja2.sandbox import SandboxedEnvironment

TEMPLATE_ROOT = Path(__file__).resolve().parents[1] / "templates" / "email"
TEMPLATE_VERSION = "v1"

TEMPLATE_CATEGORIES: dict[str, str] = {
    "auth.verify_email": "SERVICE",
    "auth.password_reset": "SERVICE",
    "auth.password_changed": "SERVICE",
    "auth.email_change_confirm": "SERVICE",
    "auth.email_change_notice": "SERVICE",
    "auth.account_exists": "SERVICE",
    "security.new_device_paired": "SERVICE",
    "security.token_reuse_detected": "SERVICE",
    "billing.payment_failed": "SERVICE",
    "billing.subscription_changed": "SERVICE",
    "billing.subscription_canceled": "SERVICE",
    "account.deletion_requested": "SERVICE",
    "account.deletion_completed": "SERVICE",
    "account.data_export_ready": "SERVICE",
    "account.suspended": "SERVICE",
    "notify.immediate": "PRODUCT",
    "notify.digest": "PRODUCT",
    "usage.limit_near": "PRODUCT",
    "usage.limit_reached": "PRODUCT",
    "announcement.service_notice": "SERVICE",
}
TEMPLATE_IDS = tuple(TEMPLATE_CATEGORIES)


@dataclass(frozen=True)
class Rendered:
    subject: str
    text: str
    html: str
    version: str = TEMPLATE_VERSION


_ENV: SandboxedEnvironment | None = None


def sandbox() -> SandboxedEnvironment:
    global _ENV
    if _ENV is None:
        _ENV = SandboxedEnvironment(loader=FileSystemLoader(str(TEMPLATE_ROOT)),
                                    autoescape=select_autoescape(enabled_extensions=("html",), default=False),
                                    undefined=StrictUndefined, trim_blocks=True, lstrip_blocks=True)
    return _ENV


def render(template_id: str, payload: Mapping[str, Any], *, category: str, app_origin: str,
           version: str = TEMPLATE_VERSION) -> Rendered:
    if template_id not in TEMPLATE_CATEGORIES:
        raise ValueError(f"unknown template {template_id}")
    origin = app_origin.rstrip("/")
    context = {**dict(payload), "category": category, "app_origin": origin,
               "settings_url": f"{origin}/settings", "preferences_url": f"{origin}/settings/communications"}
    env = sandbox()
    base = f"{template_id}/{version}"
    subject = " ".join(env.get_template(f"{base}/subject.txt").render(context).split())
    return Rendered(subject=subject, text=env.get_template(f"{base}/body.txt").render(context).strip() + "\n",
                    html=env.get_template(f"{base}/body.html").render(context), version=version)
