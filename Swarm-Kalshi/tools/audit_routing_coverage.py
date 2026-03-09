"""
Audit specialist routing coverage on live open markets.

Examples:
    python tools/audit_routing_coverage.py
    python tools/audit_routing_coverage.py --sample-size 100 --max-pages 3
"""

from __future__ import annotations

import argparse
import random
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Dict, Iterable, List, Tuple

import requests
import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from swarm.market_router import MarketRouter  # noqa: E402


BOT_CONFIG_FILES = {
    "sentinel": "config/sentinel_config.yaml",
    "oracle": "config/oracle_config.yaml",
    "pulse": "config/pulse_config.yaml",
    "vanguard": "config/vanguard_config.yaml",
}


def load_yaml(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {}
    with open(path, "r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    return data if isinstance(data, dict) else {}


def load_router_and_configs() -> Tuple[MarketRouter, Dict[str, Any], str]:
    swarm_cfg = load_yaml(PROJECT_ROOT / "config" / "swarm_config.yaml")
    if not swarm_cfg:
        swarm_cfg = load_yaml(PROJECT_ROOT / "config" / "swarm_config.yaml.example")
    default_bot = (
        (swarm_cfg.get("swarm", {}) or {}).get("unassigned_market_bot", "vanguard")
        if isinstance(swarm_cfg, dict)
        else "vanguard"
    )
    bot_cfgs: Dict[str, Dict[str, Any]] = {}
    for bot_name, rel in BOT_CONFIG_FILES.items():
        bot_cfgs[bot_name] = load_yaml(PROJECT_ROOT / rel)
    router = MarketRouter(bot_cfgs, default_bot=default_bot)
    return router, (swarm_cfg.get("api", {}) or {}), default_bot


def load_routing_fallback_cfg() -> Dict[str, Any]:
    defaults = {
        "series_prefix_to_category": {},
        "title_keywords": {},
        "event_patterns": {},
    }
    path = PROJECT_ROOT / "config" / "routing_config.yaml"
    loaded = load_yaml(path)
    out = {**defaults, **loaded}
    return out


def infer_category(
    market: Dict[str, Any],
    cfg: Dict[str, Any],
) -> Tuple[str, str]:
    category = str(market.get("category", "") or "").strip().lower()
    if category:
        return category, "category"

    ticker = str(market.get("ticker", "") or "").strip().upper()
    event_ticker = str(market.get("event_ticker", "") or "").strip().lower()
    title = str(market.get("title", "") or "").strip().lower()

    for prefix, mapped in (cfg.get("series_prefix_to_category", {}) or {}).items():
        if ticker.startswith(str(prefix).upper()):
            return str(mapped).lower(), "series_prefix"

    for kw, mapped in (cfg.get("title_keywords", {}) or {}).items():
        if str(kw).lower() in title:
            return str(mapped).lower(), "title_keyword"

    for pattern, mapped in (cfg.get("event_patterns", {}) or {}).items():
        if str(pattern).lower() in event_ticker:
            return str(mapped).lower(), "event_pattern"

    return "", "unknown"


def fetch_open_markets(
    base_url: str,
    max_pages: int,
    limit: int,
) -> List[Dict[str, Any]]:
    base = base_url.rstrip("/")
    url = f"{base}/markets"
    params = {"status": "open", "limit": max(1, min(1000, int(limit)))}
    out: List[Dict[str, Any]] = []
    cursor = None

    for _ in range(max_pages):
        q = dict(params)
        if cursor:
            q["cursor"] = cursor
        resp = requests.get(url, params=q, timeout=20)
        resp.raise_for_status()
        payload = resp.json()
        rows = payload.get("markets", []) if isinstance(payload, dict) else []
        if not isinstance(rows, list):
            rows = []
        out.extend([r for r in rows if isinstance(r, dict)])
        cursor = payload.get("cursor", "") if isinstance(payload, dict) else ""
        if not cursor or not rows:
            break
    return out


def pct(part: int, total: int) -> float:
    return (part / total * 100.0) if total else 0.0


def ordered(counter: Counter) -> Iterable[Tuple[str, int]]:
    return sorted(counter.items(), key=lambda kv: kv[1], reverse=True)


def main() -> None:
    parser = argparse.ArgumentParser(description="Audit routing coverage for specialist bots.")
    parser.add_argument("--sample-size", type=int, default=100)
    parser.add_argument("--max-pages", type=int, default=3)
    parser.add_argument("--page-limit", type=int, default=500)
    parser.add_argument("--seed", type=int, default=7)
    args = parser.parse_args()

    router, api_cfg, default_bot = load_router_and_configs()
    fallback_cfg = load_routing_fallback_cfg()

    base_url = str(api_cfg.get("base_url", "https://api.elections.kalshi.com/trade-api/v2")).strip()
    markets = fetch_open_markets(base_url, max_pages=args.max_pages, limit=args.page_limit)
    if not markets:
        raise SystemExit("No markets fetched.")

    rng = random.Random(args.seed)
    sample_size = max(1, min(int(args.sample_size), len(markets)))
    sample = rng.sample(markets, sample_size)

    source_counts: Counter = Counter()
    bot_counts: Counter = Counter()
    unresolved = 0

    for m in sample:
        inferred_category, source = infer_category(m, fallback_cfg)
        source_counts[source] += 1
        routed_input = dict(m)
        if inferred_category:
            routed_input["category"] = inferred_category
        bot = router.route(routed_input)
        bot_counts[bot] += 1
        if source == "unknown" and bot == default_bot:
            unresolved += 1

    print(f"Fetched open markets: {len(markets)}")
    print(f"Audited sample: {sample_size}")
    print("")
    print("Category Source Distribution")
    for source, count in ordered(source_counts):
        print(f"- {source}: {count} ({pct(count, sample_size):.1f}%)")

    print("")
    print("Bot Routing Distribution")
    for bot, count in ordered(bot_counts):
        print(f"- {bot}: {count} ({pct(count, sample_size):.1f}%)")

    print("")
    print(f"Unresolved -> default bot ({default_bot}): {unresolved} ({pct(unresolved, sample_size):.1f}%)")


if __name__ == "__main__":
    main()
