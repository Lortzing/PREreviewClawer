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

from pipeline_stages import PipelineConfig, default_paths, stage1_collect_reviews


def main() -> None:
    paths = default_paths()
    parser = argparse.ArgumentParser(description="Stage 1: collect PREreview/Zenodo review data only.")
    parser.add_argument("--output", type=Path, default=paths["reviews"])
    parser.add_argument("--stats", type=Path, default=paths["reviews_stats"])
    parser.add_argument("--state-dir", default="data/pipeline/state")
    parser.add_argument("--max-pages", type=int, default=100)
    parser.add_argument("--delay", type=float, default=0.05)
    parser.add_argument("--refresh-zenodo", action="store_true")
    parser.add_argument("--no-resume", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    config = PipelineConfig.from_env(
        state_dir=args.state_dir,
        delay=args.delay,
        resume=not args.no_resume,
        refresh_zenodo=args.refresh_zenodo,
    )
    result = stage1_collect_reviews(
        output=args.output,
        stats_output=args.stats,
        max_pages=args.max_pages,
        config=config,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
