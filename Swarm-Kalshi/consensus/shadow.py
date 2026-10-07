"""
WHALE-OS shadow runner.

Every cycle:
  1. Discover open markets in the configured series (public Kalshi data).
  2. Poll cheap agents on each market; poll expensive agents (RESEARCH) only
     where a cheap agent already disagrees with the price, within a budget.
  3. Jev decides fire / hold / veto.  Decisions go to the ledger; votes go to
     the reliability store.  NO ORDERS ARE PLACED.
  4. Settle: markets that have resolved update the ledger P&L and each
     agent's reliability score.

Run (from Swarm-Kalshi/):

    python -m consensus.shadow --once            # one cycle, then exit
    python -m consensus.shadow                   # loop every cycle_seconds
    python -m consensus.shadow --settle-only     # only reconcile outcomes
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import time
from typing import Any, Dict, List, Optional, Tuple

from consensus.agents import Agent, MarketContext, build_agents
from consensus.aggregator import ConsensusConfig, ConsensusEngine
from consensus.data_sources import KalshiPublic, parse_ts, snapshot_from_market, source_stats
from consensus.ledger import DecisionLedger
from consensus.reliability import ReliabilityStore
from consensus.schema import AgentVote, Decision
from consensus.settings import WhaleOSSettings, load_settings

log = logging.getLogger("whale_os.shadow")


class ShadowRunner:
    def __init__(
        self,
        settings: WhaleOSSettings,
        reader: Any = None,
        agents: Optional[List[Agent]] = None,
        engine: Optional[ConsensusEngine] = None,
        ledger: Optional[DecisionLedger] = None,
        reliability: Optional[ReliabilityStore] = None,
        clock=time.time,
    ) -> None:
        self.s = settings
        self.clock = clock
        self.reader = reader or KalshiPublic()
        self.reliability = reliability or ReliabilityStore(settings.reliability_path)
        self.ledger = ledger or DecisionLedger(settings.ledger_path, mode=settings.mode)
        self.engine = engine or ConsensusEngine(
            ConsensusConfig.from_dict(settings.consensus), reliability=self.reliability, clock=clock)
        self.agents = agents if agents is not None else build_agents(settings.agents)
        self._last_recorded: Dict[str, Tuple[str, Optional[str], float]] = {}
        # settlement bookkeeping (in memory; after a restart every open ticker is checked once)
        self._close_at: Dict[str, float] = {}
        self._last_check: Dict[str, float] = {}
        self._misses: Dict[str, int] = {}

    # ------------------------------------------------------------------ #

    def discover(self, now: float) -> List[Dict[str, Any]]:
        """Open markets per series (highest volume first), interleaved round-robin
        so the per-cycle cap never starves the series listed last."""
        per_series: List[List[Dict[str, Any]]] = []
        for series in self.s.series:
            try:
                markets = self.reader.get_markets(series, status="open")
            except Exception as exc:
                log.warning("discover %s failed: %s", series, exc)
                continue
            keep = []
            for m in markets:
                close = parse_ts(m.get("close_time"))
                if close is None:
                    continue
                hours = (close - now) / 3600.0
                if not self.s.min_hours_to_close <= hours <= self.s.max_hours_to_close:
                    continue
                keep.append(m)
            keep.sort(key=lambda m: float(m.get("volume_24h_fp") or m.get("volume_24h") or 0), reverse=True)
            per_series.append(keep[: self.s.max_markets_per_series])
        picked: List[Dict[str, Any]] = []
        for rank in range(max((len(x) for x in per_series), default=0)):
            picked.extend(x[rank] for x in per_series if rank < len(x))
        return picked[: self.s.max_markets_per_cycle]

    def _should_record(self, d: Decision) -> bool:
        prev = self._last_recorded.get(d.ticker)
        if prev is None or prev[0] != d.action or prev[1] != d.side:
            return True
        return d.created_at - prev[2] >= self.s.record_interval_s

    def evaluate(self, raw: Dict[str, Any], now: float, budget: Dict[str, int]) -> Decision:
        snap = snapshot_from_market(raw, now)
        close_at = parse_ts(raw.get("close_time"))
        if close_at is not None:
            self._close_at[snap.ticker] = close_at
        ctx = MarketContext(snapshot=snap, raw=raw, reader=self.reader, now=now)
        votes: List[AgentVote] = []
        runs: List[Dict[str, Any]] = []
        for agent in (a for a in self.agents if a.tier != "expensive"):
            v, r = agent.vote(ctx)
            votes.append(v)
            runs.append(r)
        mid = snap.mid_cents
        disagreement = any(
            not v.abstained and abs(float(v.p_yes) * 100.0 - mid) >= self.s.expensive_trigger_cents
            for v in votes
        )
        for agent in (a for a in self.agents if a.tier == "expensive"):
            if disagreement and budget.get("expensive", 0) > 0:
                budget["expensive"] -= 1
                v, r = agent.vote(ctx)
                votes.append(v)
                runs.append(r)

        decision = self.engine.decide(snap, votes)
        if self._should_record(decision):
            self.ledger.record(decision)
            self._last_recorded[decision.ticker] = (decision.action, decision.side, decision.created_at)
        self.ledger.record_agent_runs(runs)
        if decision.action != "veto":        # untradeable markets say nothing about skill
            self.reliability.record_votes(votes, mid / 100.0)
        return decision

    def settle(self, now: float) -> int:
        """
        Reconcile open decisions/forecasts against Kalshi results.

        Only markets past their close time are checked (unknown close time, e.g.
        after a restart, counts as checkable), least-recently-checked first, so
        a long list of open hourly strikes can never starve the rest.  A market
        that settles to anything other than yes/no (void, scalar) or cannot be
        fetched ``settle_max_misses`` times in a row is retired without P&L.
        """
        open_tickers = list(dict.fromkeys(self.ledger.open_tickers() + self.reliability.open_tickers()))
        due = [t for t in open_tickers if self._close_at.get(t, 0.0) <= now]
        due.sort(key=lambda t: self._last_check.get(t, 0.0))
        settled = 0
        for ticker in due[: self.s.settle_batch]:
            self._last_check[ticker] = now
            try:
                m = self.reader.get_market(ticker)
            except Exception as exc:
                self._misses[ticker] = self._misses.get(ticker, 0) + 1
                log.debug("settle fetch %s failed (%d): %s", ticker, self._misses[ticker], exc)
                if self._misses[ticker] >= self.s.settle_max_misses:
                    self._retire(ticker, now, f"unfetchable x{self._misses[ticker]}")
                continue
            self._misses.pop(ticker, None)
            result = str(m.get("result") or "").lower()
            status = str(m.get("status") or "").lower()
            if result in ("yes", "no"):
                self.ledger.resolve(ticker, result == "yes", now)
                self.reliability.resolve(ticker, result == "yes", now)
                settled += 1
            elif result or status in ("settled", "finalized"):
                self._retire(ticker, now, f"result={result or '-'} status={status or '-'}")
        return settled

    def _retire(self, ticker: str, now: float, why: str) -> None:
        log.info("retiring %s without P&L (%s)", ticker, why)
        self.ledger.void(ticker, now)
        self.reliability.void(ticker)
        for d in (self._close_at, self._last_check, self._misses):
            d.pop(ticker, None)

    def run_cycle(self) -> Dict[str, Any]:
        t0 = self.clock()
        budget = {"expensive": int(self.s.max_expensive_calls_per_cycle)}
        markets = self.discover(t0)
        counts = {"fire": 0, "hold": 0, "veto": 0}
        errors = 0
        fired: List[str] = []
        for raw in markets:
            try:
                d = self.evaluate(raw, self.clock(), budget)
            except Exception as exc:
                errors += 1
                log.warning("evaluate %s failed: %s", raw.get("ticker"), exc)
                continue
            counts[d.action] = counts.get(d.action, 0) + 1
            if d.fired:
                fired.append(f"{d.ticker} {d.side} @{d.price_cents}c edge {d.edge_cents:.1f}c")
        settled = self.settle(self.clock())
        self.ledger.prune_agent_runs(self.clock() - self.s.agent_run_retention_days * 86400)
        report = {
            "mode": self.s.mode,
            "cycle_started": t0,
            "cycle_seconds": round(self.clock() - t0, 2),
            "markets": len(markets),
            "counts": counts,
            "fired": fired,
            "settled": settled,
            "errors": errors,
            "expensive_calls_left": budget["expensive"],
            "series": self.s.series,
            "agents": [a.name for a in self.agents],
            "cycle_period_s": self.s.cycle_seconds,
            "sources": source_stats(),
        }
        self._write_status(report)
        return report

    def _write_status(self, report: Dict[str, Any]) -> None:
        path = self.s.status_path
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(report, fh, indent=2)
        os.replace(tmp, path)

    def run_forever(self) -> None:
        log.info("WHALE-OS shadow mode: %d series, %d agents, every %ss. No orders are placed.",
                 len(self.s.series), len(self.agents), self.s.cycle_seconds)
        while True:
            started = time.monotonic()
            try:
                r = self.run_cycle()
                log.info("cycle: %d markets, fire %d / hold %d / veto %d, settled %d, %.1fs",
                         r["markets"], r["counts"]["fire"], r["counts"]["hold"],
                         r["counts"]["veto"], r["settled"], r["cycle_seconds"])
                for line in r["fired"]:
                    log.info("  SHADOW FIRE %s", line)
            except Exception:
                log.exception("cycle failed")
            time.sleep(max(5.0, self.s.cycle_seconds - (time.monotonic() - started)))


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="WHALE-OS shadow runner (no orders are placed)")
    ap.add_argument("--config", default=None, help="path to whale_os.yaml")
    ap.add_argument("--once", action="store_true", help="run one cycle and exit")
    ap.add_argument("--settle-only", action="store_true", help="only reconcile settled markets")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")

    settings = load_settings(args.config)
    if settings.mode != "shadow":
        raise SystemExit("whale_os mode must be 'shadow' - live trading is not wired in this module")
    runner = ShadowRunner(settings)
    if args.settle_only:
        print(json.dumps({"settled": runner.settle(time.time())}))
        return 0
    if args.once:
        print(json.dumps(runner.run_cycle(), indent=2))
        return 0
    runner.run_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
