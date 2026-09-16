from __future__ import annotations

import copy
import json
import subprocess
import sys
import unittest
from pathlib import Path

import product.job_fit as legacy
from product.job_posting import (
    JOB_EVIDENCE_COLLECTIONS,
    JOB_POSTING_SNAPSHOT_VERSION,
    REQUIREMENT_KINDS,
    JobPostingValidationError,
    is_trustworthy_job_url,
    job_snapshot_content_id,
    validate_job_posting_snapshot,
)


ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests" / "fixtures" / "job_understanding" / "job-snapshot.json"


class JobPostingContractParityTests(unittest.TestCase):
    def snapshot(self) -> dict:
        return json.loads(FIXTURE.read_text(encoding="utf-8"))

    def test_schema_owned_constants_are_unchanged(self):
        self.assertEqual(JOB_POSTING_SNAPSHOT_VERSION, legacy.JOB_POSTING_SNAPSHOT_VERSION)
        self.assertEqual(REQUIREMENT_KINDS, legacy.REQUIREMENT_KINDS)
        self.assertEqual(JOB_EVIDENCE_COLLECTIONS, legacy.JOB_EVIDENCE_COLLECTIONS)

    def test_valid_snapshot_is_accepted_by_both_entry_points(self):
        snapshot = self.snapshot()
        validate_job_posting_snapshot(snapshot)
        legacy.validate_job_posting_snapshot(snapshot)

    def test_invalid_snapshot_errors_are_identical_through_compatibility_wrapper(self):
        mutations = []
        missing = self.snapshot()
        missing.pop("title")
        mutations.append(missing)
        duplicate = self.snapshot()
        duplicate["responsibilities"] = [copy.deepcopy(duplicate["requirements"][0])]
        mutations.append(duplicate)
        malformed = self.snapshot()
        malformed["requirements"] = {"not": "an array"}
        mutations.append(malformed)
        bad_kind = self.snapshot()
        bad_kind["requirements"][0]["kind"] = "invented"
        mutations.append(bad_kind)

        for snapshot in mutations:
            with self.subTest(snapshot=snapshot):
                with self.assertRaises(JobPostingValidationError) as product_error:
                    validate_job_posting_snapshot(snapshot)
                with self.assertRaises(legacy.JobFitValidationError) as legacy_error:
                    legacy.validate_job_posting_snapshot(snapshot)
                self.assertEqual(product_error.exception.errors, legacy_error.exception.errors)

    def test_content_identity_is_byte_for_byte_compatible(self):
        snapshot = self.snapshot()
        self.assertEqual(
            job_snapshot_content_id(snapshot),
            legacy.job_snapshot_content_id(snapshot),
        )
        reordered = dict(reversed(list(snapshot.items())))
        self.assertEqual(job_snapshot_content_id(snapshot), job_snapshot_content_id(reordered))

    def test_job_understanding_import_graph_is_candidate_independent(self):
        script = """
import sys
import product.job_understanding
import product.job_ingestion
for name in ('product.job_fit', 'product.profile_snapshot', 'product.extensions', 'product.evaluation_policy'):
    assert name not in sys.modules, name
"""
        completed = subprocess.run(
            [sys.executable, "-c", script],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)


class JobPostingSourceUrlTests(unittest.TestCase):
    def snapshot(self) -> dict:
        return json.loads(FIXTURE.read_text(encoding="utf-8"))

    def test_is_trustworthy_job_url_accepts_https(self):
        self.assertTrue(is_trustworthy_job_url("https://boards.example.com/jobs/42"))

    def test_is_trustworthy_job_url_accepts_http(self):
        self.assertTrue(is_trustworthy_job_url("http://boards.example.com/jobs/42"))

    def test_is_trustworthy_job_url_rejects_relative_url(self):
        self.assertFalse(is_trustworthy_job_url("/jobs/42"))

    def test_is_trustworthy_job_url_rejects_hostname_less_url(self):
        self.assertFalse(is_trustworthy_job_url("https:///jobs/42"))

    def test_is_trustworthy_job_url_rejects_unsupported_scheme(self):
        self.assertFalse(is_trustworthy_job_url("ftp://boards.example.com/jobs/42"))

    def test_is_trustworthy_job_url_rejects_malformed_url(self):
        self.assertFalse(is_trustworthy_job_url("not a url"))

    def test_is_trustworthy_job_url_preserves_query_and_fragment(self):
        url = "https://boards.example.com/jobs/42?ref=abc#section"
        self.assertTrue(is_trustworthy_job_url(url))

    def test_valid_snapshot_with_source_url_is_accepted(self):
        snapshot = self.snapshot()
        snapshot["source_url"] = "https://boards.example.com/jobs/42"
        validate_job_posting_snapshot(snapshot)

    def test_malformed_snapshot_source_url_is_rejected(self):
        snapshot = self.snapshot()
        snapshot["source_url"] = "not a url"
        with self.assertRaises(JobPostingValidationError) as context:
            validate_job_posting_snapshot(snapshot)
        self.assertTrue(
            any("source_url" in error for error in context.exception.errors)
        )

    def test_relative_snapshot_source_url_is_rejected(self):
        snapshot = self.snapshot()
        snapshot["source_url"] = "/jobs/42"
        with self.assertRaises(JobPostingValidationError):
            validate_job_posting_snapshot(snapshot)


if __name__ == "__main__":
    unittest.main()
