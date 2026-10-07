"""
Assemble the WHALE-OS HUD state from the shadow ledger and reliability store.
Pure function over the databases so it can be tested without a web server.
"""

from __future__ import annotations

import hashlib
import json
import os
import random
import time
from typing import Any, Dict, List, Optional

from consensus.agents import REGISTRY
from consensus.ledger import DecisionLedger
from consensus.reliability import ReliabilityStore
from consensus.settings import WhaleOSSettings

GO_NO_GO_MIN_RESOLVED = 400
FAN_MIN_RESOLVED = 10
FAN_PATHS = 120

# Which public sources feed which agent (drawn as edges on the FIELD).
AGENT_SOURCES = {
    "book": ["kalshi /orderbook"],
    "whales": ["kalshi /trades"],
    "weather": ["open-meteo /forecast"],
    "crypto": ["coinbase /ticker", "coinbase /candles"],
    "history": ["kalshi /candlesticks"],
    "xvenue": ["polymarket /markets"],
    "research": ["gemini /search"],
}
ALL_SOURCES = ["kalshi /markets", "kalshi /market", "kalshi /orderbook", "kalshi /trades",
               "kalshi /candlesticks", "open-meteo /forecast", "coinbase /ticker",
               "coinbase /candles", "polymarket /markets", "gemini /search"]


def _direction(p: Optional[float], mid: float, band: float) -> str:
    if p is None:
        return "abstain"
    c = p * 100.0
    if c > mid + band:
        return "yes"
    if c < mid - band:
        return "no"
    return "flat"


def _spark24(spark: Dict[int, int]) -> List[int]:
    out = [0] * 24
    for k, n in spark.items():
        out[min(int(k), 23)] += n
    return out


def _agents_enabled(settings: WhaleOSSettings) -> List[str]:
    out = []
    for name in REGISTRY:
        cfg = settings.agents.get(name) or {}
        if cfg.get("enabled", name not in ("xvenue", "research")):
            out.append(name)
    return out


def _fan(pnls: List[float]) -> Dict[str, Any]:
    """Bootstrap cumulative P&L paths from resolved first-fire results."""
    n = len(pnls)
    if n < FAN_MIN_RESOLVED:
        return {"ready": False, "need": FAN_MIN_RESOLVED, "have": n}
    rng = random.Random(n * 7919 + int(sum(pnls) * 100))
    paths, finals = [], []
    for _ in range(FAN_PATHS):
        total, path = 0.0, [0.0]
        for _ in range(n):
            total += pnls[rng.randrange(n)]
            path.append(round(total, 2))
        paths.append(path)
        finals.append(total)
    finals.sort()
    pick = lambda q: finals[min(len(finals) - 1, int(q * len(finals)))]  # noqa: E731
    actual, total = [0.0], 0.0
    for p in pnls:
        total += p
        actual.append(round(total, 2))
    return {"ready": True, "paths": paths, "actual": actual, "p5": pick(0.05),
            "median": pick(0.5), "p95": pick(0.95),
            "p_profit": sum(1 for f in finals if f > 0) / len(finals)}


def _log_lines(recent: List[Dict[str, Any]], runs: List[Dict[str, Any]], band: float) -> List[Dict[str, Any]]:
    lines: List[Dict[str, Any]] = []
    for d in recent[:30]:
        verb = {"fire": "fire", "veto": "veto"}.get(d["action"], "hold")
        reason = (d.get("reasons") or [""])[0]
        lines.append({"ts": d["created_at"], "kind": verb, "agent": "jev", "ticker": d["ticker"],
                      "text": f"{d['effective_agree']:.1f}/{d['quorum']:.0f} - {reason}"})
        for v in d.get("votes", []):
            if v.get("p_yes") is None:
                continue
            dirn = _direction(v["p_yes"], d["market_mid_cents"], band)
            kind = "vote" if dirn in ("yes", "no") else "read"
            lines.append({"ts": v.get("created_at", d["created_at"]), "kind": kind, "agent": v["agent"],
                          "ticker": d["ticker"],
                          "text": f"{v['p_yes'] * 100:.1f} vs {d['market_mid_cents']:.1f}"
                                  + (f" - {v['rationale']}" if v.get("rationale") else "")})
    for r in runs:
        if r["status"] == "error":
            lines.append({"ts": r["ts"], "kind": "flag", "agent": r["agent"], "ticker": r["ticker"],
                          "text": r["error"], "latency_ms": r["latency_ms"]})
    lines.sort(key=lambda x: x["ts"])
    return lines[-80:]


