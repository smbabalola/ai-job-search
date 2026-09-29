"""A real 6D-B service world on top of the 6D-A v2 chain: an EFFECTIVE 6D-A
approval that binds contact:email (profile evidence, ANSWER), the notice
period (an approved answer) and both approved documents, plus a stored
certified greenhouse@2 observation of the approved apply target."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from product.autonomy_contract import Reach
from product.fill_observation import observation_fingerprint, structure_fingerprint
from tests.product.fill_observation_fixtures import element, identity  # noqa: F401
from tests.webapp.services.review_fixtures import (  # noqa: F401
    NOW, V2_ACCOUNT, add_contact_claim, blocker, v2_chain,
)

TARGET = "https://jobs.example.test/acme/123"  # review_fixtures.V2World.set_target default
ORIGIN = "https://jobs.example.test"
DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


def elements(*extra):
    return [
        element("gh:email", control_kind="email", type="email", label="Email", question="Email", name="email",
                id="email", required=True),
        element("gh:notice", label="What is your notice period?", question="What is your notice period?",
                name="notice", id="notice", required=True),
        element("gh:resume", control_kind="file", type="file", label="Resume/CV", question="Resume/CV", name="resume",
                id="resume", accept=".pdf,.doc,.docx"),
        element("gh:cover", control_kind="file", type="file", label="Cover Letter", question="Cover Letter",
                name="cover_letter", id="cover_letter", accept=".pdf,.doc,.docx"),
        element("gh:csrf", control_kind="hidden", type="hidden", name="authenticity_token", visible=False,
                classification="NON_APPLICATION",
                proof={"kind": "ADAPTER_NON_APPLICATION_RULE", "rule": "greenhouse.site_state_hidden@1"}),
        *extra,
    ]


def observation_doc(*extra, url=TARGET):
    return {
        "schema_version": "fill-observation.v1",
        "context": {"canonical_url": url, "origin": ORIGIN, "adapter_id": "greenhouse",
                    "adapter_version": "greenhouse@2", "tenant_key": "acme", "ats_job_id": "123",
                    "frames": [{"frame_path": "0", "origin": ORIGIN}], "application_root_found": True,
                    "multi_step_indicators": []},
        "elements": elements(*extra),
        "submit_controls": [{"control_fingerprint": "sha256:" + "a" * 64}],
    }


def store_observation(world, doc, *, run_id=None, phase="INITIAL"):
    from webapp.persistence import fill as f
    row = f.insert_observation(world.conn, account_id=V2_ACCOUNT, application_workspace_id=world.ws,
                               fill_run_id=run_id, phase=phase, action_index=None,
                               structure_fingerprint=structure_fingerprint(doc),
                               observation_fingerprint=observation_fingerprint(doc), observation=doc, now=NOW)
    world.conn.commit()
    return row


def _prepare(w, *, gate_ready=False, target=TARGET, store=True):
    from webapp.services import review_answers as rv
    add_contact_claim(w, "email", "ada@example.com")
    blocker(w.conn, w.ws, "employment.notice_period", "What is your notice period?")
    w.conn.commit()
    rv.answer_field(w.conn, settings=w.settings, account_id=V2_ACCOUNT, application_workspace_id=w.ws,
                    answer_key="subject:employment.notice_period", value="1 month", reach=Reach.APPLICATION,
                    actor="u", now=NOW)
    if gate_ready:
        _make_6b_ready(w, target)
    rv.set_field_disposition(w.conn, settings=w.settings, account_id=V2_ACCOUNT, application_workspace_id=w.ws,
                             answer_key="contact:email", disposition="ANSWER", actor="u", now=NOW)
    w.make_approvable()
    w.approve()
    assert w.state().approval_effective
    obs = store_observation(w, observation_doc()) if store else None
    return SimpleNamespace(world=w, conn=w.conn, ws=w.ws, settings=w.settings, observation=obs)


def _make_6b_ready(w, target=TARGET):
    """The 6B facts a FILL grant needs, through production paths: the
    governing blocker resolved, an application identity, and FILL authority
    (policy + account and workspace ceilings)."""
    from product.autonomy_contract import Capability
    from webapp.persistence.application_identity import save_application_identity
    from webapp.services.autonomy_context import current_governing_blockers
    from webapp.services.autonomy_controls import enable_autonomous_preparation, set_capability
    from webapp.services.decision_policy import resolve_blocker
    for b in current_governing_blockers(w.conn, w.ws):
        if b["status"] == "open":
            resolve_blocker(w.conn, workspace_id=w.ws, blocker_id=b["id"], request_id=f"r_{b['id']}",
                            answer_value="1 month", answer_scope="APPLICATION_ONLY", resolved_by="u")
    save_application_identity(w.conn, application_workspace_id=w.ws, source_record={
        "source": "greenhouse", "source_record_id": "123", "source_url": target, "company": "Acme",
        "title": "Engineer", "location": "London"})
    w.conn.commit()
    enable_autonomous_preparation(w.conn, account_id=V2_ACCOUNT, actor="u", timezone="Europe/London", now=NOW)
    for scope_type, scope_id in (("ACCOUNT_MAX", V2_ACCOUNT), ("WORKSPACE_CEILING", SEARCH_WS)):
        set_capability(w.conn, account_id=V2_ACCOUNT, scope_type=scope_type, scope_id=scope_id,
                       capability=Capability.FILL, actor="u", now=NOW)


SEARCH_WS = "sw_fill"


def patch_6b_external_reads(monkeypatch, target=TARGET):
    """As the 6B suites do: discovery origin, ATS URL provenance and pack
    staleness have their own suites and are fixed here."""
    from webapp.services import autonomy_context
    from webapp.services.workspace_view import ApplyTarget
    monkeypatch.setattr(autonomy_context, "get_search_workspace_for_application", lambda c, w: SEARCH_WS)
    monkeypatch.setattr(autonomy_context, "resolve_apply_target",
                        lambda c, workspace_id, account_id: ApplyTarget(url=target, provenance="discovery_verified"))
    monkeypatch.setattr(autonomy_context, "pack_readiness", lambda c, **kw: ("art_pack", True))


@pytest.fixture
def fill_world(v2_chain):
    return _prepare(v2_chain)


@pytest.fixture
def grant_world(v2_chain, monkeypatch):
    """fill_world whose 6B gate grants FILL (the plan confirmed too)."""
    import dataclasses
    patch_6b_external_reads(monkeypatch)
    v2_chain.settings = dataclasses.replace(v2_chain.settings, autonomy_max_capability="SUBMIT",
                                            autonomy_submit_capable_adapters=("greenhouse",))
    w = _prepare(v2_chain, gate_ready=True)
    from webapp.services import fill_plans as fp
    shown = fp.fill_plan_presentation(w.conn, settings=w.settings, account_id=V2_ACCOUNT, application_workspace_id=w.ws,
                                      observation_id=w.observation["id"], now=NOW)["displayed_plan_hash"]
    assert shown, "the grant world must have a complete plan"
    fp.confirm_plan(w.conn, settings=w.settings, account_id=V2_ACCOUNT, application_workspace_id=w.ws,
                    observation_id=w.observation["id"], displayed_plan_hash=shown, actor="u", now=NOW)
    w.plan_hash = shown
    return w
