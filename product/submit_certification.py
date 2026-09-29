"""Bundle 6E-A submit certification catalogue (spec §9). Pure.

Only an adapter VERSION listed here may be submitted, and only when
submission_permitted() says so: LIVE_CERTIFIED with recorded live evidence,
or FIXTURE_CERTIFIED on a loopback origin with the fixture-origin setting on.
extension/src/submit/certification.ts mirrors this catalogue; the shared
vectors in tests/fixtures/submit/egress_vectors.json pin both sides.

Egress templates are regexes over the request URL. Every bound value
(origin, tenant, job) is re.escape()d before substitution, so a bound value
can never widen the pattern; the result is anchored ^…$."""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

FIXTURE_CERTIFIED = "FIXTURE_CERTIFIED"
LIVE_CERTIFIED = "LIVE_CERTIFIED"

_LOOPBACK = re.compile(r"^http://(127\.0\.0\.1|localhost)(:\d{1,5})?$")


@dataclass(frozen=True)
class EgressEntry:
    id: str
    methods: tuple[str, ...]
    types: tuple[str, ...]
    template: str  # regex body with {origin} {tenant} {job} placeholders


@dataclass(frozen=True)
class SubmitCertification:
    certification_id: str
    adapter_id: str
    adapter_version: str
    status: str
    live_evidence: str | None
    submit_control_selector: str
    egress: tuple[EgressEntry, ...]
    success_selectors: tuple[str, ...]
    confirmation_template: str
    failure_selectors: tuple[str, ...]
    challenge_selectors: tuple[str, ...]
    failure_signal_proves_not_submitted: bool


GREENHOUSE_SUBMIT = SubmitCertification(
    certification_id="greenhouse@2/submit@1",
    adapter_id="greenhouse",
    adapter_version="greenhouse@2",
    status=FIXTURE_CERTIFIED,
    live_evidence=None,
    submit_control_selector="#application_form [type=submit]",
    egress=(
        EgressEntry("E1_SUBMIT", ("post",), ("main_frame", "xmlhttprequest"), "{origin}/{tenant}/jobs/{job}"),
        EgressEntry("E2_CONFIRM", ("get",), ("main_frame", "xmlhttprequest"),
                    r"{origin}/{tenant}/jobs/{job}/confirmation(\?.*)?"),
        EgressEntry("E3_RENDER", ("get",), ("stylesheet", "script", "image", "font"), "{origin}/.*"),
        EgressEntry("C1_RECAPTCHA", ("get", "post"), ("script", "sub_frame", "xmlhttprequest", "image"),
                    r"https://www\.(google|gstatic|recaptcha)\.(com|net)/recaptcha/.*"),
    ),
    success_selectors=("#application_confirmation",),
    confirmation_template="{origin}/{tenant}/jobs/{job}/confirmation",
    failure_selectors=("#error_explanation",),
    challenge_selectors=('iframe[src*="recaptcha/api2/bframe"]', 'iframe[title*="challenge" i]',
                         "[data-submit-challenge]"),
    failure_signal_proves_not_submitted=True,
)

SUBMIT_CATALOGUE: dict[tuple[str, str], SubmitCertification] = {
    (GREENHOUSE_SUBMIT.adapter_id, GREENHOUSE_SUBMIT.adapter_version): GREENHOUSE_SUBMIT,
}


def submit_certified(adapter_id: str, adapter_version: str) -> SubmitCertification | None:
    return SUBMIT_CATALOGUE.get((adapter_id, adapter_version))


def _fill(template: str, *, origin: str, tenant_key: str, ats_job_id: str) -> str:
    return (template.replace("{origin}", re.escape(origin)).replace("{tenant}", re.escape(tenant_key))
            .replace("{job}", re.escape(ats_job_id)))


def resolve_egress(cert: SubmitCertification, *, origin: str, tenant_key: str, ats_job_id: str) -> list[dict[str, Any]]:
    return [{"id": e.id, "methods": list(e.methods), "types": list(e.types),
             "regex": "^" + _fill(e.template, origin=origin, tenant_key=tenant_key, ats_job_id=ats_job_id) + "$"}
            for e in cert.egress]


def confirmation_url(cert: SubmitCertification, *, origin: str, tenant_key: str, ats_job_id: str) -> str:
    return (cert.confirmation_template.replace("{origin}", origin).replace("{tenant}", tenant_key)
            .replace("{job}", ats_job_id))


def is_loopback_origin(origin: str) -> bool:
    return bool(_LOOPBACK.fullmatch(origin))


def submission_permitted(cert: SubmitCertification | None, origin: str, *,
                         fixture_origins_enabled: bool) -> tuple[bool, str | None]:
    if cert is None:
        return False, "adapter_not_submit_certified"
    if cert.status == LIVE_CERTIFIED and cert.live_evidence:
        return True, None
    if cert.status == FIXTURE_CERTIFIED and fixture_origins_enabled and is_loopback_origin(origin):
        return True, None
    return False, "adapter_not_live_certified"
