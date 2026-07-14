#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from pipeline_stages import default_paths, stage4_validate_dataset


def main() -> None:
    paths = default_paths()
    parser = argparse.ArgumentParser(description="Stage 4: validate the final PREreview CSV.")
    parser.add_argument("--input", type=Path, default=paths["csv"])
    parser.add_argument("--report", type=Path, default=paths["validation"])
    parser.add_argument("--audit", type=Path, default=paths["audit"])
    parser.add_argument("--expected", type=int, default=200)
    args = parser.parse_args()
    result = stage4_validate_dataset(
        csv_input=args.input,
        report_output=args.report,
        expected=args.expected,
        audit_input=args.audit,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    sys.exit(0 if result["valid"] else 2)


if __name__ == "__main__":
    main()
