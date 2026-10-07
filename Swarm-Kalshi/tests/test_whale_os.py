"""Tests for WHALE-OS: data parsing, strike math, agents, shadow runner, HUD state."""

import json
import math

import pytest

from consensus.agents import (CrossVenueAgent, CryptoAgent, FlowAgent, HistoryAgent,
                              MarketContext, OrderbookAgent, ResearchAgent, WeatherAgent,
                              build_agents)
from consensus.agents.research import parse_reply
from consensus.agents.strikes import prob_lognormal, prob_normal, yes_interval
from consensus.calibration import bucket
from consensus.data_sources import (OpenMeteo, parse_orderbook, parse_trade,
                                    snapshot_from_market, to_cents)
from consensus.ledger import DecisionLedger
from consensus.reliability import ReliabilityStore
from consensus.settings import WhaleOSSettings
from consensus.shadow import ShadowRunner
from consensus.hud.state import build_state

# 2026-10-07 14:00:00 UTC (10:00 New York)
NOW = 1791381600.0


def raw_market(ticker="KXHIGHNY-26OCT08-B74.5", bid="0.4400", ask="0.4600",
               strike_type="between", floor=74, cap=75, close="2026-10-09T05:00:00Z", **kw):
    m = {
        "ticker": ticker,
        "event_ticker": ticker.rsplit("-", 1)[0],
        "title": "Highest temperature in NYC on Oct 8, 2026?",
        "yes_bid_dollars": bid, "yes_ask_dollars": ask,
        "no_bid_dollars": f"{1 - float(ask):.4f}", "no_ask_dollars": f"{1 - float(bid):.4f}",
        "last_price_dollars": bid, "volume_24h_fp": "1200.00", "open_interest_fp": "800.00",
        "close_time": close, "strike_type": strike_type,
        "floor_strike": floor, "cap_strike": cap,
        "rules_primary": "If the maximum temperature recorded ...", "result": "",
    }
    m.update(kw)
    return m


def ctx_for(raw, orderbook=None, trades=None, now=NOW):
    return MarketContext(snapshot=snapshot_from_market(raw, now), raw=raw, now=now,
                         orderbook_data=orderbook, trades_data=trades)


# ---------------------------------------------------------------- parsing --

class TestParsing:
    def test_to_cents(self):
        assert to_cents("0.4400") == 44
        assert to_cents("1.0000") == 100
        assert to_cents(37) == 37
        assert to_cents(None) == 0

    def test_snapshot_from_market(self):
        s = snapshot_from_market(raw_market(), NOW)
        assert (s.yes_bid, s.yes_ask, s.no_bid, s.no_ask) == (44, 46, 54, 56)
        assert s.series_ticker == "KXHIGHNY"
        assert s.volume_24h == 1200 and s.open_interest == 800
        assert s.hours_to_close == pytest.approx(39.0, abs=0.01)

    def test_parse_orderbook_fp(self):
        ob = parse_orderbook({"orderbook_fp": {"yes_dollars": [["0.4400", "120.00"]],
                                               "no_dollars": [["0.5400", "80.00"]]}})
        assert ob == {"yes": [[44, 120.0]], "no": [[54, 80.0]]}

    def test_parse_trade(self):
        t = parse_trade({"taker_side": "yes", "yes_price_dollars": "0.4500", "count_fp": "10.00",
                         "created_time": "2026-10-07T13:55:00Z"})
        assert t == {"taker_side": "yes", "yes_price": 45, "count": 10, "ts": NOW - 300}
        assert parse_trade({"taker_side": "", "count": 5}) is None

    def test_event_date_from_ticker(self):
        assert str(ctx_for(raw_market()).event_date) == "2026-10-08"

    def test_open_meteo_multi_model_parse(self):
        payload = {"daily": {"time": ["2026-10-08"],
                             "temperature_2m_max_gfs_seamless": [74.1],
                             "temperature_2m_max_ecmwf_ifs025": [75.3]}}
        om = OpenMeteo(get_json=lambda url, params: payload)
        assert om.daily_max_f(0, 0, "UTC", "2026-10-08", ["gfs_seamless", "ecmwf_ifs025"]) == {
            "gfs_seamless": 74.1, "ecmwf_ifs025": 75.3}

    def test_source_labels(self):
        from consensus.data_sources import source_label
        base = "https://api.elections.kalshi.com/trade-api/v2"
        assert source_label(f"{base}/markets/KXHIGHNY-26OCT08-B74.5/orderbook") == "kalshi /orderbook"
        assert source_label(f"{base}/markets/trades") == "kalshi /trades"
        assert source_label(f"{base}/markets") == "kalshi /markets"
        assert source_label(f"{base}/markets/KXHIGHNY-26OCT08-B74.5") == "kalshi /market"
        assert source_label("https://api.exchange.coinbase.com/products/BTC-USD/candles") == "coinbase /candles"

    def test_open_meteo_error_raises(self):
        om = OpenMeteo(get_json=lambda url, params: {"error": True, "reason": "limit"})
        with pytest.raises(RuntimeError):
            om.daily_max_f(0, 0, "UTC", "2026-10-08", [])


