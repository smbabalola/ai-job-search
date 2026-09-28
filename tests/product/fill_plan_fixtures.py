"""A small, explicit 6D-B planning world: a 6D-A-shaped approval binding, the
approved cleartext values (used only for rendering hashes), the approved
documents, and a certified greenhouse@2 observation."""
from __future__ import annotations

from product.fill_certification import certified
from product.fill_manifest import value_hash
from product.review_contract import binding_hash
from tests.product.fill_observation_fixtures import element, observation

ENTRY = certified("greenhouse", "greenhouse@2")
TRANSFORMS = sorted(["identity", "whitespace_normalize", "country_name_to_iso2", "country_iso2_to_name",
                     "date_iso_to_dmy", "date_iso_to_mdy", "phone_e164"])
DOC_SHA = {"cv": "a" * 64, "cover_letter": "b" * 64}
DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


def field(answer_key, *, disposition="ANSWER", required=True, source_kind="APPROVED_ANSWER", value=None,
          subject=None):
    answering = disposition == "ANSWER"
    return {"answer_key": answer_key, "subject": subject or answer_key.split(":", 1)[1], "required": required,
            "disposition": disposition, "source_kind": source_kind if answering else None,
            "source_ref": f"ref_{answer_key.split(':', 1)[1]}" if answering else None,
            "value_hash": value_hash(value) if answering else None, "permitted_transforms": TRANSFORMS}


VALUES = {"contact:full_name": "Ada Lovelace", "contact:email": "ada@example.com", "contact:phone": "+44 20 7946 0000",
          "subject:employment.notice_period": "1 month", "subject:mobility.relocation": "Yes"}


def binding(extra_fields=(), *, omit=()):
    fields = [
        field("contact:full_name", source_kind="EVIDENCE", value=VALUES["contact:full_name"]),
        field("contact:email", source_kind="EVIDENCE", value=VALUES["contact:email"]),
        field("contact:phone", source_kind="EVIDENCE", value=VALUES["contact:phone"], required=False),
        field("subject:employment.notice_period", value=VALUES["subject:employment.notice_period"]),
        field("subject:mobility.relocation", value=VALUES["subject:mobility.relocation"], required=False),
        *extra_fields,
    ]
    fields = [({**f, "disposition": "OMIT", "source_kind": None, "source_ref": None, "value_hash": None}
               if f["answer_key"] in omit else f) for f in fields]
    return {
        "schema_version": "application-approval-binding.v1", "account_id": "account_local",
        "application_workspace_id": "ws_1",
        "job": {"identity_key": "source:x", "identity_strength": "SOURCE_RECORD", "job_posting_content_id": "jp"},
        "apply_target": {"canonical_url": "url:https://boards.example-ats.test/acme/jobs/123",
                         "provenance": "discovery_verified"},
        "pack": {"artifact_id": "art_1", "content_hash": "sha256:" + "c" * 64, "schema_version": "application-pack.v2"},
        "documents": [
            {"kind": "cover_letter", "document_version_id": "docv_cl", "sha256": DOC_SHA["cover_letter"], "byte_length": 900,
             "origin": "ai_generated", "claim_provenance_hash": None},
            {"kind": "cv", "document_version_id": "docv_cv", "sha256": DOC_SHA["cv"], "byte_length": 1200,
             "origin": "ai_generated", "claim_provenance_hash": None}],
        "fields": sorted(fields, key=lambda f: f["answer_key"]),
        "review_warnings": [], "review_contract_version": "review-contract.v1",
    }


DOCUMENTS = {"cv": {"document_version_id": "docv_cv", "sha256": DOC_SHA["cv"], "byte_length": 1200,
                    "filename": "cv.docx", "media_type": DOCX},
             "cover_letter": {"document_version_id": "docv_cl", "sha256": DOC_SHA["cover_letter"], "byte_length": 900,
                              "filename": "cover_letter.docx", "media_type": DOCX}}

RELOCATE_OPTIONS = [{"option_value": "", "option_text": "Select"}, {"option_value": "Yes", "option_text": "Yes"},
                    {"option_value": "No", "option_text": "No"}]


def page(*extra, value_states=None):
    value_states = value_states or {}
    elements = [
        element("gh:full_name", label="Full name", question="Full name", name="full_name", id="full_name", required=True),
        element("gh:email", control_kind="email", type="email", label="Email", question="Email", name="email",
                id="email", required=True),
        element("gh:notice", label="What is your notice period?", question="What is your notice period?",
                name="notice", id="notice", required=True),
        element("gh:relocate", control_kind="select", tag="select", type="select-one",
                label="Are you willing to relocate?", question="Are you willing to relocate?", name="relocate",
                id="relocate", options=RELOCATE_OPTIONS),
        element("gh:resume", control_kind="file", type="file", label="Resume/CV", question="Resume/CV", name="resume",
                id="resume", accept=".pdf,.doc,.docx"),
        element("gh:csrf", control_kind="hidden", type="hidden", name="authenticity_token", visible=False,
                classification="NON_APPLICATION",
                proof={"kind": "ADAPTER_NON_APPLICATION_RULE", "rule": "greenhouse.site_state_hidden@1"}),
        *extra,
    ]
    for el in elements:
        if el["page_field_key"] in value_states:
            el["value_state"] = value_states[el["page_field_key"]]
    return observation(elements)


def build(obs=None, bind=None, choices=None, values=None):
    from product.fill_plan import build_plan
    bind = bind or binding()
    return build_plan(observation=obs or page(), binding=bind, approval_id="apr_1", binding_hash=binding_hash(bind),
                      catalogue_entry=ENTRY, mapping_choices=choices or {}, values=values or VALUES,
                      documents=DOCUMENTS)
