"""Builders for observation-v1 documents used by the 6D-B product tests.
`element(...)` computes the field fingerprint exactly as the observer must
(product.fill_observation.field_fingerprint over the identity)."""
from __future__ import annotations

import copy

from product.fill_observation import OBSERVATION_SCHEMA, field_fingerprint

ORIGIN = "https://boards.example-ats.test"
URL = f"{ORIGIN}/acme/jobs/123"


def identity(**overrides) -> dict:
    base = {
        "tag": "input", "type": "text", "name": "q", "id": "q", "form_owner": "application_form",
        "label": "Question", "question": "Question", "aria": {}, "required": False, "disabled": False,
        "readonly": False, "visible": True, "options": [], "accept": None, "multiple": False, "maxlength": None,
        "pattern": None, "min": None, "max": None, "frame_path": "0",
    }
    base.update(overrides)
    return base


def element(key: str, *, control_kind: str = "text", classification: str = "APPLICATION", proof=None,
            value_state=None, **identity_overrides) -> dict:
    ident = identity(**identity_overrides)
    return {"page_field_key": key, "control_kind": control_kind, "identity": ident,
            "field_fingerprint": field_fingerprint(ident), "classification": classification, "proof": proof,
            "value_state": value_state or {"state": "BLANK"}}


def observation(elements: list[dict], *, adapter_id="greenhouse", adapter_version="greenhouse@2",
                root=True, multi_step=(), frames=None, submit_controls=()) -> dict:
    return {
        "schema_version": OBSERVATION_SCHEMA,
        "context": {
            "canonical_url": URL, "origin": ORIGIN, "adapter_id": adapter_id, "adapter_version": adapter_version,
            "tenant_key": "acme", "ats_job_id": "123",
            "frames": frames if frames is not None else [{"frame_path": "0", "origin": ORIGIN}],
            "application_root_found": root, "multi_step_indicators": list(multi_step),
        },
        "elements": copy.deepcopy(elements),
        "submit_controls": [{"control_fingerprint": f"sha256:{'a' * 63}{i}"} for i, _ in enumerate(submit_controls)],
    }
