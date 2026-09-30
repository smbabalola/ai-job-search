"""Bundle 7 Task 23 (spec §16.1): user-profile v2 preferences."""
from __future__ import annotations

import pytest

from product.user_profile import (
    USER_PROFILE_VERSION, UserProfileValidationError, normalize_user_profile, normalize_user_profile_for_write,
)


def test_the_version_is_v2_with_defaults():
    assert USER_PROFILE_VERSION == "user-profile.v2"
    profile = normalize_user_profile({})
    assert profile["schema_version"] == "user-profile.v2"
    assert (profile["rotation_preference"], profile["acceptable_rotations"], profile["relocation"],
            profile["job_family_ids"]) == ("no_preference", [], "no", [])


def test_a_v1_document_is_read_as_v2():
    profile = normalize_user_profile({"schema_version": "user-profile.v1", "target_roles": ["Drilling Engineer"]})
    assert profile["schema_version"] == "user-profile.v2" and profile["target_roles"] == ["Drilling Engineer"]
    assert profile["relocation"] == "no"


def test_a_v1_write_is_refused():
    with pytest.raises(UserProfileValidationError, match="user-profile.v2"):
        normalize_user_profile_for_write({"schema_version": "user-profile.v1", "target_roles": ["x"]})
    assert normalize_user_profile_for_write({"target_roles": ["x"]})["schema_version"] == "user-profile.v2"


def test_the_new_fields_validate():
    ok = normalize_user_profile({"rotation_preference": "rotation_only", "acceptable_rotations": ["28/28", "14/14"],
                                 "relocation": "international", "job_family_ids": ["drilling"]})
    assert ok["acceptable_rotations"] == ["14/14", "28/28"]
    for bad in ({"rotation_preference": "sometimes"}, {"acceptable_rotations": ["7/7"]}, {"relocation": "moon"},
                {"job_family_ids": ["Not A Slug!"]}):
        with pytest.raises(UserProfileValidationError):
            normalize_user_profile(bad)
