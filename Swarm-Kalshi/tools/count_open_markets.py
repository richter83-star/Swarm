#!/usr/bin/env python3
"""
Count the true number of open Kalshi markets via full cursor pagination.

Usage:
    python tools/count_open_markets.py
    python tools/count_open_markets.py --status open --limit 1000
"""

from __future__ import annotations

import argparse
import time
from pathlib import Path
from typing import Any, Dict

import requests
import yaml


def load_base_url(project_root: Path) -> str:
    cfg_path = project_root / "config" / "swarm_config.yaml"
    with cfg_path.open("r", encoding="utf-8") as fh:
        cfg = yaml.safe_load(fh) or {}
    return str(cfg.get("api", {}).get("base_url", "https://api.elections.kalshi.com/trade-api/v2")).rstrip("/")


def main() -> int:
    parser = argparse.ArgumentParser(description="Count open markets via cursor pagination.")
    parser.add_argument("--status", default="open", help="Market status filter (default: open)")
    parser.add_argument("--limit", type=int, default=1000, help="Page size (default: 1000)")
    parser.add_argument("--max-pages", type=int, default=5000, help="Safety cap on pages (default: 5000)")
    args = parser.parse_args()

    project_root = Path(__file__).resolve().parent.parent
    base_url = load_base_url(project_root)
    url = f"{base_url}/markets"

    cursor = None
    total = 0
    pages = 0
    started = time.time()

    while True:
        params: Dict[str, Any] = {"status": args.status, "limit": args.limit}
        if cursor:
            params["cursor"] = cursor

        resp = requests.get(url, params=params, timeout=30)
        resp.raise_for_status()
        body = resp.json()
        markets = body.get("markets", [])
        total += len(markets)
        pages += 1
        cursor = body.get("cursor")

        if pages % 25 == 0:
            elapsed = time.time() - started
            print(f"progress pages={pages} total={total} has_cursor={bool(cursor)} elapsed={elapsed:.1f}s")

        if not cursor or not markets:
            break
        if pages >= args.max_pages:
            print("warning max-pages safety cap reached; total is a lower bound")
            break

    elapsed = time.time() - started
    print(f"final status={args.status} total={total} pages={pages} elapsed={elapsed:.1f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
