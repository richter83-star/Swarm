"""
Plain-text WHALE-OS status report (read-only).

    python -m consensus.report                 # human-readable
    python -m consensus.report --json          # machine-readable

Shows whether the runner is alive, what Jev has decided, the go/no-go gate,
per-agent reliability and calibration coverage.  All P&L is SHADOW
(hypothetical, one contract per fired market, maker fills assumed).
"""

from __future__ import annotations

import argparse
import json
import time
from typing import Any, Dict, List, Optional

from consensus.hud.state import build_state
from consensus.ledger import DecisionLedger
from consensus.reliability import ReliabilityStore
from consensus.settings import WhaleOSSettings, load_settings


def _c(v: Optional[float], d: int = 1) -> str:
    return "-" if v is None else f"{v:+.{d}f}c"


def summarize(settings: WhaleOSSettings, now: Optional[float] = None) -> Dict[str, Any]:
    now = time.time() if now is None else now
    ledger = DecisionLedger(settings.ledger_path, mode=settings.mode)
    reliability = ReliabilityStore(settings.reliability_path)
    try:
        st = build_state(settings, ledger, reliability, now=now)
    finally:
        ledger.close()
    status = st["status"] or {}
    started = status.get("cycle_started")
    period = float(status.get("cycle_period_s") or settings.cycle_seconds)
    age = (now - float(started)) if started else None
    return {
        "mode": st["mode"],
        "runner": {"last_cycle_age_s": round(age, 0) if age is not None else None,
                   # a cycle can take a few minutes; three periods without one means it is down
                   "healthy": age is not None and age < 3 * period + 300,
                   "markets": status.get("markets"), "counts": status.get("counts"),
                   "errors": status.get("errors")},
        "summary": st["summary"],
        "go_no_go": st["go_no_go"],
        "rule": st["rule"],
        "calibration": st["calibration"],
        "agents": [{k: a.get(k) for k in ("name", "enabled", "votes_24h", "errors_24h",
                                          "avg_latency_ms", "resolved_n", "brier", "skill", "weight")}
                   for a in st["agents"]],
    }


def render(r: Dict[str, Any]) -> str:
    s, g, run, cal = r["summary"], r["go_no_go"], r["runner"], r["calibration"]
    ci = s.get("pnl_ci95_cents")
    lines: List[str] = [
        f"WHALE-OS {r['mode']}  (hypothetical P&L, no orders are placed)",
        "",
        f"runner     {'OK' if run['healthy'] else 'no cycle yet' if run['last_cycle_age_s'] is None else 'DOWN?'}  last cycle "
        f"{'-' if run['last_cycle_age_s'] is None else str(int(run['last_cycle_age_s'])) + 's ago'}"
        f"  markets {run['markets']}  counts {run['counts']}  errors {run['errors']}",
        f"decisions  {s['decisions']} on {s['markets']} markets   fired {s['fired']}  "
        f"held {s['held']}  vetoed {s['vetoed']}",
        f"settled    {s['resolved_fired']} fired markets   win rate "
        f"{'-' if s['win_rate'] is None else format(s['win_rate'] * 100, '.1f') + '%'}   "
        f"mean {_c(s['mean_pnl_cents'], 2)} vs expected {_c(s['mean_expected_edge_cents'], 2)}   "
        f"total {_c(s['total_pnl_cents'])}",
        f"95% CI     {'-' if not ci else f'{ci[0]:+.1f}c to {ci[1]:+.1f}c'} per contract "
        f"(clustered by event)",
        f"go/no-go   {'PASS' if g['pass'] else 'not yet'}  ({g['resolved']}/{g['target']} settled markets, "
        f"{g['events']}/{g['target_events']} independent events, needs CI low > 0)   "
        f"days running {g['days_running']}",
        f"rule       fire at {r['rule']['quorum']} families, >= {r['rule']['min_voters']} voters, "
        f"edge >= {r['rule']['min_edge_cents']}c net of fees",
        f"calibration {'present' if cal['exists'] else 'MISSING'}  {len(cal['series'])} series  "
        f"{cal['samples']} samples  age {'-' if cal['age_days'] is None else str(cal['age_days']) + 'd'}"
        + (f"  missing: {', '.join(cal['missing'])}" if cal["missing"] else ""),
        "",
        f"{'agent':<10}{'on':<4}{'votes24h':>9}{'err':>6}{'ms':>7}{'settled':>9}{'brier':>8}{'skill':>8}{'weight':>8}",
    ]
    for a in r["agents"]:
        lines.append(
            f"{a['name']:<10}{'y' if a['enabled'] else '-':<4}{a['votes_24h'] or 0:>9}{a['errors_24h'] or 0:>6}"
            f"{int(a['avg_latency_ms'] or 0):>7}{a['resolved_n'] or 0:>9}"
            f"{('-' if not a['resolved_n'] else format(a['brier'], '.3f')):>8}"
            f"{('-' if not a['resolved_n'] else format(a['skill'] * 100, '+.1f') + '%'):>8}"
            f"{a['weight'] if a['weight'] is not None else '-':>8}")
    return "\n".join(lines)


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="WHALE-OS status report (read-only)")
    ap.add_argument("--config", default=None)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    r = summarize(load_settings(args.config))
    print(json.dumps(r, indent=2, default=str) if args.json else render(r))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
