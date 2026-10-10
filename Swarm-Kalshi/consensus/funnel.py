"""
Where each decision stopped in Jev's gate sequence.

The aggregator checks its gates in a fixed order (veto, voters, direction,
quorum, quote, edge) and records the first one that failed as a reason
string.  This module turns that back into a stage, so the HUD can draw a
funnel (how many markets cleared each gate) and a Pareto of what blocks
trades most often.  It only reads what the aggregator already wrote; it
never re-decides anything.
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

# Gate order, matching ConsensusEngine.decide.  "fire" means every gate passed.
STOPS: Tuple[str, ...] = ("veto", "voters", "direction", "quorum", "quote", "edge", "fire")

STOP_LABEL = {
    "veto": "rules veto",
    "voters": "too few voters",
    "direction": "no majority side",
    "quorum": "quorum short",
    "quote": "no executable quote",
    "edge": "edge below minimum",
    "other": "unclassified",
}

# Prefix of the reason string the aggregator writes for each failed gate.
_PREFIX = (
    ("voters ", "voters"),
    ("no majority direction", "direction"),
    ("quorum ", "quorum"),
    ("no executable quote", "quote"),
    ("net edge ", "edge"),
)


def stop_of(action: str, reasons: Sequence[str]) -> Tuple[str, str]:
    """(stop key, detail).  detail is the veto name for vetoes, else the gate's reason text."""
    reasons = [str(r) for r in (reasons or [])]
    if action == "fire":
        return "fire", ""
    if action == "veto":
        for r in reasons:
            if r.startswith("veto:"):
                return "veto", r[5:].split(" ", 1)[0]
        return "veto", ""
    for r in reasons:
        for prefix, key in _PREFIX:
            if r.startswith(prefix):
                return key, r
    return "other", reasons[-1] if reasons else ""


def stop_index(key: str) -> int:
    """How many gates a decision cleared (0 = stopped at the veto gate, 6 = fired)."""
    return STOPS.index(key) if key in STOPS else 0


def stop_counts(decisions: Iterable[Any]) -> Dict[str, int]:
    """Count stops over Decision objects or ledger dicts."""
    out: Dict[str, int] = {}
    for d in decisions:
        action = d.get("action") if isinstance(d, dict) else d.action
        reasons = d.get("reasons", ()) if isinstance(d, dict) else d.reasons
        key, _ = stop_of(action, reasons)
        out[key] = out.get(key, 0) + 1
    return out


def funnel_from_counts(counts: Dict[str, int], min_voters: int, quorum: float,
                       min_edge_cents: float) -> List[Dict[str, Any]]:
    """Cumulative stages: how many markets reached each one.
    'other' (a reason string this module doesn't recognise) is left out entirely, so it can
    never show up as a veto drop; summarize() reports it separately."""
    total = sum(c for k, c in counts.items() if k in STOPS)
    stages = [("scanned", "markets scanned"),
              ("no_veto", "passed rules veto"),
              ("voters", f"voters >= {min_voters}"),
              ("direction", "majority side"),
              ("quorum", f"quorum >= {quorum:g}"),
              ("quote", "executable quote"),
              ("fire", f"edge >= {min_edge_cents:g}c \u2192 fire")]
    out = []
    for i, (key, label) in enumerate(stages):
        if i == 0:
            n = total
        else:
            n = sum(c for k, c in counts.items() if k in STOPS and STOPS.index(k) >= i)
        out.append({"key": key, "label": label, "n": n})
    return out


def pareto(stops: Iterable[Tuple[str, str]]) -> List[Dict[str, Any]]:
    """Blocking reasons, largest first, with cumulative share.  Vetoes split by rule name."""
    counts: Dict[str, int] = {}
    for key, detail in stops:
        if key == "fire":
            continue
        label = f"veto: {detail}" if key == "veto" and detail else STOP_LABEL.get(key, key)
        counts[label] = counts.get(label, 0) + 1
    total = sum(counts.values())
    rows, cum = [], 0
    for label, n in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])):
        cum += n
        rows.append({"label": label, "n": n, "share": n / total, "cum": cum / total})
    return rows


def strand(d: Dict[str, Any]) -> Dict[str, Any]:
    """One market's latest decision, trimmed for the field view."""
    key, detail = stop_of(d.get("action", ""), d.get("reasons", ()))
    reasons = d.get("reasons") or []
    return {
        "ticker": d.get("ticker"),
        "action": d.get("action"),
        "side": d.get("side"),
        "stop": key,
        "stage": stop_index(key),
        "detail": detail if key == "veto" else (reasons[-1] if reasons else ""),
        "agree": d.get("effective_agree"),
        "quorum": d.get("quorum"),
        "voters": d.get("voting_count"),
        "edge_cents": d.get("edge_cents") if d.get("price_cents") is not None else None,  # no quote -> no edge
        "p": d.get("p_yes_pooled"),
        "mid": d.get("market_mid_cents"),
        "ts": d.get("created_at"),
    }


def summarize(latest: List[Dict[str, Any]], min_voters: int, quorum: float, min_edge_cents: float,
              cycle_counts: Optional[Dict[str, int]] = None) -> Dict[str, Any]:
    """HUD block: 24h funnel + Pareto over each market's latest decision, plus the last cycle's funnel."""
    stops = [stop_of(d.get("action", ""), d.get("reasons", ())) for d in latest]
    counts: Dict[str, int] = {}
    for key, _ in stops:
        counts[key] = counts.get(key, 0) + 1
    out = {
        "markets": len(latest),
        "unclassified": counts.get("other", 0),
        "funnel": funnel_from_counts(counts, min_voters, quorum, min_edge_cents),
        "pareto": pareto(stops),
        "cycle": None,
    }
    if cycle_counts is not None:
        out["cycle"] = funnel_from_counts(cycle_counts, min_voters, quorum, min_edge_cents)
    return out
