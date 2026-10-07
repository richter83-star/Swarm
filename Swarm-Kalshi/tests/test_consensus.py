"""Tests for the consensus core (fees, vetoes, reliability, engine, ledger)."""

import pytest

from consensus import (
    AgentVote,
    ConsensusConfig,
    ConsensusEngine,
    DecisionLedger,
    MarketSnapshot,
    ReliabilityStore,
    VetoConfig,
    VetoRules,
    breakeven_probability,
    fee_per_contract_cents,
    trade_fee_cents,
)
from consensus.fees import MAKER_RATE

NOW = 1_800_000_000.0


def clock():
    return NOW


def market(**kw):
    base = dict(
        ticker="KXHIGHNY-26OCT08-T70",
        series_ticker="KXHIGHNY",
        yes_bid=48,
        yes_ask=52,
        no_bid=48,
        no_ask=52,
        open_interest=500,
        liquidity_cents=50_000,
        volume_24h=1_000,
        hours_to_close=6.0,
    )
    base.update(kw)
    return MarketSnapshot(**base)


def vote(agent, p, ticker="KXHIGHNY-26OCT08-T70", family="", confidence=0.5, age=0.0):
    return AgentVote(agent=agent, ticker=ticker, p_yes=p, family=family,
                     confidence=confidence, created_at=NOW - age)


def engine(**cfg):
    return ConsensusEngine(ConsensusConfig(**cfg), clock=clock)


# --------------------------------------------------------------- fees --

class TestFees:
    def test_taker_fee_matches_published_example(self):
        assert trade_fee_cents(100, 50) == 175           # $1.75

    def test_single_contract_rounds_up_to_a_cent(self):
        assert trade_fee_cents(1, 95) == 1

    def test_maker_fee_quarter_rate(self):
        assert trade_fee_cents(100, 50, MAKER_RATE) == 44   # 43.75 -> 44

    def test_zero_contracts_is_free(self):
        assert trade_fee_cents(0, 50) == 0

    def test_invalid_price_rejected(self):
        with pytest.raises(ValueError):
            trade_fee_cents(1, 0)
        with pytest.raises(ValueError):
            trade_fee_cents(1, 100)

    def test_per_contract_and_breakeven(self):
        fee = fee_per_contract_cents(100, 50)
        assert fee == pytest.approx(1.75)
        assert breakeven_probability(50, fee) == pytest.approx(0.5175)


# ------------------------------------------------------------- vetoes --

class TestVetoes:
    def blocked(self, m, **cfg):
        return {r.name for r in VetoRules(VetoConfig(**cfg)).evaluate(m) if r.blocked}

    def test_healthy_market_passes(self):
        assert self.blocked(market()) == set()

    def test_wide_spread_blocked(self):
        assert "max_spread" in self.blocked(market(yes_bid=40, yes_ask=55))

    def test_one_sided_book_blocked(self):
        assert "two_sided_book" in self.blocked(market(yes_bid=0, yes_ask=52))

    def test_combo_series_blocked_by_default(self):
        m = market(ticker="KXMVECROSSCATEGORY-X", series_ticker="KXMVECROSSCATEGORY")
        assert "blocked_series" in self.blocked(m)

    def test_price_band(self):
        assert "price_band" in self.blocked(market(yes_bid=97, yes_ask=99, no_bid=1, no_ask=3))

    def test_too_close_to_expiry(self):
        assert "time_to_close" in self.blocked(market(hours_to_close=0.1))

    def test_liquidity_floor_only_when_configured(self):
        m = market(open_interest=5)
        assert "min_open_interest" not in self.blocked(m)
        assert "min_open_interest" in self.blocked(m, min_open_interest=100)

    def test_ambiguous_rules_terms(self):
        m = market(rules_text="Settles at the sole discretion of the exchange")
        assert "ambiguous_rules" in self.blocked(m, blocked_rule_terms=("sole discretion",))

    def test_from_dict_ignores_unknown_keys(self):
        cfg = VetoConfig.from_dict({"max_spread_cents": 3, "bogus": 1,
                                    "blocked_series_prefixes": ["KXA", "KXB"]})
        assert cfg.max_spread_cents == 3
        assert cfg.blocked_series_prefixes == ("KXA", "KXB")


# ------------------------------------------------------------- engine --

