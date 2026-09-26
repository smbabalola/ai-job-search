from __future__ import annotations

from datetime import timedelta

import pytest

from product.autonomy_contract import REACH_ORDER, EmployerKeyStrength, Reach, RepresentationRequirement
from product.autonomy_gate import usable_answer_candidates
from product.fill_manifest import value_hash
from product.semantic_subject_policy import load_subject_policy, subject_entry
from webapp.persistence import review_approval as ra
from webapp.persistence.autonomy_answers import save_proposed_answer
from tests.webapp.services.review_fixtures import (  # noqa: F401
    ACCOUNT, EMPLOYER, NOW, SEARCH_WS, answer, blocker, by_key, claim, conn, fields, make_workspace, profile,
    warning_types,
)

NOTICE = "employment.notice_period"


def delta(conn, ws, *, kind="NEW_QUESTION", subject=None, required=True, answer_key=None, field_key="f1"):
    return ra.insert_delta(conn, account_id=ACCOUNT, application_workspace_id=ws, kind=kind, answer_key=answer_key,
                           subject=subject, required=required, question="Q?", observed={"field_key": field_key},
                           source="FILL_SESSION:s1", now=NOW)


def test_blocker_subjects_are_required_and_contact_fields_optional_without_a_requirement(conn):
    ws = make_workspace(conn)
    blocker(conn, ws, NOTICE, "Notice period?")
    planned = by_key(fields(conn, ws, profile_payload=profile(claim("clm_e", "email", "a@b.test"))))
    assert planned[f"subject:{NOTICE}"].required and planned[f"subject:{NOTICE}"].disposition is None
    email = planned["contact:email"]
    assert not email.required and email.disposition is None and email.source_kind is None


def test_contact_field_required_when_a_delta_or_blocker_names_its_subject(conn):
    ws = make_workspace(conn)
    delta(conn, ws, subject="email", answer_key="subject:email")
    planned = by_key(fields(conn, ws, profile_payload=profile(claim("clm_e", "email", "a@b.test"))))
    email = planned["contact:email"]
    assert email.required and email.disposition == "ANSWER" and email.source_ref == "clm_e"
    assert email.value_hash == value_hash("a@b.test") and "subject:email" not in planned


def test_narrowest_applicable_answer_is_chosen_via_the_shared_rule(conn):
    ws = make_workspace(conn)
    blocker(conn, ws, NOTICE)
    answer(conn, NOTICE, "1 month")
    mine = answer(conn, NOTICE, "2 months", reach=Reach.APPLICATION, scope_id=ws)
    field = by_key(fields(conn, ws))[f"subject:{NOTICE}"]
    assert field.source_ref == mine["id"] and field.value_hash == value_hash("2 months") and field.reach == "APPLICATION"


def test_two_answers_at_the_same_narrowest_reach_are_ambiguous(conn):
    ws = make_workspace(conn)
    blocker(conn, ws, NOTICE)
    answer(conn, NOTICE, "1 month")
    answer(conn, NOTICE, "3 months")
    assert "ambiguous_answer" in warning_types(fields(conn, ws))


def test_expired_answer_raises_a_blocking_warning_with_unchanged_value_hash(conn):
    ws = make_workspace(conn)
    blocker(conn, ws, NOTICE)
    answer(conn, NOTICE, "1 month", now=NOW - timedelta(days=61))
    planned = fields(conn, ws)
    assert "answer_expired" in warning_types(planned)
    assert by_key(planned)[f"subject:{NOTICE}"].value_hash == value_hash("1 month")
    assert by_key(planned)[f"subject:{NOTICE}"].freshness == "expired"


