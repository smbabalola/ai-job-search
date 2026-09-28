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


@pytest.fixture
def fill_world(v2_chain):
    from webapp.services import review_answers as rv
    w = v2_chain
    add_contact_claim(w, "email", "ada@example.com")
    blocker(w.conn, w.ws, "employment.notice_period", "What is your notice period?")
    w.conn.commit()
    rv.answer_field(w.conn, settings=w.settings, account_id=V2_ACCOUNT, application_workspace_id=w.ws,
                    answer_key="subject:employment.notice_period", value="1 month", reach=Reach.APPLICATION,
                    actor="u", now=NOW)
    rv.set_field_disposition(w.conn, settings=w.settings, account_id=V2_ACCOUNT, application_workspace_id=w.ws,
                             answer_key="contact:email", disposition="ANSWER", actor="u", now=NOW)
    w.make_approvable()
    w.approve()
    assert w.state().approval_effective
    obs = store_observation(w, observation_doc())
    return SimpleNamespace(world=w, conn=w.conn, ws=w.ws, settings=w.settings, observation=obs)