class TestEngine:
    def test_four_of_six_agree_fires_yes(self):
        votes = [vote(a, 0.75) for a in "abcd"] + [vote("e", None), vote("f", None)]
        d = engine().decide(market(), votes)
        assert d.action == "fire", d.reasons
        assert d.side == "yes"
        assert d.agree_count == 4
        assert d.abstain_count == 2
        assert d.order_type == "taker" and d.price_cents == 52       # default: taker-priced
        assert d.edge_cents >= 2.0
        m = engine(prefer_maker=True).decide(market(), votes)       # live execution option
        assert m.order_type == "maker" and m.price_cents == 49

    def test_three_agree_holds_on_quorum(self):
        votes = [vote(a, 0.75) for a in "abc"] + [vote("d", 0.50), vote("e", 0.50)]
        d = engine().decide(market(), votes)
        assert d.action == "hold"
        assert any("quorum" in r for r in d.reasons)

    def test_too_few_voters_holds(self):
        votes = [vote(a, 0.9) for a in "abc"] + [vote("d", None)]
        d = engine().decide(market(), votes)
        assert d.action == "hold"
        assert any("voters" in r for r in d.reasons)

    def test_correlated_family_counts_once(self):
        votes = [vote(a, 0.8, family="news") for a in "abcd"]
        d = engine().decide(market(), votes)
        assert d.action == "hold"
        assert d.effective_agree == pytest.approx(1.0)

    def test_deadband_member_does_not_dilute_its_family(self):
        # book sits at mid (no opinion), whales leans YES: the book family counts as a full YES
        votes = [vote("book", 0.505, family="book"), vote("whales", 0.60, family="book"),
                 vote("weather", 0.70), vote("history", 0.60)]
        d = engine().decide(market(), votes)
        assert d.side == "yes" and d.effective_agree == pytest.approx(3.0)

    def test_split_family_counts_half_each_way(self):
        votes = [vote("book", 0.40, family="book"), vote("whales", 0.60, family="book"),
                 vote("weather", 0.70), vote("history", 0.60)]
        d = engine().decide(market(), votes)
        assert d.side == "yes" and d.effective_agree == pytest.approx(2.5)

    def test_fee_eats_thin_edge(self):
        votes = [vote(a, 0.53) for a in "abcd"]
        d = engine(prefer_maker=False).decide(market(), votes)
        assert d.side == "yes"
        assert d.action == "hold"
        assert any("net edge" in r for r in d.reasons)

    def test_veto_overrides_unanimous_agreement(self):
        votes = [vote(a, 0.9) for a in "abcdef"]
        d = engine().decide(market(yes_bid=30, yes_ask=60), votes)
        assert d.action == "veto"
        assert any(r.startswith("veto:max_spread") for r in d.reasons)

    def test_no_side(self):
        votes = [vote(a, 0.20) for a in "abcd"]
        d = engine().decide(market(), votes)
        assert d.action == "fire", d.reasons
        assert d.side == "no"
        assert d.p_side == pytest.approx(1.0 - d.p_yes_pooled)

    def test_stale_votes_ignored(self):
        votes = [vote(a, 0.75) for a in "abc"] + [vote("d", 0.75, age=3600)]
        d = engine().decide(market(), votes)
        assert d.action == "hold"
        assert any("stale" in r for r in d.reasons)

    def test_latest_vote_per_agent_wins(self):
        votes = [vote(a, 0.75) for a in "abcd"] + [vote("a", 0.20, age=60)]
        d = engine().decide(market(), votes)
        assert d.voting_count == 4
        assert d.action == "fire"

    def test_other_tickers_ignored(self):
        votes = [vote(a, 0.75) for a in "abcd"] + [vote("e", 0.1, ticker="OTHER")]
        d = engine().decide(market(), votes)
        assert d.voting_count == 4

    def test_market_anchor_pulls_toward_mid(self):
        votes = [vote(a, 0.90) for a in "abcd"]
        light = engine(market_weight=0.0).decide(market(), votes)
        heavy = engine(market_weight=8.0).decide(market(), votes)
        assert light.p_yes_pooled == pytest.approx(0.90, abs=1e-6)
        assert 0.5 < heavy.p_yes_pooled < light.p_yes_pooled

    def test_taker_when_spread_too_tight_for_maker(self):
        votes = [vote(a, 0.80) for a in "abcd"]
        d = engine(prefer_maker=True).decide(market(yes_bid=50, yes_ask=51), votes)
        assert d.order_type == "taker" and d.price_cents == 51

    def test_config_from_dict(self):
        cfg = ConsensusConfig.from_dict({"quorum": 3, "unknown": 1,
                                         "vetoes": {"max_spread_cents": 2}})
        assert cfg.quorum == 3
        assert cfg.vetoes.max_spread_cents == 2

    def test_decision_serialises(self):
        votes = [vote(a, 0.75) for a in "abcd"]
        payload = engine().decide(market(), votes).to_dict()
        assert payload["action"] == "fire"
        assert len(payload["votes"]) == 4


# -------------------------------------------------------- reliability --

