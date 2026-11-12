#!/usr/bin/env python3
"""CLI: run a hunt and print the summary.

Usage:
    python scripts/run_hunt.py --mode dry [--max-keywords 10]
    python scripts/run_hunt.py --mode live [--max-keywords 10]  # needs PLH_LIVE=1
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend import setup_logging
from backend.runner import run_hunt


def main() -> int:
    parser = argparse.ArgumentParser(description="Amazon PL Hunter v2 — run a hunt")
    parser.add_argument("--mode", choices=["dry", "live"], default="dry")
    parser.add_argument("--max-keywords", type=int, default=None)
    args = parser.parse_args()
    setup_logging()
    try:
        summary = run_hunt(mode=args.mode, max_keywords=args.max_keywords)
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
