"""Gate funnel / Pareto: classification is driven by the real aggregator's reason strings."""

import json

import pytest

from consensus.aggregator import ConsensusConfig, ConsensusEngine
from consensus.data_sources import snapshot_from_market
from consensus.funnel import STOPS, funnel_from_counts, pareto, stop_of, summarize
from consensus.hud.state import build_state
from consensus.ledger import DecisionLedger
from consensus.schema import AgentVote

from tests.test_whale_os import NOW, StubAgent, make_runner, raw_market

CFG = ConsensusConfig.from_dict({"quorum": 3, "min_voters": 3, "min_edge_cents": 3})


def decide(raw, ps):
    snap = snapshot_from_market(raw, NOW)
    votes = [AgentVote(agent=f"a{i}", ticker=snap.ticker, p_yes=p, family=f"f{i}", created_at=NOW)
             for i, p in enumerate(ps)]
    return ConsensusEngine(CFG, clock=lambda: NOW).decide(snap, votes)


@pytest.mark.parametrize("raw,ps,expect", [
    (raw_market(), [0.70, 0.70, 0.70], "fire"),
    (raw_market(), [0.70], "voters"),
    (raw_market(), [0.45, 0.45, 0.45], "direction"),
    (raw_market(), [0.70, 0.70, 0.20], "quorum"),
    (raw_market(), [0.47, 0.47, 0.47], "edge"),
    (raw_market(bid="0.1000", ask="0.9000"), [0.70, 0.70, 0.70], "veto"),
])
def test_stop_matches_engine_gate(raw, ps, expect):
    d = decide(raw, ps)
    key, detail = stop_of(d.action, d.reasons)
    assert key == expect, (d.action, d.reasons)
    if expect == "veto":
        assert detail == "max_spread"
    # the ledger round-trip (dict with list reasons) classifies the same way
    assert stop_of(d.to_dict()["action"], list(d.to_dict()["reasons"]))[0] == expect


def test_stale_vote_note_does_not_hide_the_gate():
    assert stop_of("hold", ["ignored 1 stale vote(s)", "quorum 2.00 < 3 on yes"]) == \
        ("quorum", "quorum 2.00 < 3 on yes")
    assert stop_of("hold", ["something new"])[0] == "other"


def test_funnel_is_monotonic_and_pareto_sums():
    counts = {"veto": 5, "voters": 10, "direction": 3, "quorum": 7, "quote": 1, "edge": 4, "fire": 2, "other": 1}
    f = funnel_from_counts(counts, 3, 3, 2)
    ns = [s["n"] for s in f]
    assert ns[0] == 32 and ns[1] == 27 and ns[-1] == 2          # "other" never counts as a veto drop
    assert all(a >= b for a, b in zip(ns, ns[1:]))
    rows = pareto([("veto", "max_spread")] * 3 + [("veto", "price_band")] + [("quorum", "")] * 5 + [("fire", "")])
    assert [r["label"] for r in rows] == ["quorum short", "veto: max_spread", "veto: price_band"]
    assert rows[-1]["cum"] == pytest.approx(1.0) and sum(r["n"] for r in rows) == 9


def test_unclassified_and_quote_less_edge():
    s = summarize([{"ticker": "X", "action": "hold", "reasons": ["mystery"], "edge_cents": 0.0}], 3, 3, 3)
    assert s["unclassified"] == 1 and s["funnel"][0]["n"] == 0
    from consensus.funnel import strand
    d = decide(raw_market(), [0.45, 0.45, 0.45]).to_dict()  # no majority side -> no quote, edge never computed
    assert strand(d)["edge_cents"] is None
    d = decide(raw_market(), [0.47, 0.47, 0.47]).to_dict()  # edge gate: quoted, edge is real
    assert strand(d)["edge_cents"] is not None


def test_recorder_writes_when_the_stopping_gate_changes(tmp_path):
    m = raw_market()
    agents = [StubAgent(n, 0.70) for n in ("a", "b", "c")]
    runner, clock = make_runner(tmp_path, [m], agents, consensus={"quorum": 4, "min_voters": 3, "min_edge_cents": 3})
    runner.run_cycle()                                         # 3 agree < quorum 4: hold, side yes
    import dataclasses
    runner.engine = ConsensusEngine(dataclasses.replace(runner.engine.config, quorum=3, min_edge_cents=50),
                                    clock=lambda: clock["t"])
    clock["t"] += 60
    runner.run_cycle()                                         # still hold + yes, but now stopped at the edge gate
    latest = runner.ledger.latest_per_ticker(NOW - 10)
    assert stop_of(latest[0]["action"], latest[0]["reasons"])[0] == "edge"


def test_latest_per_ticker_counts_each_market_once(tmp_path):
    led = DecisionLedger(str(tmp_path / "l.db"))
    a1 = decide(raw_market(), [0.70])                       # voters, superseded below
    led.record(a1)
    led.record(decide(raw_market(), [0.70, 0.70, 0.70]))   # same market, now fires
    led.record(decide(raw_market(ticker="KXHIGHNY-26OCT08-B76.5"), [0.70, 0.70, 0.20]))
    latest = led.latest_per_ticker(NOW - 10)
    assert len(latest) == 2
    s = summarize(latest, 3, 3, 3)
    assert s["markets"] == 2 and s["funnel"][-1]["n"] == 1
    assert [r["label"] for r in s["pareto"]] == ["quorum short"]
    assert led.latest_per_ticker(NOW + 10) == []


def test_cycle_status_and_hud_gates(tmp_path):
    m = raw_market()
    runner, clock = make_runner(tmp_path, [m, raw_market(ticker="KXHIGHNY-26OCT08-B76.5", bid="0.1000", ask="0.9000")],
                                [StubAgent(n, 0.70) for n in ("book", "whales", "weather")])
    report = runner.run_cycle()
    assert report["stops"] == {"fire": 1, "veto": 1}
    status = json.load(open(runner.s.status_path))
    assert status["stops"] == {"fire": 1, "veto": 1}
    g = build_state(runner.s, runner.ledger, runner.reliability, now=clock["t"])["gates"]
    assert g["markets"] == 2 and g["funnel"][0]["n"] == 2 and g["funnel"][-1]["n"] == 1
    assert g["cycle"][0]["n"] == 2 and g["pareto"][0]["label"] == "veto: max_spread"
    st = {s["ticker"]: s for s in g["strands"]}
    assert st[m["ticker"]]["stop"] == "fire" and st[m["ticker"]]["stage"] == len(STOPS) - 1
    assert st["KXHIGHNY-26OCT08-B76.5"]["stage"] == 0