def _calibration(settings: WhaleOSSettings, now: float) -> Dict[str, Any]:
    """Is the HISTORY table present, how fresh, which series does it cover?"""
    path = str(((settings.agents or {}).get("history") or {}).get("table_path") or "")
    out: Dict[str, Any] = {"exists": False, "series": [], "missing": list(settings.series),
                           "age_days": None, "samples": 0}
    if not path or not os.path.exists(path):
        return out
    try:
        with open(path, "r", encoding="utf-8") as fh:
            table = json.load(fh)
    except (OSError, ValueError):
        return out
    series = table.get("series") or {}
    built = [float(v.get("built_at") or 0) for v in series.values() if v.get("built_at")]
    out.update(exists=True, series=sorted(series),
               missing=[x for x in settings.series if x not in series],
               age_days=round((now - min(built)) / 86400, 1) if built else None,
               samples=sum(int(l.get("n") or 0) for v in series.values()
                           for l in (v.get("leads") or {}).values()))
    return out


def build_state(settings: WhaleOSSettings, ledger: DecisionLedger,
                reliability: ReliabilityStore, now: Optional[float] = None) -> Dict[str, Any]:
    now = time.time() if now is None else now
    day_ago = now - 86400
    band = float((settings.consensus or {}).get("direction_deadband_cents", 1.0))
    cfg = settings.consensus or {}
    enabled = _agents_enabled(settings)

    status: Dict[str, Any] = {}
    if os.path.exists(settings.status_path):
        try:
            with open(settings.status_path, "r", encoding="utf-8") as fh:
                status = json.load(fh)
        except (OSError, ValueError):
            status = {}
    src_by_name = {s["name"]: s for s in status.get("sources", [])}

    activity = ledger.agent_activity(day_ago)
    rel = reliability.all_stats()
    agents = []
    for name, cls in REGISTRY.items():
        act = activity.get(name, {})
        st = rel.get(name)
        agents.append({
            "name": name,
            "label": cls.label or name.upper(),
            "family": (settings.agents.get(name) or {}).get("family") or cls.family,
            "tier": cls.tier,
            "enabled": name in enabled,
            "sources": AGENT_SOURCES.get(name, []),
            "runs_24h": act.get("runs", 0),
            "votes_24h": act.get("votes", 0),
            "abstains_24h": act.get("abstains", 0),
            "errors_24h": act.get("errors", 0),
            "avg_latency_ms": act.get("avg_latency_ms", 0.0),
            "last_ts": act.get("last_ts"),
            "last_error": act.get("last_error", ""),
            "spark": _spark24(act.get("spark", {})),
            "resolved_n": st.n if st else 0,
            "brier": round(st.brier_agent, 4) if st and st.brier_agent is not None else None,
            "skill": round(st.skill, 4) if st else 0.0,
            "weight": round(st.weight, 3) if st else 1.0,
        })

    sources = []
    for name in ALL_SOURCES:
        s = src_by_name.get(name, {})
        sources.append({"name": name, "calls": s.get("calls", 0), "errors": s.get("errors", 0),
                        "avg_ms": s.get("avg_ms", 0.0), "last_ms": s.get("last_ms", 0),
                        "samples": s.get("samples", []),
                        "agents": [a for a, srcs in AGENT_SOURCES.items() if name in srcs]})

    recent = ledger.recent(160)
    latest_by_ticker: Dict[str, Dict[str, Any]] = {}
    for d in recent:
        latest_by_ticker.setdefault(d["ticker"], d)

    history = []
    for d in reversed(recent[:48]):
        votes = {v["agent"]: v.get("p_yes") for v in d.get("votes", [])}
        history.append({"ticker": d["ticker"], "action": d["action"],
                        "cells": {a: (_direction(votes[a], d["market_mid_cents"], band) if a in votes else "absent")
                                  for a in enabled}})

    jev = None
    window = recent[:40]
    if window:
        pick = next((d for d in window if d["action"] == "fire"), None) or \
            max(window, key=lambda d: (d["voting_count"], d["created_at"]))
        side = pick.get("side")
        breakeven = None
        if pick.get("price_cents"):
            be = (pick["price_cents"] + (pick.get("fee_cents_per_contract") or 0)) / 100.0
            breakeven = be if side == "yes" else 1.0 - be
        jev = {
            "ticker": pick["ticker"], "action": pick["action"], "side": side,
            "prior": round(pick["market_mid_cents"] / 100.0, 4),
            "posterior": pick["p_yes_pooled"],
            "breakeven": breakeven,
            "price_cents": pick.get("price_cents"), "order_type": pick.get("order_type"),
            "fee_cents": pick.get("fee_cents_per_contract"), "edge_cents": pick.get("edge_cents"),
            "effective_agree": pick.get("effective_agree"), "quorum": pick.get("quorum"),
            "voting": pick.get("voting_count"), "created_at": pick.get("created_at"),
            "reasons": pick.get("reasons", []),
            "gates": [
                {"name": "rules veto", "ok": not any(v.get("blocked") for v in pick.get("vetoes", []))},
                {"name": f"voters >= {cfg.get('min_voters', 4)}",
                 "ok": pick.get("voting_count", 0) >= int(cfg.get("min_voters", 4))},
                {"name": f"quorum >= {pick.get('quorum')}",
                 "ok": (pick.get("effective_agree") or 0) + 1e-9 >= float(pick.get("quorum") or 0)},
                {"name": f"edge >= {cfg.get('min_edge_cents', 2.0)}c",
                 "ok": (pick.get("edge_cents") or 0) + 1e-9 >= float(cfg.get("min_edge_cents", 2.0))},
            ],
            "votes": pick.get("votes", []),
            "weights": pick.get("weights", {}),
        }

    inspector = None
    for d in recent[:20]:
        voted = [v for v in d.get("votes", []) if v.get("p_yes") is not None]
        if voted:
            v = max(voted, key=lambda x: x.get("latency_ms", 0))
            payload = json.dumps(d, sort_keys=True, default=str).encode()
            inspector = {"ticker": d["ticker"], "agent": v["agent"], "family": v.get("family"),
                         "latency_ms": v.get("latency_ms", 0), "p_yes": v["p_yes"],
                         "sources": v.get("sources", []), "rationale": v.get("rationale", ""),
                         "payload_bytes": len(payload),
                         "sha": hashlib.sha256(payload).hexdigest()[:16],
                         "hex": payload[:48].hex()}
            break

    summary = ledger.summary().to_dict()
    ci = summary.get("pnl_ci95_cents")
    curve = ledger.pnl_curve()
    hourly = ledger.hourly_counts(day_ago)
    votes_hourly = [sum(a["spark"][i] for a in agents) for i in range(24)]
    fired = [d for d in recent if d["action"] == "fire"]
    seen, trades = set(), []
    for d in fired:
        if d["ticker"] in seen:
            continue
        seen.add(d["ticker"])
        agreeing = [v["agent"] for v in d.get("votes", [])
                    if v.get("p_yes") is not None and _direction(v["p_yes"], d["market_mid_cents"], band) == d.get("side")]
        trades.append({"ticker": d["ticker"], "side": d.get("side"), "price_cents": d.get("price_cents"),
                       "p": d["p_yes_pooled"], "agree": f"{d['effective_agree']:.1f}/{d['quorum']:.0f}",
                       "agents": agreeing, "edge_cents": d["edge_cents"], "pnl_cents": d.get("pnl_cents"),
                       "outcome": d.get("outcome"), "created_at": d["created_at"]})

    first_ts = min((d["created_at"] for d in recent), default=None)
    return {
        "mode": settings.mode.upper(),
        "generated_at": now,
        "status": {k: v for k, v in status.items() if k != "sources"},
        "summary": summary,
        "go_no_go": {
            "resolved": summary["resolved_fired"], "target": GO_NO_GO_MIN_RESOLVED,
            "ci_low": ci[0] if ci else None,
            "pass": bool(ci and ci[0] > 0 and summary["resolved_fired"] >= GO_NO_GO_MIN_RESOLVED),
            "days_running": round((now - first_ts) / 86400, 1) if first_ts else 0.0,
        },
        "rule": {"quorum": cfg.get("quorum", 4), "min_voters": cfg.get("min_voters", 4),
                 "min_edge_cents": cfg.get("min_edge_cents", 2.0)},
        "agents": agents,
        "enabled": enabled,
        "sources": sources,
        "matrix": {"agents": enabled, "history": history,
                   "rows": [{"ticker": t, "action": d["action"], "side": d.get("side")}
                            for t, d in list(latest_by_ticker.items())[:24]]},
        "jev": jev,
        "inspector": inspector,
        "tape": recent[:40],
        "trades": trades[:14],
        "log": _log_lines(recent, ledger.recent_runs(60), band),
        "pnl_curve": curve[-500:],
        "fan": _fan([c["pnl"] for c in curve]),
        "hourly": {"votes": votes_hourly, **hourly},
        "series": settings.series,
        "calibration": _calibration(settings, now),
        "markets_tape": [{"ticker": t, "mid": d["market_mid_cents"], "action": d["action"]}
                         for t, d in list(latest_by_ticker.items())[:40]],
    }