# ---------------------------------------------------------------- strikes --

class TestStrikes:
    def test_intervals_integer(self):
        assert yes_interval("greater", 79, None, True) == (79.5, math.inf)
        assert yes_interval("less", None, 72, True) == (-math.inf, 71.5)
        assert yes_interval("between", 78, 79, True) == (77.5, 79.5)
        assert yes_interval("weird", 1, 2) is None

    def test_normal_partition_sums_to_one(self):
        mu, sd = 74.3, 2.4
        parts = [yes_interval("less", None, 72, True)] + \
                [yes_interval("between", lo, lo + 1, True) for lo in range(72, 80, 2)] + \
                [yes_interval("greater", 79, None, True)]
        assert sum(prob_normal(iv, mu, sd) for iv in parts) == pytest.approx(1.0, abs=1e-9)

    def test_lognormal_at_the_money_is_about_half(self):
        p = prob_lognormal(yes_interval("greater", 100.0, None), 100.0, 0.02)
        assert p == pytest.approx(0.496, abs=0.01)


# ----------------------------------------------------------------- agents --

class FakeOM:
    def __init__(self, values):
        self.values, self.calls = values, 0

    def daily_max_f(self, *a, **k):
        self.calls += 1
        return self.values


class FakeCB:
    def __init__(self, spot, closes):
        self._spot, self._closes = spot, closes

    def spot(self, product):
        return self._spot

    def closes(self, product, granularity=300):
        return self._closes


