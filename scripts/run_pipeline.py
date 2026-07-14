#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path
import sys

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from pipeline_stages import (
    PipelineConfig,
    default_paths,
    stage1_collect_reviews,
    stage2_resolve_metadata,
    stage3_build_dataset,
    stage4_validate_dataset,
)


def main() -> None:
    parser = argparse.ArgumentParser(description="Run all PREreview pipeline stages in order.")
    parser.add_argument("--root", type=Path, default=Path("data/pipeline"))
    parser.add_argument("--state-dir", default="data/pipeline/state")
    parser.add_argument("--limit", type=int, default=200)
    parser.add_argument("--max-pages", type=int, default=100)
    parser.add_argument("--checkpoint-every", type=int, default=25)
    parser.add_argument("--seed", default="PREreview-staged-pipeline-v1")
    parser.add_argument("--field-policy", choices=["empty", "native", "metadata", "broad"], default="metadata")
    parser.add_argument("--sampling-policy", choices=["hash", "coverage"], default="hash")
    parser.add_argument("--use-openalex", action="store_true")
    parser.add_argument("--refresh-zenodo", action="store_true")
    parser.add_argument("--allow-partial-scan", action="store_true", help="Allow an intentionally incomplete snapshot for small tests")
    parser.add_argument("--refresh-metadata", action="store_true")
    parser.add_argument("--retry-missing", action="store_true")
    parser.add_argument("--no-resume", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    paths = default_paths(args.root)
    config = PipelineConfig.from_env(
        state_dir=args.state_dir,
        seed=args.seed,
        checkpoint_every=args.checkpoint_every,
        resume=not args.no_resume,
        refresh_zenodo=args.refresh_zenodo,
        allow_partial_scan=args.allow_partial_scan,
        refresh_metadata=args.refresh_metadata,
        use_openalex=args.use_openalex,
        field_policy=args.field_policy,
        sampling_policy=args.sampling_policy,
    )
    reports = {
        "stage1": stage1_collect_reviews(
            output=paths["reviews"],
            stats_output=paths["reviews_stats"],
            max_pages=args.max_pages,
            config=config,
        ),
        "stage2": stage2_resolve_metadata(
            reviews_input=paths["reviews"],
            output=paths["metadata"],
            stats_output=paths["metadata_stats"],
            config=config,
            retry_missing=args.retry_missing,
        ),
        "stage3": stage3_build_dataset(
            reviews_input=paths["reviews"],
            metadata_input=paths["metadata"],
            output=paths["csv"],
            extended_output=paths["extended_csv"],
            audit_output=paths["audit"],
            dedup_output=paths["dedup"],
            stats_output=paths["build_stats"],
            limit=args.limit,
            config=config,
        ),
        "stage4": stage4_validate_dataset(
            csv_input=paths["csv"],
            report_output=paths["validation"],
            expected=args.limit,
            audit_input=paths["audit"],
        ),
    }
    print(json.dumps(reports, ensure_ascii=False, indent=2))
    if not reports["stage4"]["valid"]:
        raise SystemExit(2)


if __name__ == "__main__":
    main()
