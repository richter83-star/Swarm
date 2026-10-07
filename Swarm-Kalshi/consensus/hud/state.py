"""
Assemble the WHALE-OS HUD state from the shadow ledger and reliability store.
Pure function over the databases so it can be tested without a web server.
"""

from __future__ import annotations

import json
import os
import time
from typing import Any, Dict, List, Optional

from consensus.agents import REGISTRY
from consensus.ledger import DecisionLedger
from consensus.reliability import ReliabilityStore
from consensus.settings import WhaleOSSettings

GO_NO_GO_MIN_RESOLVED = 400


def _direction(p: Optional[float], mid: float, band: float) -> str:
    if p is None:
        return "abstain"
    c = p * 100.0
    if c > mid + band:
        return "yes"
    if c < mid - band:
        return "no"
    return "flat"


def _agents_enabled(settings: WhaleOSSettings) -> List[str]:
    out = []
    for name in REGISTRY:
        cfg = settings.agents.get(name) or {}
        if cfg.get("enabled", name not in ("xvenue", "research")):
            out.append(name)
    return out


def build_state(settings: WhaleOSSettings, ledger: DecisionLedger,
                reliability: ReliabilityStore, now: Optional[float] = None) -> Dict[str, Any]:
    now = time.time() if now is None else now
    day_ago = now - 86400
    band = float((settings.consensus or {}).get("direction_deadband_cents", 1.0))
    enabled = _agents_enabled(settings)

    activity = ledger.agent_activity(day_ago)
    rel = reliability.all_stats()
    agents = []
    for name in enabled:
        cls = REGISTRY[name]
        act = activity.get(name, {})
        spark = [act.get("spark", {}).get(i, 0) for i in range(24)]
        st = rel.get(name)
        agents.append({
            "name": name,
            "label": cls.label or name.upper(),
            "family": (settings.agents.get(name) or {}).get("family") or cls.family,
            "tier": cls.tier,
            "runs_24h": act.get("runs", 0),
            "votes_24h": act.get("votes", 0),
            "abstains_24h": act.get("abstains", 0),
            "errors_24h": act.get("errors", 0),
            "avg_latency_ms": act.get("avg_latency_ms", 0.0),
            "last_ts": act.get("last_ts"),
            "last_error": act.get("last_error", ""),
            "spark": spark,
            "resolved_n": st.n if st else 0,
            "brier": round(st.brier_agent, 4) if st and st.brier_agent is not None else None,
            "brier_market": round(st.brier_market, 4) if st and st.brier_market is not None else None,
            "skill": round(st.skill, 4) if st else 0.0,
            "weight": round(st.weight, 3) if st else 1.0,
        })

    recent = ledger.recent(120)
    latest_by_ticker: Dict[str, Dict[str, Any]] = {}
    for d in recent:
        latest_by_ticker.setdefault(d["ticker"], d)
    matrix = []
    for ticker, d in list(latest_by_ticker.items())[:24]:
        votes = {v["agent"]: v.get("p_yes") for v in d.get("votes", [])}
        matrix.append({
            "ticker": ticker,
            "action": d["action"],
            "side": d.get("side"),
            "cells": {a: (_direction(votes[a], d["market_mid_cents"], band) if a in votes else "absent")
                      for a in enabled},
        })

    jev = None
    window = recent[:40]
    if window:
        pick = next((d for d in window if d["action"] == "fire"), None) or \
            max(window, key=lambda d: (d["voting_count"], d["created_at"]))
        cfg = settings.consensus or {}
        jev = {
            "ticker": pick["ticker"],
            "action": pick["action"],
            "side": pick.get("side"),
            "prior": round(pick["market_mid_cents"] / 100.0, 4),
            "posterior": pick["p_yes_pooled"],
            "price_cents": pick.get("price_cents"),
            "order_type": pick.get("order_type"),
            "fee_cents": pick.get("fee_cents_per_contract"),
            "edge_cents": pick.get("edge_cents"),
            "effective_agree": pick.get("effective_agree"),
            "quorum": pick.get("quorum"),
            "voting": pick.get("voting_count"),
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

    summary = ledger.summary().to_dict()
    ci = summary.get("pnl_ci95_cents")
    status: Dict[str, Any] = {}
    if os.path.exists(settings.status_path):
        try:
            with open(settings.status_path, "r", encoding="utf-8") as fh:
                status = json.load(fh)
        except (OSError, ValueError):
            status = {}

    return {
        "mode": settings.mode.upper(),
        "generated_at": now,
        "status": status,
        "summary": summary,
        "go_no_go": {
            "resolved": summary["resolved_fired"],
            "target": GO_NO_GO_MIN_RESOLVED,
            "ci_low": ci[0] if ci else None,
            "pass": bool(ci and ci[0] > 0 and summary["resolved_fired"] >= GO_NO_GO_MIN_RESOLVED),
        },
        "agents": agents,
        "matrix": {"agents": enabled, "rows": matrix},
        "jev": jev,
        "tape": recent[:40],
        "pnl_curve": ledger.pnl_curve()[-500:],
        "series": settings.series,
    }
