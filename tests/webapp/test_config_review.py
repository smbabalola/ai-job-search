# tests/webapp/test_config_review.py
import pytest

from webapp.config import Settings


@pytest.mark.parametrize("raw,expected", [(None, 14), ("7", 7), ("30", 14), ("0", 1), ("x", 14)])
def test_review_ttl_is_shorter_only(monkeypatch, tmp_path, raw, expected):
    if raw is None:
        monkeypatch.delenv("JOBSEARCH_REVIEW_APPROVAL_TTL_DAYS", raising=False)
    else:
        monkeypatch.setenv("JOBSEARCH_REVIEW_APPROVAL_TTL_DAYS", raw)
    assert Settings(db_path=tmp_path / "d.sqlite3").review_approval_ttl_days == expected
