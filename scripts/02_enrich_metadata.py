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

from pipeline_stages import PipelineConfig, default_paths, stage2_resolve_metadata


def boolean_switch(parser: argparse.ArgumentParser, name: str, default: bool, help_text: str) -> None:
    group = parser.add_mutually_exclusive_group()
    group.add_argument(f"--use-{name}", dest=f"use_{name}", action="store_true", help=help_text)
    group.add_argument(f"--no-use-{name}", dest=f"use_{name}", action="store_false")
    parser.set_defaults(**{f"use_{name}": default})


def main() -> None:
    paths = default_paths()
    parser = argparse.ArgumentParser(description="Stage 2: resolve metadata for DOI/arXiv targets from stage 1.")
    parser.add_argument("--reviews", type=Path, default=paths["reviews"])
    parser.add_argument("--output", type=Path, default=paths["metadata"])
    parser.add_argument("--stats", type=Path, default=paths["metadata_stats"])
    parser.add_argument("--state-dir", default="data/pipeline/state")
    parser.add_argument("--delay", type=float, default=0.05)
    parser.add_argument("--checkpoint-every", type=int, default=25)
    parser.add_argument("--retry-missing", action="store_true")
    parser.add_argument("--allow-partial-scan", action="store_true", help="Accept a deliberately partial stage-1 artifact for testing")
    parser.add_argument("--refresh-metadata", action="store_true")
    parser.add_argument("--no-resume", action="store_true")
    boolean_switch(parser, "datacite", True, "Use DataCite DOI metadata.")
    boolean_switch(parser, "crossref", True, "Use Crossref DOI metadata.")
    boolean_switch(parser, "openalex", False, "Use OpenAlex as a final optional fallback.")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    config = PipelineConfig.from_env(
        state_dir=args.state_dir,
        delay=args.delay,
        checkpoint_every=args.checkpoint_every,
        resume=not args.no_resume,
        refresh_metadata=args.refresh_metadata,
        allow_partial_scan=args.allow_partial_scan,
        use_datacite=args.use_datacite,
        use_crossref=args.use_crossref,
        use_openalex=args.use_openalex,
    )
    result = stage2_resolve_metadata(
        reviews_input=args.reviews,
        output=args.output,
        stats_output=args.stats,
        config=config,
        retry_missing=args.retry_missing,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