class TestReliability:
    def _train(self, store, agent, p_yes, outcomes, market_p=0.5):
        for i, outcome in enumerate(outcomes):
            t = f"T{agent}{i}"
            store.record_forecast(AgentVote(agent=agent, ticker=t, p_yes=p_yes), market_p)
            store.resolve(t, outcome)

    def test_no_history_is_neutral(self):
        assert ReliabilityStore().weight("new") == 1.0

    def test_agent_beating_market_gains_weight(self):
        s = ReliabilityStore(prior_n=10)
        self._train(s, "good", 0.8, [True] * 80 + [False] * 20)
        assert s.weight("good") > 1.0

    def test_agent_worse_than_market_loses_weight(self):
        s = ReliabilityStore(prior_n=10)
        self._train(s, "bad", 0.9, [True] * 20 + [False] * 80)
        assert s.weight("bad") < 1.0

    def test_abstentions_not_recorded(self):
        s = ReliabilityStore()
        assert s.record_forecast(AgentVote(agent="a", ticker="T", p_yes=None), 0.5) is None

    def test_engine_uses_reliability_weights(self):
        s = ReliabilityStore(prior_n=5)
        self._train(s, "bad", 0.9, [False] * 100)
        votes = [vote("bad", 0.95)] + [vote(a, 0.60) for a in "abc"]
        weighted = ConsensusEngine(ConsensusConfig(), reliability=s, clock=clock).decide(market(), votes)
        flat = engine().decide(market(), votes)
        assert weighted.weights["bad"] < flat.weights["bad"]
        assert weighted.p_yes_pooled < flat.p_yes_pooled


# ------------------------------------------------------------- ledger --

class TestLedger:
    def test_shadow_pnl_and_summary(self):
        ledger = DecisionLedger()
        e = engine()
        fired = e.decide(market(), [vote(a, 0.75) for a in "abcd"])
        held = e.decide(market(ticker="T2"), [vote(a, 0.5, ticker="T2") for a in "abcd"])
        ledger.record(fired)
        ledger.record(held)
        assert ledger.resolve(fired.ticker, outcome_yes=True) == 1
        s = ledger.summary()
        assert s.fired == 1 and s.held == 1
        assert s.resolved_fired == 1 and s.wins == 1
        expected = 100 - fired.price_cents - fired.fee_cents_per_contract
        assert s.total_pnl_cents == pytest.approx(expected)

    def test_losing_no_side(self):
        ledger = DecisionLedger()
        d = engine().decide(market(), [vote(a, 0.2) for a in "abcd"])
        ledger.record(d)
        ledger.resolve(d.ticker, outcome_yes=True)
        s = ledger.summary()
        assert s.wins == 0
        assert s.total_pnl_cents == pytest.approx(-(d.price_cents + d.fee_cents_per_contract))

    def _fire(self, ledger, ticker, created_at=NOW):
        e = ConsensusEngine(ConsensusConfig(), clock=lambda: created_at)
        d = e.decide(market(ticker=ticker), [vote(a, 0.75, ticker=ticker) for a in "abcd"])
        assert d.action == "fire"
        ledger.record(d)
        return d

    def test_ci_is_clustered_by_event(self):
        # 3 events x 4 strikes: strikes of one event share their outcome
        ledger = DecisionLedger()
        outcomes = {"EVA": True, "EVB": False, "EVC": True}
        for ev, won in outcomes.items():
            for k in range(4):
                t = f"KXHIGHNY-{ev}-T{70 + k}"
                self._fire(ledger, t)
                ledger.resolve(t, outcome_yes=won)
        s = ledger.summary()
        assert s.resolved_fired == 12 and s.resolved_events == 3
        pnls = [p for (p,) in ledger._conn.execute("SELECT pnl_cents FROM decisions")]
        m = sum(pnls) / 12
        naive_half = 1.96 * (sum((p - m) ** 2 for p in pnls) / 11 / 12) ** 0.5
        clustered_half = (s.pnl_ci95_cents[1] - s.pnl_ci95_cents[0]) / 2
        assert clustered_half > 1.5 * naive_half          # 12 correlated strikes != 12 samples
        from consensus.ledger import t95
        assert t95(2) == pytest.approx(4.303) and t95(500) == 1.96

    def test_summary_since_does_not_recount_refires(self):
        ledger = DecisionLedger()
        self._fire(ledger, "KXHIGHNY-EVA-T70", created_at=NOW - 7200)
        self._fire(ledger, "KXHIGHNY-EVA-T70", created_at=NOW)     # same market fires again later
        assert ledger.summary().fired == 1
        assert ledger.summary(since=NOW - 3600).fired == 0          # its first fire is before the cutoff
        assert ledger.first_ts() == pytest.approx(NOW - 7200)

    def test_void_closes_without_pnl(self):
        ledger = DecisionLedger()
        self._fire(ledger, "KXHIGHNY-EVA-T70")
        assert ledger.void("KXHIGHNY-EVA-T70") == 1
        assert ledger.open_tickers() == [] and ledger.summary().resolved_fired == 0

    def test_recent_returns_payloads(self):
        ledger = DecisionLedger()
        ledger.record(engine().decide(market(), [vote(a, 0.75) for a in "abcd"]))
        rows = ledger.recent()
        assert rows[0]["action"] == "fire" and rows[0]["outcome"] is None