class TestAgents:
    def test_orderbook_microprice_leans_to_thin_side(self):
        ob = {"yes": [[44, 100.0]], "no": [[54, 900.0]]}   # big NO bids => heavy ask side
        v, run = OrderbookAgent().vote(ctx_for(raw_market(), orderbook=ob))
        assert run["status"] == "vote"
        assert 0.44 <= v.p_yes < 0.45                     # pulled toward the bid

    def test_orderbook_one_sided_abstains(self):
        v, run = OrderbookAgent().vote(ctx_for(raw_market(), orderbook={"yes": [[44, 5]], "no": []}))
        assert v.abstained and run["status"] == "abstain"

    def test_flow_imbalance_shifts_up(self):
        trades = [{"taker_side": "yes", "yes_price": 46, "count": 100, "ts": NOW - 60 * i} for i in range(6)]
        v, _ = FlowAgent({"max_shift_cents": 4, "size_norm_contracts": 600}).vote(
            ctx_for(raw_market(), trades=trades))
        assert v.p_yes == pytest.approx(0.49, abs=1e-6)     # mid 45 + 4 * 1.0 * 1.0

    def test_flow_needs_min_trades(self):
        trades = [{"taker_side": "yes", "yes_price": 46, "count": 10, "ts": NOW}]
        v, _ = FlowAgent().vote(ctx_for(raw_market(), trades=trades))
        assert v.abstained

    def test_weather_probability_and_cache(self):
        om = FakeOM({"gfs_seamless": 74.0, "ecmwf_ifs025": 75.0})
        agent = WeatherAgent({}, source=om)
        v1, _ = agent.vote(ctx_for(raw_market()))
        v2, _ = agent.vote(ctx_for(raw_market(ticker="KXHIGHNY-26OCT08-T79", strike_type="greater",
                                              floor=79, cap=None)))
        assert om.calls == 1
        assert 0.25 < v1.p_yes < 0.40                       # 74-75 band around a 74.5 mean
        assert v2.p_yes < 0.10                              # >=80 is far above the mean
        assert v1.family == "weather_model"

    def test_weather_skips_unknown_series_and_late_same_day(self):
        agent = WeatherAgent({}, source=FakeOM({"gfs_seamless": 70.0}))
        v, _ = agent.vote(ctx_for(raw_market(ticker="KXHIGHXYZ-26OCT08-B74.5")))
        assert v.abstained
        late = NOW + 6 * 3600                               # 16:00 New York, same day
        v2, _ = agent.vote(ctx_for(raw_market(ticker="KXHIGHNY-26OCT07-B66.5", floor=66, cap=67),
                                   now=late))
        assert v2.abstained

    def test_weather_source_failure_is_isolated(self):
        class Boom:
            def daily_max_f(self, *a, **k):
                raise TimeoutError("open-meteo down")
        v, run = WeatherAgent({}, source=Boom()).vote(ctx_for(raw_market()))
        assert v.abstained and run["status"] == "error" and "open-meteo down" in run["error"]

    def test_crypto_lognormal(self):
        closes = [100.0 * (1 + 0.001 * ((-1) ** i)) for i in range(60)]
        raw = raw_market(ticker="KXBTCD-26OCT0717-T99.99", strike_type="greater", floor=99.99, cap=None,
                         close="2026-10-07T21:00:00Z")
        v, run = CryptoAgent({}, source=FakeCB(100.0, closes)).vote(ctx_for(raw))
        assert run["status"] == "vote"
        assert 0.4 < v.p_yes < 0.6

    def test_history_applies_bucket_bias(self):
        table = {"series": {"KXHIGHNY": {"leads": {"24": {"n": 100, "bins": [
            {"lo": 40, "hi": 50, "n": 100, "yes": 30, "avg_price": 45.0}]}}}}}
        v, _ = HistoryAgent({"prior_n": 0}, table=table).vote(ctx_for(raw_market()))
        assert v.p_yes == pytest.approx(0.30, abs=1e-6)     # mid 45 + (30 - 45)

    def test_history_thin_bucket_abstains(self):
        table = {"series": {"KXHIGHNY": {"leads": {"24": {"bins": [
            {"lo": 40, "hi": 50, "n": 3, "yes": 0, "avg_price": 45.0}]}}}}}
        v, _ = HistoryAgent({}, table=table).vote(ctx_for(raw_market()))
        assert v.abstained

    def test_cross_venue_mapping_and_invert(self):
        class PM:
            def outcome_price(self, slug, outcome="Yes"):
                return 0.62
        agent = CrossVenueAgent({"mappings": [{"kalshi_regex": "^KXHIGHNY", "polymarket_slug": "x",
                                                "invert": True}]}, source=PM())
        v, _ = agent.vote(ctx_for(raw_market()))
        assert v.p_yes == pytest.approx(0.38)
        v2, _ = agent.vote(ctx_for(raw_market(ticker="KXBTCD-1")))
        assert v2.abstained

    def test_research_parses_and_caches(self):
        calls = []

        def fake(prompt):
            calls.append(prompt)
            return 'Here you go: {"p_yes": 0.7, "confidence": 0.6, "rationale": "fronts"}'
        agent = ResearchAgent({"enabled": True}, forecaster=fake)
        v, _ = agent.vote(ctx_for(raw_market()))
        agent.vote(ctx_for(raw_market()))
        assert v.p_yes == pytest.approx(0.7) and len(calls) == 1
        assert "0.44" not in calls[0] and "44" not in calls[0].split("Market:")[0]

    def test_research_without_key_reports_error(self, monkeypatch):
        monkeypatch.delenv("GEMINI_API_KEY", raising=False)
        v, run = ResearchAgent({"enabled": True}).vote(ctx_for(raw_market()))
        assert v.abstained and run["status"] == "error"

    def test_parse_reply_rejects_garbage(self):
        assert parse_reply("no json here") is None
        assert parse_reply('{"p_yes": 1.7}') is None

    def test_build_agents_defaults(self):
        names = [a.name for a in build_agents({"history": {"table_path": ""}})]
        assert names == ["book", "whales", "weather", "crypto", "history"]


# ------------------------------------------------------------ calibration --

def test_bucket():
    rows = [(42, True), (45, False), (48, False), (91, True), (99, True)]
    bins = {b["lo"]: b for b in bucket(rows, 10)}
    assert bins[40]["n"] == 3 and bins[40]["yes"] == 1
    assert bins[90]["n"] == 2


# ------------------------------------------------------------ shadow loop --

class FakeReader:
    def __init__(self, markets):
        self.markets = {m["ticker"]: m for m in markets}

    def get_markets(self, series, status="open", **kw):
        return [m for t, m in self.markets.items() if t.startswith(series) and not m.get("result")]

    def get_market(self, ticker):
        return self.markets[ticker]

    def get_orderbook(self, ticker):
        return {"yes": [], "no": []}

    def get_trades(self, ticker, limit=100):
        return []


