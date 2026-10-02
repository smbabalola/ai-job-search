"""Bundle 7 Task 19 (spec §18.3): every closed template id renders (text and
HTML) from its fixture payload, the HTML autoescapes, and each body carries
the settings link; PRODUCT mail the preference link; SERVICE mail says so."""
from __future__ import annotations

import pytest

from webapp.comms.render import TEMPLATE_CATEGORIES, TEMPLATE_IDS, render

ORIGIN = "https://app.example.test"
INJECTED = "<script>alert(1)</script>"

FIXTURES = {
    "auth.verify_email": {"token": "t", "verify_url": f"{ORIGIN}/verify-email?token=t"},
    "auth.password_reset": {"token": "t", "reset_url": f"{ORIGIN}/reset-password/confirm?token=t"},
    "auth.password_changed": {},
    "auth.email_change_confirm": {"token": "t", "confirm_url": f"{ORIGIN}/email-change/confirm?token=t"},
    "auth.email_change_notice": {"new_email_hint": "ad…@example.com"},
    "auth.account_exists": {"login_url": f"{ORIGIN}/login", "reset_url": f"{ORIGIN}/reset-password"},
    "security.new_device_paired": {"device_label": INJECTED},
    "security.token_reuse_detected": {"device_label": "Chrome on Windows"},
    "billing.payment_failed": {"plan_name": "Pro"},
    "billing.subscription_changed": {"plan_name": "Power"},
    "billing.subscription_canceled": {"plan_name": "Pro", "ends_at": "2026-11-01"},
    "account.deletion_requested": {"purge_after": "2026-11-15", "cancel_url": f"{ORIGIN}/settings/account"},
    "account.deletion_completed": {},
    "account.data_export_ready": {"download_url": f"{ORIGIN}/settings/account/export/x", "expires_at": "2026-10-22"},
    "account.suspended": {"reason": "Payment dispute"},
    "notify.immediate": {"title": INJECTED, "body": "A thing happened.", "action_url": f"{ORIGIN}/inbox"},
    "notify.digest": {"items": [{"title": INJECTED, "action_url": f"{ORIGIN}/inbox"}], "period": "today"},
    "usage.limit_near": {"allowance_label": "prepares", "used": 8, "limit": 10, "window_end": "2026-11-01"},
    "usage.limit_reached": {"allowance_label": "prepares", "limit": 10, "window_end": "2026-11-01"},
    "announcement.service_notice": {"title": "Maintenance", "body": INJECTED},
}


def test_the_template_ids_are_the_spec_vocabulary():
    assert set(TEMPLATE_IDS) == set(FIXTURES)
    assert len(TEMPLATE_IDS) == 20


@pytest.mark.parametrize("template_id", sorted(FIXTURES))
def test_every_template_renders_with_the_required_links(template_id):
    category = TEMPLATE_CATEGORIES[template_id]
    rendered = render(template_id, FIXTURES[template_id], category=category, app_origin=ORIGIN)
    assert rendered.subject.strip() and "\n" not in rendered.subject
    for body in (rendered.text, rendered.html):
        assert f"{ORIGIN}/settings" in body
        if category == "PRODUCT":
            assert f"{ORIGIN}/settings/communications" in body
        if category == "SERVICE":
            assert "service message" in body.lower()
    assert INJECTED not in rendered.html
    if INJECTED in str(FIXTURES[template_id]):
        assert "&lt;script&gt;" in rendered.html


def test_a_template_cannot_reach_python_internals():
    from jinja2.exceptions import SecurityError
    from webapp.comms.render import sandbox
    with pytest.raises(SecurityError):
        sandbox().from_string("{{ ''.__class__.__mro__[1].__subclasses__() }}").render()
