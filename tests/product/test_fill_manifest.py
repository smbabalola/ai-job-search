from __future__ import annotations

import copy

import pytest

from product.fill_manifest import FillManifestError, manifest_hash, validate_fill_manifest, value_hash


def _manifest():
    return {
        "schema_version": "fill-manifest.v1",
        "application_workspace_id": "ws_1",
        "adapter_id": "greenhouse", "adapter_version": "1.0.0",
        "pages": [{
            "page_key": "p1",
            "entries": [
                {"page_field_key": "email", "normalized_field_type": "email", "subject": None,
                 "source": {"kind": "EVIDENCE", "ref": "contact.email", "confirmation_id": None},
                 "transform_id": "identity", "value_hash": value_hash("a@b.c"), "required": True},
                {"page_field_key": "notice", "normalized_field_type": None, "subject": "employment.notice_period",
                 "source": {"kind": "APPROVED_ANSWER", "ref": "ans_1", "confirmation_id": "conf_1"},
                 "transform_id": "whitespace_normalize", "value_hash": value_hash("1 month"), "required": True},
            ],
        }],
    }


def test_valid_manifest():
    validate_fill_manifest(_manifest())


def test_hash_ignores_entry_order_within_page_but_not_page_order():
    a = _manifest()
    b = copy.deepcopy(a)
    b["pages"][0]["entries"].reverse()
    assert manifest_hash(a) == manifest_hash(b)
    c = copy.deepcopy(a)
    c["pages"].append({"page_key": "p0", "entries": []})
    d = copy.deepcopy(c)
    d["pages"].reverse()
    assert manifest_hash(c) != manifest_hash(d)


@pytest.mark.parametrize("mutate, message", [
    (lambda m: m["pages"][0]["entries"][1]["source"].update(confirmation_id=None), "confirmation_id"),
    (lambda m: m["pages"][0]["entries"][0].update(transform_id="llm_rewrite"), "transform_id"),
    (lambda m: m["pages"][0]["entries"][0].update(subject="licence.driving"), "exactly one"),
    (lambda m: m["pages"][0]["entries"].append(dict(m["pages"][0]["entries"][0])), "duplicate"),
    (lambda m: m["pages"][0]["entries"][0]["source"].update(kind="GENERATED"), "source.kind"),
    (lambda m: m["pages"][0]["entries"][1].update(subject="made.up"), "subject"),
])
def test_invalid_manifests(mutate, message):
    m = _manifest()
    mutate(m)
    with pytest.raises(FillManifestError) as exc:
        validate_fill_manifest(m)
    assert any(message in e for e in exc.value.errors), exc.value.errors
