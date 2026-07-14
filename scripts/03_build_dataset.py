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

from pipeline_stages import PipelineConfig, default_paths, stage3_build_dataset


def main() -> None:
    paths = default_paths()
    parser = argparse.ArgumentParser(description="Stage 3: build CSV from frozen review and metadata artifacts.")
    parser.add_argument("--reviews", type=Path, default=paths["reviews"])
    parser.add_argument("--metadata", type=Path, default=paths["metadata"])
    parser.add_argument("--output", type=Path, default=paths["csv"])
    parser.add_argument("--extended-output", type=Path, default=paths["extended_csv"])
    parser.add_argument("--audit", type=Path, default=paths["audit"])
    parser.add_argument("--dedup-log", type=Path, default=paths["dedup"])
    parser.add_argument("--stats", type=Path, default=paths["build_stats"])
    parser.add_argument("--state-dir", default="data/pipeline/state")
    parser.add_argument("--limit", type=int, default=200)
    parser.add_argument("--checkpoint-every", type=int, default=25)
    parser.add_argument("--seed", default="PREreview-staged-pipeline-v1")
    parser.add_argument("--field-policy", choices=["empty", "native", "metadata", "broad"], default="metadata")
    parser.add_argument("--sampling-policy", choices=["hash", "coverage"], default="hash")
    parser.add_argument("--no-resume", action="store_true")
    parser.add_argument("--allow-partial-scan", action="store_true", help="Accept a deliberately partial stage-1 artifact for testing")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    config = PipelineConfig.from_env(
        state_dir=args.state_dir,
        seed=args.seed,
        checkpoint_every=args.checkpoint_every,
        resume=not args.no_resume,
        field_policy=args.field_policy,
        sampling_policy=args.sampling_policy,
        allow_partial_scan=args.allow_partial_scan,
    )
    result = stage3_build_dataset(
        reviews_input=args.reviews,
        metadata_input=args.metadata,
        output=args.output,
        extended_output=args.extended_output,
        audit_output=args.audit,
        dedup_output=args.dedup_log,
        stats_output=args.stats,
        limit=args.limit,
        config=config,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