def test_stale_evidence_basis_is_blocking_with_unchanged_value_hash(conn):
    from webapp.services.autonomy_context import evidence_values_hash
    ws = make_workspace(conn)
    blocker(conn, ws, NOTICE)
    before = profile(claim("clm_n", "notice", "1 month"))
    basis = {"kind": "EVIDENCE", "evidence_ids": ["clm_n"], "value_hash": evidence_values_hash(before, ["clm_n"])}
    answer(conn, NOTICE, "1 month", basis=basis)
    assert "answer_basis_stale" not in warning_types(fields(conn, ws, profile_payload=before))
    after = profile(claim("clm_n", "notice", "3 months"))
    planned = fields(conn, ws, profile_payload=after)
    assert "answer_basis_stale" in warning_types(planned)
    assert by_key(planned)[f"subject:{NOTICE}"].value_hash == value_hash("1 month")


def test_optional_field_without_disposition_is_undecided_and_omit_is_bound(conn):
    ws = make_workspace(conn)
    p = profile(claim("clm_e", "email", "a@b.test"))
    assert by_key(fields(conn, ws, profile_payload=p))["contact:email"].disposition is None
    ra.set_disposition(conn, account_id=ACCOUNT, application_workspace_id=ws, answer_key="contact:email",
                       disposition="OMIT", actor="u", now=NOW)
    assert by_key(fields(conn, ws, profile_payload=p))["contact:email"].disposition == "OMIT"
    ra.set_disposition(conn, account_id=ACCOUNT, application_workspace_id=ws, answer_key="contact:email",
                       disposition="ANSWER", actor="u", now=NOW)
    assert by_key(fields(conn, ws, profile_payload=p))["contact:email"].source_ref == "clm_e"


def test_pending_proposal_is_blocking_until_accepted(conn):
    ws = make_workspace(conn)
    b = blocker(conn, ws, NOTICE)
    proposal = save_proposed_answer(conn, blocker_id=b["id"], subject=NOTICE, value="1 month", now=NOW)
    assert "proposal_unaccepted" in warning_types(fields(conn, ws))
    accepted = answer(conn, NOTICE, "1 month")
    ra.record_event(conn, account_id=ACCOUNT, application_workspace_id=ws, event="PROPOSAL_ACCEPTED",
                    binding_hash=None, detail={"proposal_id": proposal["id"], "approved_answer_id": accepted["id"]},
                    actor="u", now=NOW)
    assert "proposal_unaccepted" not in warning_types(fields(conn, ws))


def test_placeholder_or_conflicted_contact_claims_are_not_planned(conn):
    ws = make_workspace(conn)
    p = profile(claim("clm_e", "email", "a@b.test", placeholder=True),
                claim("clm_p", "phone", "0123", concept_id="cpt_phone"), conflicts=("cpt_phone",))
    planned = by_key(fields(conn, ws, profile_payload=p))
    assert "contact:email" not in planned and "contact:phone" not in planned


def test_unknown_subject_delta_is_planned_under_delta_key(conn):
    ws = make_workspace(conn)
    d = delta(conn, ws, required=True)
    planned = fields(conn, ws)
    field = by_key(planned)[f"delta:{d['id']}"]
    assert field.disposition is None and field.subject is None
    assert "unclassified_required_question" in warning_types(planned)


def test_unclassified_optional_delta_accepts_only_omit(conn):
    ws = make_workspace(conn)
    d = delta(conn, ws, required=False)
    key = f"delta:{d['id']}"
    ra.set_disposition(conn, account_id=ACCOUNT, application_workspace_id=ws, answer_key=key, disposition="ANSWER",
                       actor="u", now=NOW)
    assert by_key(fields(conn, ws))[key].disposition is None
    ra.set_disposition(conn, account_id=ACCOUNT, application_workspace_id=ws, answer_key=key, disposition="OMIT",
                       actor="u", now=NOW)
    assert by_key(fields(conn, ws))[key].disposition == "OMIT"