class StubAgent:
    """Fixed-probability agent for driving the shadow loop in tests."""

    def __init__(self, name, p, family=None, tier="cheap"):
        self.name, self.family, self.tier, self._p = name, family or name, tier, p
        self.calls = 0

    def vote(self, ctx):
        from consensus.schema import AgentVote
        self.calls += 1
        v = AgentVote(agent=self.name, ticker=ctx.ticker, p_yes=self._p, family=self.family,
                      created_at=ctx.now)
        return v, {"agent": self.name, "ticker": ctx.ticker, "ts": ctx.now, "latency_ms": 1,
                   "status": "vote", "error": ""}


def make_runner(tmp_path, markets, agents, **over):
    s = WhaleOSSettings.from_dict({
        "data_dir": str(tmp_path), "series": ["KXHIGHNY"], "record_interval_s": 3600,
        "consensus": {"quorum": 3, "min_voters": 3, "min_edge_cents": 3},
        "agents": {"history": {"table_path": ""}}, **over})
    clock = {"t": NOW}
    runner = ShadowRunner(s, reader=FakeReader(markets), agents=agents, clock=lambda: clock["t"])
    return runner, clock


class TestShadow:
    def test_cycle_fires_records_and_settles(self, tmp_path):
        m = raw_market()
        agents = [StubAgent(n, 0.70) for n in ("a", "b", "c")]
        runner, clock = make_runner(tmp_path, [m], agents)
        r1 = runner.run_cycle()
        assert r1["counts"]["fire"] == 1 and r1["markets"] == 1
        runner.run_cycle()                                   # same decision: not re-recorded
        assert runner.ledger.summary().decisions == 1
        m["result"] = "yes"
        clock["t"] += 600
        assert runner.settle(clock["t"]) == 1
        s = runner.ledger.summary()
        assert s.resolved_fired == 1 and s.wins == 1 and s.total_pnl_cents > 0
        assert runner.reliability.stats("a").n == 1
        status = json.load(open(runner.s.status_path))
        assert status["mode"] == "shadow"

    def test_expensive_agent_only_on_disagreement_and_budget(self, tmp_path):
        markets = [raw_market(ticker=f"KXHIGHNY-26OCT08-B7{i}.5", floor=70 + i, cap=71 + i) for i in range(3)]
        cheap = [StubAgent("a", 0.70)]
        pricey = StubAgent("r", 0.70, tier="expensive")
        runner, _ = make_runner(tmp_path, markets, cheap + [pricey], max_expensive_calls_per_cycle=2)
        runner.run_cycle()
        assert pricey.calls == 2

        quiet = [StubAgent("a", 0.45)]                     # agrees with the 45c mid
        pricey2 = StubAgent("r", 0.70, tier="expensive")
        runner2, _ = make_runner(tmp_path / "q", markets, quiet + [pricey2])
        runner2.run_cycle()
        assert pricey2.calls == 0

    def test_discover_respects_close_window(self, tmp_path):
        soon = raw_market(ticker="KXHIGHNY-26OCT07-B66.5", close="2026-10-07T14:05:00Z")
        later = raw_market()
        runner, _ = make_runner(tmp_path, [soon, later], [StubAgent("a", 0.5)])
        assert [m["ticker"] for m in runner.discover(NOW)] == [later["ticker"]]

    def test_discover_round_robin_across_series(self, tmp_path):
        ny = [raw_market(ticker=f"KXHIGHNY-26OCT08-B7{i}.5") for i in range(3)]
        btc = [raw_market(ticker=f"KXBTCD-26OCT0817-T9{i}", strike_type="greater", floor=90 + i, cap=None)
               for i in range(2)]
        runner, _ = make_runner(tmp_path, ny + btc, [StubAgent("a", 0.5)],
                                series=["KXHIGHNY", "KXBTCD"], max_markets_per_cycle=3)
        picked = [m["ticker"].split("-")[0] for m in runner.discover(NOW)]
        assert picked == ["KXHIGHNY", "KXBTCD", "KXHIGHNY"]

    def test_hud_state(self, tmp_path):
        m = raw_market()
        runner, clock = make_runner(tmp_path, [m], [StubAgent(n, 0.70) for n in ("book", "whales", "weather")])
        runner.run_cycle()
        state = build_state(runner.s, runner.ledger, runner.reliability, now=clock["t"])
        assert state["mode"] == "SHADOW"
        assert state["jev"]["action"] == "fire"
        assert state["matrix"]["history"][-1]["cells"]["book"] == "yes"
        assert state["go_no_go"]["target"] == 400 and state["go_no_go"]["pass"] is False
        assert any(a["name"] == "book" and a["votes_24h"] == 1 for a in state["agents"])
        assert state["trades"][0]["ticker"] == m["ticker"] and state["inspector"]["ticker"] == m["ticker"]
        assert any(line["kind"] == "fire" for line in state["log"])
        assert state["fan"]["ready"] is False and state["hourly"]["fire"][-1] == 1

    def test_drift_fan_bootstrap(self):
        from consensus.hud.state import _fan
        fan = _fan([10.0] * 8 + [-5.0] * 4)
        assert fan["ready"] and len(fan["paths"]) == 120 and len(fan["paths"][0]) == 13
        assert fan["actual"][-1] == pytest.approx(60.0)
        assert fan["p5"] <= fan["median"] <= fan["p95"] and 0.0 <= fan["p_profit"] <= 1.0

    def test_market_snapshot_and_endpoint(self, tmp_path):
        pytest.importorskip("flask")
        from consensus.hud import create_app, market_snapshot

        class R(FakeReader):
            def get_orderbook(self, ticker):
                return {"yes": [[44, 10.0]], "no": [[54, 12.0]]}

            def get_candlesticks(self, series, ticker, start, end, period_interval=60):
                return [{"end_period_ts": 1, "price": {"open_dollars": "0.40", "high_dollars": "0.46",
                                                       "low_dollars": "0.39", "close_dollars": "0.45"},
                         "volume_fp": "12.00"},
                        {"end_period_ts": 2, "price": {"previous_dollars": "0.45"},
                         "yes_bid": {"close_dollars": "0.44"}, "yes_ask": {"close_dollars": "0.46"}}]
        reader = R([raw_market()])
        snap = market_snapshot(reader, raw_market()["ticker"], NOW)
        assert snap["candles"][0] == {"t": 1, "o": 40, "h": 46, "l": 39, "c": 45, "v": 12.0}
        assert snap["candles"][1]["c"] == 45.0 and snap["book"]["no"] == [[54, 12.0]]
        runner, _ = make_runner(tmp_path, [raw_market()], [StubAgent("a", 0.5)])
        c = create_app(runner.s, runner.ledger, runner.reliability, reader=reader).test_client()
        assert c.get(f"/api/market/{raw_market()['ticker']}").json["yes_bid"] == 44
        assert c.get("/api/market/bad%20ticker!").status_code == 400

    def test_hud_flask_endpoints(self, tmp_path):
        pytest.importorskip("flask")
        from consensus.hud import create_app
        runner, _ = make_runner(tmp_path, [raw_market()], [StubAgent("a", 0.5)])
        app = create_app(runner.s, runner.ledger, runner.reliability)
        c = app.test_client()
        assert c.get("/healthz").json["ok"] is True
        assert c.get("/api/state").status_code == 200
        assert b"WHALE" in c.get("/").data


