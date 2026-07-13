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
    PipelineConfig,
    family_from_dict,
    family_to_dict,
    stage3_build_dataset,
    stage4_validate_dataset,
    target_from_dict,
    target_to_dict,
    unique_targets,
)
from prereview_crawler_production import Family, Review, Target, TargetBucket, atomic_write_json


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

    def test_stage3_and_stage4_from_frozen_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            family = self.make_family()
            target = next(iter(family.targets.values())).target
            stage1 = {
                "pipeline_schema_version": 1,
                "stage": "01_reviews",
                "families": [family_to_dict(family)],
                "responses_by_review": {},
                "response_family_keys": [],
            }
            metadata = {
                "title": "A reproducible example study",
                "authors": ["Author A"],
                "year": "2024",
                "venue": "Preprints.org",
                "venue_candidates": ["Preprints.org"],
                "field_candidates": [{"value": "Biology", "source": "Crossref"}],
                "sources": ["Crossref"],
                "provenance": {
                    "title": ["Crossref"],
                    "authors": ["Crossref"],
                    "year": ["Crossref"],
                    "venue": ["Crossref"],
                    "field": ["Crossref"],
                },
            }
            stage2 = {
                "pipeline_schema_version": 1,
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
            )
            self.assertTrue(report["valid"], json.dumps(report, indent=2))


if __name__ == "__main__":
    unittest.main()