def test_classified_predecessor_is_not_planned(conn):
    ws = make_workspace(conn)
    old = delta(conn, ws, required=True)
    new = delta(conn, ws, subject=NOTICE, answer_key=f"subject:{NOTICE}", required=True)
    ra.record_event(conn, account_id=ACCOUNT, application_workspace_id=ws, event="DELTA_RESOLVED", binding_hash=None,
                    detail={"delta_id": old["id"], "reason": "classified", "successor_delta_id": new["id"]},
                    actor="system", now=NOW)
    planned = by_key(fields(conn, ws))
    assert f"delta:{old['id']}" not in planned and planned[f"subject:{NOTICE}"].required


def test_non_field_deltas_never_become_fields(conn):
    ws = make_workspace(conn)
    for kind in ("TARGET_CHANGE", "NEW_UPLOAD", "DOCUMENT_CONVERSION"):
        delta(conn, ws, kind=kind, required=True)
    assert fields(conn, ws)[0] == ()


def test_sensitive_application_answer_is_not_planned_for_another_application(conn):
    a, b = make_workspace(conn, company="Acme"), make_workspace(conn, company="Acme")
    for ws in (a, b):
        blocker(conn, ws, "demographic.eeo")
    answer(conn, "demographic.eeo", "prefer not to say", reach=Reach.APPLICATION, scope_id=a)
    assert by_key(fields(conn, a))["subject:demographic.eeo"].disposition == "ANSWER"
    assert by_key(fields(conn, b))["subject:demographic.eeo"].disposition is None


@pytest.mark.parametrize("scopes", [
    ("ACCOUNT",), ("ACCOUNT", "APPLICATION:self"), ("APPLICATION:other",), ("SEARCH_WORKSPACE", "ACCOUNT"),
    ("APPLICATION:other", "ACCOUNT"), ("EMPLOYER:acme", "ACCOUNT"), ("EMPLOYER:other",)])
def test_review_and_authorization_context_agree_on_usable_candidates(conn, scopes):
    from webapp.services.autonomy_context import answer_candidates
    ws = make_workspace(conn)
    subject = NOTICE  # max reach ACCOUNT: every reach is valid
    blocker(conn, ws, subject)
    for scope in scopes:
        if scope == "ACCOUNT":
            answer(conn, subject, "account")
        elif scope == "SEARCH_WORKSPACE":
            answer(conn, subject, "workspace", reach=Reach.SEARCH_WORKSPACE, scope_id=SEARCH_WS)
        elif scope.startswith("EMPLOYER"):
            answer(conn, subject, scope, reach=Reach.EMPLOYER, scope_id=EMPLOYER if scope.endswith("acme") else "name:x")
        else:
            other = make_workspace(conn, company="Other")
            answer(conn, subject, scope, reach=Reach.APPLICATION, scope_id=ws if scope.endswith("self") else other)
    cands = answer_candidates(conn, account_id=ACCOUNT, workspace_id=ws, subject=subject, profile_payload={})
    req = RepresentationRequirement(key=f"subject:{subject}", subject=subject, required=True,
                                    evidence_available=False, candidates=cands)
    usable, _ = usable_answer_candidates(req, subject_entry(load_subject_policy(), subject),
                                         application_workspace_id=ws, search_workspace_id=SEARCH_WS,
                                         employer_key=EMPLOYER, employer_key_strength=EmployerKeyStrength.NORMALIZED_NAME)
    field = by_key(fields(conn, ws))[f"subject:{subject}"]
    if usable:
        narrowest = min(REACH_ORDER[c.reach] for c in usable)
        assert field.source_ref in {c.approved_answer_id for c in usable if REACH_ORDER[c.reach] == narrowest}
    else:
        assert field.source_ref is None and field.disposition is None


def test_required_contact_requirement_without_a_profile_value_is_not_lost(conn):
    ws = make_workspace(conn)
    delta(conn, ws, subject="email", answer_key="subject:email", required=True)
    planned = fields(conn, ws, profile_payload=profile())
    assert by_key(planned)["subject:email"].required and by_key(planned)["subject:email"].disposition is None
    assert warning_types(planned)  # it blocks approval rather than disappearing
