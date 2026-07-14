from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from pipeline_stages import (
    PIPELINE_SCHEMA_VERSION,
    PipelineConfig,
    discussion_from_dict,
    discussion_to_dict,
    family_from_dict,
    family_to_dict,
    stage3_build_dataset,
    stage4_validate_dataset,
    validate_audit,
    require_stage_payload,
    require_complete_or_explicit_partial,
    target_from_dict,
    target_to_dict,
    unique_targets,
)
from prereview_crawler_production import DiscussionComment, Family, Review, Target, TargetBucket, atomic_write_json


class PipelineStageTests(unittest.TestCase):
    def make_family(self) -> Family:
        target = Target(
            kind="doi",
            value="10.20944/preprints202401.0001.v1",
            doi="10.20944/preprints202401.0001.v1",
            family_key="doi:10.20944/preprints202401.0001",
            version=1,
            scheme="doi",
            source_identifier="10.20944/preprints202401.0001.v1",
        )
        review = Review(
            review_id="10.5281/zenodo.123456",
            record_id="123456",
            target=target,
            comment="The manuscript is clear and the methods are reproducible.",
            review_date="2024-02-02",
            review_type="full_review",
            title_hint="A reproducible example study",
            record_url="https://zenodo.org/records/123456",
            creators=["Reviewer One"],
            subjects=["Biology"],
        )
        family = Family(target.family_key)
        family.targets[target.value] = TargetBucket(target, [review])
        return family

    def test_target_round_trip(self) -> None:
        target = next(iter(self.make_family().targets.values())).target
        self.assertEqual(target, target_from_dict(target_to_dict(target)))

    def test_family_round_trip(self) -> None:
        family = self.make_family()
        restored = family_from_dict(family_to_dict(family))
        self.assertEqual(restored.key, family.key)
        self.assertEqual(list(restored.targets), list(family.targets))
        restored_review = next(iter(restored.targets.values())).reviews[0]
        self.assertEqual(restored_review.review_id, "10.5281/zenodo.123456")

    def test_unique_targets_deduplicates(self) -> None:
        family = family_to_dict(self.make_family())
        payload = {"families": [family, family]}
        targets = unique_targets(payload)
        self.assertEqual(len(targets), 1)

    def test_old_stage_schema_is_rejected_explicitly(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "old.json"
            atomic_write_json(path, {"pipeline_schema_version": PIPELINE_SCHEMA_VERSION - 1, "stage": "01_reviews"})
            with self.assertRaisesRegex(ValueError, "Rerun the preceding stage"):
                require_stage_payload(path, "01_reviews")

    def test_partial_stage_requires_explicit_opt_in(self) -> None:
        stage1 = {"stats": {"zenodo_scan_complete": False}}
        with self.assertRaisesRegex(ValueError, "not marked as a complete"):
            require_complete_or_explicit_partial(stage1, PipelineConfig())
        require_complete_or_explicit_partial(stage1, PipelineConfig(allow_partial_scan=True))

    def test_audit_allows_arxiv_family_when_csv_has_no_doi(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            csv_path = Path(temp_dir) / "arxiv.csv"
            csv_path.write_text('DOI\n""\n', encoding="utf-8")
            provenance = {
                field_name: ["test"]
                for field_name in ("DOI", "PaperTitle", "Authors", "Source", "Venue", "Year", "PeerReview", "Field")
            }
            audit = [{
                "output_doi": "",
                "family_key": "arxiv:2401.00001",
                "field_level_provenance": provenance,
                "rounds": [],
            }]
            self.assertEqual(validate_audit(audit, 1, csv_path), [])

    def test_discussion_round_trip(self) -> None:
        value = DiscussionComment(
            comment_id="10.5281/zenodo.2",
            record_id="2",
            target_review_id="10.5281/zenodo.1",
            family_key="doi:10.20944/preprints202401.0001",
            content="A follow-up comment.",
            comment_date="2024-02-03",
            record_url="https://zenodo.org/records/2",
            creators=["Author A"],
            creator_orcids=["0000-0001-2345-6789"],
            body_source="description",
            target_relation_verified=True,
        )
        self.assertEqual(value, discussion_from_dict(discussion_to_dict(value)))

    def test_stage3_rejects_incomplete_stage2_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            stage1_path = root / "01.json"
            stage2_path = root / "02.json"
            atomic_write_json(stage1_path, {
                "pipeline_schema_version": PIPELINE_SCHEMA_VERSION,
                "stage": "01_reviews",
                "stats": {"zenodo_scan_complete": True},
                "families": [family_to_dict(self.make_family())],
            })
            atomic_write_json(stage2_path, {
                "pipeline_schema_version": PIPELINE_SCHEMA_VERSION,
                "stage": "02_metadata",
                "records": {},
            })
            with self.assertRaisesRegex(ValueError, "does not cover every"):
                stage3_build_dataset(
                    reviews_input=stage1_path, metadata_input=stage2_path,
                    output=root / "out.csv", extended_output=None,
                    audit_output=root / "audit.json", dedup_output=root / "dedup.json",
                    stats_output=root / "stats.json", limit=1,
                    config=PipelineConfig(state_dir=str(root / "state")),
                )

    def test_stage3_and_stage4_from_frozen_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            family = self.make_family()
            target = next(iter(family.targets.values())).target
            stage1 = {
                "pipeline_schema_version": PIPELINE_SCHEMA_VERSION,
                "stage": "01_reviews",
                "stats": {"zenodo_scan_complete": True},
                "families": [family_to_dict(family)],
                "responses_by_review": {},
                "discussions_by_review": {},
                "response_family_keys": [],
                "interaction_family_keys": [],
            }
            metadata = {
                "title": "A reproducible example study",
                "authors": ["Author A"],
                "author_orcids": ["0000-0001-2345-6789"],
                "year": "2024",
                "venue": "Preprints.org",
                "venue_candidates": ["Preprints.org"],
                "field_candidates": [{"value": "Biology", "source": "Crossref"}],
                "sources": ["Crossref"],
                "provenance": {
                    "title": ["Crossref"],
                    "authors": ["Crossref"],
                    "author_orcids": ["Crossref"],
                    "year": ["Crossref"],
                    "venue": ["Crossref"],
                    "field": ["Crossref"],
                },
            }
            stage2 = {
                "pipeline_schema_version": PIPELINE_SCHEMA_VERSION,
                "stage": "02_metadata",
                "records": {
                    target.value: {
                        "target": target_to_dict(target),
                        "status": "resolved",
                        "metadata": metadata,
                    }
                },
            }
            review_path = root / "01_reviews.json"
            metadata_path = root / "02_metadata.json"
            atomic_write_json(review_path, stage1)
            atomic_write_json(metadata_path, stage2)
            output = root / "03_dataset.csv"
            config = PipelineConfig(state_dir=str(root / "state"), checkpoint_every=1)
            stats = stage3_build_dataset(
                reviews_input=review_path,
                metadata_input=metadata_path,
                output=output,
                extended_output=root / "03_dataset_extended.csv",
                audit_output=root / "03_audit.json",
                dedup_output=root / "03_dedup.json",
                stats_output=root / "03_stats.json",
                limit=1,
                config=config,
            )
            self.assertEqual(stats["written"], 1)
            report = stage4_validate_dataset(
                csv_input=output,
                report_output=root / "04_validation.json",
                expected=1,
                audit_input=root / "03_audit.json",
            )
            self.assertTrue(report["valid"], json.dumps(report, indent=2))

            audit_path = root / "03_audit.json"
            mismatched_audit = json.loads(audit_path.read_text(encoding="utf-8"))
            mismatched_audit[0]["output_doi"] = "10.9999/not-the-csv-paper"
            atomic_write_json(audit_path, mismatched_audit)
            mismatch_report = stage4_validate_dataset(
                csv_input=output,
                report_output=root / "04_validation_mismatch.json",
                expected=1,
                audit_input=audit_path,
            )
            self.assertFalse(mismatch_report["valid"])
            self.assertTrue(
                any("does not match CSV DOI" in issue for issue in mismatch_report["issues"]),
                mismatch_report,
            )


if __name__ == "__main__":
    unittest.main()
