"""Bundle 7 Task 1: deployment modes and fail-closed hosted configuration (spec H1, §27.1-4)."""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI

from webapp.app import _start_autonomy_driver, create_app
from webapp.config import Settings
from webapp.deployment import DeploymentConfigError, validate_settings

PRODUCTION_CATALOG = Path("product/plans/plan-catalog.v1.json")


def hosted_settings(**overrides) -> Settings:
    values = dict(
        deployment="hosted",
        host="0.0.0.0",
        database_url="postgresql://jobsearch@db.internal:5432/jobsearch",
        secret_key="k" * 43,
        public_origin="https://app.example.test",
        extension_ids=("abcdefghijklmnopabcdefghijklmnop",),
        object_store={"kind": "s3", "bucket": "b", "endpoint_url": "https://s3.example.test",
                      "region": "eu-west-2", "prefix": "prod"},
        billing_provider="fake",
        email_provider="smtp",
        plan_catalog_path=PRODUCTION_CATALOG,
        ai_pricing_path=Path("product/policies/ai-pricing.v1.json"),
    )
    values.update(overrides)
    return Settings(**values)


def test_default_settings_are_local_and_valid():
    settings = Settings()
    assert settings.deployment == "local"
    assert settings.is_hosted is False
    assert validate_settings(settings) == []


def test_local_mode_requires_a_loopback_host():
    assert validate_settings(Settings(host="0.0.0.0")) == ["local mode requires a loopback host"]


@pytest.mark.parametrize("host", ["127.0.0.1", "::1", "localhost"])
def test_local_mode_accepts_loopback_hosts(host):
    assert validate_settings(Settings(host=host)) == []


def test_hosted_settings_with_every_field_valid_have_no_problems():
    settings = hosted_settings()
    assert settings.is_hosted is True
    assert validate_settings(settings) == []


@pytest.mark.parametrize(
    ("override", "problem"),
    [
        ({"database_url": None}, "hosted mode requires a postgresql:// JOBSEARCH_DATABASE_URL"),
        ({"database_url": "sqlite:///x.db"}, "hosted mode requires a postgresql:// JOBSEARCH_DATABASE_URL"),
        ({"secret_key": None}, "hosted mode requires JOBSEARCH_SECRET_KEY of at least 43 characters"),
        ({"secret_key": "short"}, "hosted mode requires JOBSEARCH_SECRET_KEY of at least 43 characters"),
        ({"public_origin": None}, "hosted mode requires an https JOBSEARCH_PUBLIC_ORIGIN without a path"),
        ({"public_origin": "http://app.example.test"}, "hosted mode requires an https JOBSEARCH_PUBLIC_ORIGIN without a path"),
        ({"public_origin": "https://app.example.test/app"}, "hosted mode requires an https JOBSEARCH_PUBLIC_ORIGIN without a path"),
        ({"extension_ids": ()}, "hosted mode requires JOBSEARCH_EXTENSION_IDS"),
        ({"object_store": {"kind": "local", "root": "documents"}}, "hosted mode requires an s3 JOBSEARCH_OBJECT_STORE"),
        ({"email_provider": "console"}, "hosted mode requires a real JOBSEARCH_EMAIL_PROVIDER"),
        ({"plan_catalog_path": Path("product/plans/plan-catalog.dev.json")},
         "hosted mode refuses the development plan catalog"),
        ({"ai_pricing_path": Path("product/policies/ai-pricing.dev.json")},
         "hosted mode refuses the development AI pricing table"),
    ],
)
def test_hosted_mode_refuses_each_missing_or_invalid_setting(override, problem):
    assert validate_settings(hosted_settings(**override)) == [problem]


def test_create_app_refuses_invalid_configuration():
    with pytest.raises(DeploymentConfigError) as excinfo:
        create_app(hosted_settings(secret_key=None))
    assert excinfo.value.problems == ["hosted mode requires JOBSEARCH_SECRET_KEY of at least 43 characters"]


def test_hosted_mode_never_starts_the_in_app_autonomy_driver():
    app = FastAPI()
    settings = hosted_settings(autonomy_scheduler_enabled=True)
    assert _start_autonomy_driver(app, settings) == (None, None)
    assert app.state.autonomy_driver == {"running": False}


def test_settings_read_deployment_values_from_the_environment(monkeypatch):
    monkeypatch.setenv("JOBSEARCH_DEPLOYMENT", "hosted")
    monkeypatch.setenv("JOBSEARCH_DATABASE_URL", "postgresql://u@h/db")
    monkeypatch.setenv("JOBSEARCH_EXTENSION_IDS", "aaa, bbb")
    monkeypatch.setenv("JOBSEARCH_OBJECT_STORE", '{"kind": "s3", "bucket": "b"}')
    settings = Settings()
    assert settings.deployment == "hosted"
    assert settings.database_url == "postgresql://u@h/db"
    assert settings.extension_ids == ("aaa", "bbb")
    assert settings.object_store == {"kind": "s3", "bucket": "b"}


def test_unknown_deployment_value_is_a_problem(monkeypatch):
    monkeypatch.setenv("JOBSEARCH_DEPLOYMENT", "cloud")
    assert validate_settings(Settings()) == ["JOBSEARCH_DEPLOYMENT must be 'local' or 'hosted'"]


@pytest.mark.parametrize(("raw", "expected"), [(None, 10000), ("2500", 2500), ("50", 1000), ("99999", 30000), ("junk", 10000)])
def test_writer_lock_timeout_is_bounded(monkeypatch, raw, expected):
    if raw is None:
        monkeypatch.delenv("JOBSEARCH_WRITER_LOCK_TIMEOUT_MS", raising=False)
    else:
        monkeypatch.setenv("JOBSEARCH_WRITER_LOCK_TIMEOUT_MS", raw)
    assert Settings().writer_lock_timeout_ms == expected