def test_settings_refuse_live_mode(tmp_path):
    from consensus.shadow import main
    cfg = tmp_path / "w.yaml"
    cfg.write_text(f"mode: live\ndata_dir: {tmp_path.as_posix()}\nseries: [KXHIGHNY]\n")
    with pytest.raises(SystemExit):
        main(["--config", str(cfg), "--once"])


def test_ledger_dedupes_first_fire(tmp_path):
    from consensus import ConsensusEngine, ConsensusConfig
    from consensus.schema import AgentVote
    led = DecisionLedger(str(tmp_path / "l.db"))
    eng = ConsensusEngine(ConsensusConfig(quorum=3, min_voters=3), clock=lambda: NOW)
    snap = snapshot_from_market(raw_market(), NOW)
    votes = [AgentVote(agent=a, ticker=snap.ticker, p_yes=0.7, created_at=NOW) for a in "abc"]
    for _ in range(3):
        led.record(eng.decide(snap, votes))
    led.resolve(snap.ticker, True, NOW)
    s = led.summary()
    assert s.decisions == 3 and s.fired == 1 and s.resolved_fired == 1
    assert len(led.pnl_curve()) == 1


def test_reliability_upserts_open_forecast():
    from consensus.schema import AgentVote
    rs = ReliabilityStore()
    for p in (0.6, 0.7, 0.8):
        rs.record_forecast(AgentVote(agent="a", ticker="T", p_yes=p), 0.5)
    rs.resolve("T", True)
    st = rs.stats("a")
    assert st.n == 1 and st.brier_agent == pytest.approx(0.04)
