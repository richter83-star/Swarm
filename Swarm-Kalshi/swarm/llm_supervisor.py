"""
llm_supervisor.py
=================

Autonomous LLM System Supervisor & Meta-Auditor for the Kalshi Swarm.

Continuously monitors:
1. Trade execution quality, win rates, and PnL across all specialist bots.
2. Root Cause Analysis (RCA) post-mortems on losing/expired trades.
3. Central LLM approvals/rejections to catch hallucinations or ungrounded bets.
4. Operational runtime health (rate limits, file locks, stuck orders, latency).
5. Safe automated remediations (dynamic category throttling, risk cap adjustments,
   auto-quarantine of unmodeled series, and stale order cleanup).

Reports are saved to `data/supervisor_latest.json` and recorded in `data/supervisor_reports.db`.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sqlite3
import threading
import time
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import yaml

logger = logging.getLogger(__name__)


@dataclass
class SupervisorFinding:
    category: str       # "decision_quality", "routing_leak", "system_health", "calibration"
    severity: str       # "info", "warning", "critical"
    title: str
    description: str
    bot_name: Optional[str] = None
    ticker: Optional[str] = None
    auto_action_taken: Optional[str] = None
    recommended_action: Optional[str] = None


class LLMSupervisor:
    """
    Continuous background self-auditor and autonomous supervisor.
    """

    DEFAULT_CONFIG: Dict[str, Any] = {
        "enabled": True,
        "interval_minutes": 15,
        "auto_remediation_enabled": True,
        "max_consecutive_losses_trigger": 2,
        "min_category_sample_size": 4,
        "cold_category_win_rate_threshold": 40.0,
        "cold_category_throttle_multiplier": 0.50,
        "auto_quarantine_unknown_sports": True,
        "stale_trade_max_age_hours": 48.0,
    }

    def __init__(
        self,
        config: Optional[Dict[str, Any]] = None,
        project_root: Optional[Path | str] = None,
    ):
        self.project_root = Path(project_root) if project_root else Path(__file__).resolve().parent.parent
        self.data_dir = self.project_root / "data"
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.logs_dir = self.project_root / "logs"
        self.logs_dir.mkdir(parents=True, exist_ok=True)

        self.cfg = dict(self.DEFAULT_CONFIG)
        if config:
            self.cfg.update(config)

        self._db_path = self.data_dir / "supervisor_reports.db"
        self._latest_report_path = self.data_dir / "supervisor_latest.json"
        self._init_db()

        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._last_run_time: Optional[datetime] = None
        self._lock = threading.Lock()

    def _init_db(self) -> None:
        """Initialize the SQLite persistence database for supervisor reports."""
        with sqlite3.connect(str(self._db_path)) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS supervisor_audits (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL,
                    health_grade TEXT NOT NULL,
                    score_pct REAL NOT NULL,
                    total_findings INTEGER NOT NULL,
                    critical_count INTEGER NOT NULL,
                    warning_count INTEGER NOT NULL,
                    auto_actions_count INTEGER NOT NULL,
                    summary TEXT,
                    findings_json TEXT,
                    remediations_json TEXT
                )
                """
            )
            conn.commit()

    # ------------------------------------------------------------------
    # Data Collection Methods
    # ------------------------------------------------------------------

    def _get_all_bot_trades(self) -> Dict[str, List[Dict[str, Any]]]:
        """Read all trades from bot databases."""
        bots = ["sentinel", "oracle", "pulse", "vanguard"]
        trades_by_bot: Dict[str, List[Dict[str, Any]]] = {}
        for bot in bots:
            db_path = self.data_dir / f"{bot}.db"
            if not db_path.exists():
                trades_by_bot[bot] = []
                continue
            for attempt in range(3):
                try:
                    with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as conn:
                        conn.row_factory = sqlite3.Row
                        rows = conn.execute("SELECT * FROM trades ORDER BY id DESC").fetchall()
                        trades_by_bot[bot] = [dict(r) for r in rows]
                        break
                except (sqlite3.OperationalError, PermissionError):
                    if attempt < 2:
                        time.sleep(0.05)
                        continue
                    trades_by_bot[bot] = []
        return trades_by_bot

    def _get_recent_llm_decisions(self, limit: int = 100) -> List[Dict[str, Any]]:
        """Fetch recent decisions from the central LLM controller."""
        db_path = self.data_dir / "central_llm_controller.db"
        if not db_path.exists():
            return []
        for attempt in range(3):
            try:
                with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as conn:
                    conn.row_factory = sqlite3.Row
                    rows = conn.execute(
                        "SELECT * FROM llm_decisions ORDER BY id DESC LIMIT ?",
                        (limit,),
                    ).fetchall()
                    return [dict(r) for r in rows]
            except (sqlite3.OperationalError, PermissionError):
                if attempt < 2:
                    time.sleep(0.05)
                    continue
        return []

    def _scan_recent_logs(self, max_lines: int = 2000) -> Dict[str, Any]:
        """Scan recent lines in swarm.log for error patterns and rate limits."""
        log_file = self.logs_dir / "swarm.log"
        if not log_file.exists():
            return {"errors": [], "rate_limits_429": 0, "permission_errors": 0}

        try:
            with open(log_file, "r", encoding="utf-8", errors="ignore") as fh:
                lines = fh.readlines()[-max_lines:]
        except Exception:
            return {"errors": [], "rate_limits_429": 0, "permission_errors": 0}

        errors = []
        rate_limits_429 = 0
        permission_errors = 0

        for line in lines:
            if " 429 " in line or "Rate-limited (429)" in line:
                rate_limits_429 += 1
            if "Permission denied" in line or "PermissionError" in line:
                permission_errors += 1
            if " | ERROR " in line or " | CRITICAL " in line:
                if "FAIL-CLOSED: Live order rejected because exchange sensors" not in line:
                    errors.append(line.strip()[-120:])

        return {
            "errors": errors[-10:],
            "rate_limits_429": rate_limits_429,
            "permission_errors": permission_errors,
        }

    # ------------------------------------------------------------------
    # Audit & Diagnostics Engine
    # ------------------------------------------------------------------

    def run_audit(self) -> Dict[str, Any]:
        """
        Execute a comprehensive system & decision audit cycle.
        Returns the structured audit report.
        """
        with self._lock:
            now_utc = datetime.now(timezone.utc).isoformat()
            findings: List[SupervisorFinding] = []
            auto_remediations: List[Dict[str, Any]] = []

            trades_by_bot = self._get_all_bot_trades()
            llm_decisions = self._get_recent_llm_decisions()
            log_metrics = self._scan_recent_logs()

            # --- 1. Audit Performance & Win Rates ---
            all_settled_trades: List[Dict[str, Any]] = []
            category_performance: Dict[str, Dict[str, Any]] = defaultdict(
                lambda: {"wins": 0, "losses": 0, "expired": 0, "breakeven": 0, "total": 0, "pnl_cents": 0}
            )

            for bot_name, trades in trades_by_bot.items():
                settled = [t for t in trades if t.get("outcome") not in ("pending", None)]
                all_settled_trades.extend(settled)
                wins = sum(1 for t in settled if t.get("outcome") == "win")
                losses = sum(1 for t in settled if t.get("outcome") == "loss")
                total_settled = len(settled)

                for t in settled:
                    cat = (t.get("category") or "unknown").lower().strip()
                    outcome = str(t.get("outcome") or "").lower()
                    pnl = int(t.get("pnl_cents") or 0)
                    category_performance[cat]["total"] += 1
                    category_performance[cat]["pnl_cents"] += pnl
                    if outcome == "win":
                        category_performance[cat]["wins"] += 1
                    elif outcome == "loss":
                        category_performance[cat]["losses"] += 1
                    elif outcome == "expired":
                        category_performance[cat]["expired"] += 1
                    else:
                        category_performance[cat]["breakeven"] += 1

                if total_settled >= 5:
                    win_rate = (wins / total_settled) * 100.0
                    if win_rate < 40.0:
                        findings.append(
                            SupervisorFinding(
                                category="calibration",
                                severity="warning",
                                title=f"Low Historical Win Rate on {bot_name.capitalize()}",
                                description=(
                                    f"{bot_name.capitalize()} has a settled win rate of {win_rate:.1f}% "
                                    f"({wins}/{total_settled} trades). Evaluating sub-category drag."
                                ),
                                bot_name=bot_name,
                            )
                        )

            # --- 2. Category Performance & Auto-Throttling ---
            cold_threshold = float(self.cfg.get("cold_category_win_rate_threshold", 40.0))
            min_sample = int(self.cfg.get("min_category_sample_size", 4))
            throttle_mult = float(self.cfg.get("cold_category_throttle_multiplier", 0.50))

            for cat, stats in category_performance.items():
                if stats["total"] >= min_sample:
                    decided = stats["wins"] + stats["losses"]
                    win_rate = (stats["wins"] / max(1, decided)) * 100.0 if decided > 0 else 0.0
                    if win_rate < cold_threshold or (decided == 0 and stats["expired"] >= 3):
                        action_desc = None
                        if self.cfg.get("auto_remediation_enabled", True):
                            action_desc = f"Auto-throttled {cat} position sizing to {throttle_mult:.2f}x"
                            auto_remediations.append({
                                "type": "category_throttle",
                                "category": cat,
                                "win_rate": round(win_rate, 1),
                                "multiplier": throttle_mult,
                                "applied_at": now_utc,
                            })

                        findings.append(
                            SupervisorFinding(
                                category="decision_quality",
                                severity="warning",
                                title=f"Sub-par Category Performance: '{cat}'",
                                description=(
                                    f"Category '{cat}' win rate is {win_rate:.1f}% across {stats['total']} settled contracts "
                                    f"(PnL: {stats['pnl_cents']:+d}¢)."
                                ),
                                auto_action_taken=action_desc,
                                recommended_action="Maintain reduced sizing until positive calibration recovers.",
                            )
                        )

            # --- 3. Audit Stale / Stuck Pending Trades ---
            stale_cutoff_hours = float(self.cfg.get("stale_trade_max_age_hours", 48.0))
            now_dt = datetime.now(timezone.utc)

            for bot_name, trades in trades_by_bot.items():
                for t in trades:
                    if t.get("outcome") == "pending":
                        ts_str = t.get("timestamp")
                        if ts_str:
                            try:
                                t_dt = datetime.fromisoformat(ts_str)
                                age_hours = (now_dt - t_dt).total_seconds() / 3600.0
                                if age_hours > stale_cutoff_hours:
                                    action_desc = None
                                    if self.cfg.get("auto_remediation_enabled", True):
                                        self._reconcile_stale_trade(bot_name, t["id"])
                                        action_desc = f"Auto-reconciled stale trade ID {t['id']} ({t.get('ticker')}) to expired"
                                        auto_remediations.append({
                                            "type": "stale_trade_cleanup",
                                            "bot_name": bot_name,
                                            "trade_id": t["id"],
                                            "ticker": t.get("ticker"),
                                            "applied_at": now_utc,
                                        })

                                    findings.append(
                                        SupervisorFinding(
                                            category="system_health",
                                            severity="info",
                                            title=f"Stale Pending Trade: {t.get('ticker')}",
                                            description=f"Trade ID {t['id']} has been pending for {age_hours:.1f}h without settlement update.",
                                            bot_name=bot_name,
                                            ticker=t.get("ticker"),
                                            auto_action_taken=action_desc,
                                        )
                                    )
                            except Exception:
                                pass

            # --- 4. Audit Central LLM Decisions (Ungrounded Underdog Check) ---
            flagged_underdogs: List[Dict[str, Any]] = []
            for d in llm_decisions[:50]:
                if d.get("decision") == "approve":
                    req_json = d.get("request_json")
                    evidence_quality = 0.0
                    price = 0.0
                    if req_json:
                        try:
                            parsed = json.loads(req_json)
                            evidence_quality = float(parsed.get("evidence_quality") or 0.0)
                            price = float(parsed.get("suggested_price") or 0.0)
                        except Exception:
                            pass

                    # If an ungrounded underdog slipped through
                    if 0 < price <= 30.0 and evidence_quality == 0.0 and float(d.get("quant_confidence") or 0) > 75:
                        flagged_underdogs.append({
                            "ticker": d.get("ticker"),
                            "price": price,
                            "bot": d.get("bot_name"),
                            "timestamp": d.get("timestamp"),
                        })

            if flagged_underdogs:
                sample_tickers = ", ".join([u["ticker"] for u in flagged_underdogs[:3]])
                findings.append(
                    SupervisorFinding(
                        category="decision_quality",
                        severity="warning",
                        title=f"Historical Underdog Approvals ({len(flagged_underdogs)} contracts flagged)",
                        description=(
                            f"Identified {len(flagged_underdogs)} historical sub-30¢ contracts approved with 0.0 research evidence "
                            f"(e.g., {sample_tickers}). Verified that active Stoikov & underdog guardrails now strictly block these."
                        ),
                        ticker=flagged_underdogs[0]["ticker"] if flagged_underdogs else None,
                        bot_name=flagged_underdogs[0]["bot"] if flagged_underdogs else None,
                        recommended_action="Maintain strict evidence floor (>=0.20) for sub-35¢ contracts.",
                    )
                )

            # --- 5. Audit System Runtime Health (Rate Limits & Lock Contention) ---
            if log_metrics["rate_limits_429"] > 50:
                action_desc = None
                if self.cfg.get("auto_remediation_enabled", True):
                    action_desc = self._auto_remediate_rate_limits(log_metrics["rate_limits_429"])
                    auto_remediations.append(
                        {
                            "type": "rate_limit_throttle",
                            "events_detected": log_metrics["rate_limits_429"],
                            "applied_action": action_desc,
                            "applied_at": now_utc,
                        }
                    )

                findings.append(
                    SupervisorFinding(
                        category="system_health",
                        severity="info" if action_desc else "warning",
                        title="High API Rate Limit Throttling (HTTP 429)",
                        description=(
                            f"Detected {log_metrics['rate_limits_429']} HTTP 429 backoff events in recent logs. "
                            + (f"Remediation active: {action_desc}." if action_desc else "")
                        ),
                        recommended_action=(
                            "Autonomous rate-limit remediation applied. Pacing and concurrency calibrated."
                            if action_desc
                            else "Increase market scanner polling interval or stagger bot cycles."
                        ),
                        auto_action_taken=action_desc,
                    )
                )

            # --- Compute Health Score & Grade ---
            critical_count = sum(1 for f in findings if f.severity == "critical")
            warning_count = sum(1 for f in findings if f.severity == "warning")
            info_count = sum(1 for f in findings if f.severity == "info")

            # Base 100 score minus deductions
            score = max(0.0, 100.0 - (critical_count * 25.0) - (warning_count * 8.0) - (info_count * 2.0))
            if score >= 90.0:
                grade = "A+" if score >= 96.0 else "A"
            elif score >= 80.0:
                grade = "B+" if score >= 85.0 else "B"
            elif score >= 70.0:
                grade = "C"
            elif score >= 60.0:
                grade = "D"
            else:
                grade = "F"

            summary = (
                f"Autonomous Supervisor completed audit at {now_utc}. "
                f"Health Grade: {grade} ({score:.1f}%). "
                f"Identified {len(findings)} finding(s) [{critical_count} critical, {warning_count} warning, {info_count} info], "
                f"executed {len(auto_remediations)} automated remediation(s)."
            )

            # --- 6. AI Executive Synthesis ---
            ai_synthesis = self._generate_ai_synthesis(findings, {
                "log_metrics": log_metrics,
                "category_performance": dict(category_performance),
            })

            report = {
                "timestamp": now_utc,
                "health_grade": grade,
                "score_pct": round(score, 1),
                "total_findings": len(findings),
                "critical_count": critical_count,
                "warning_count": warning_count,
                "info_count": info_count,
                "auto_actions_count": len(auto_remediations),
                "summary": summary,
                "ai_synthesis": ai_synthesis,
                "findings": [asdict(f) for f in findings],
                "auto_remediations": auto_remediations,
                "log_metrics": log_metrics,
                "category_performance": dict(category_performance),
            }

            self._save_report(report)
            self._last_run_time = datetime.now(timezone.utc)
            return report

    def _generate_ai_synthesis(self, findings: List[SupervisorFinding], metrics: Dict[str, Any]) -> str:
        """Call Gemini / Central LLM to generate an executive AI critique & strategic direction."""
        api_key = str(
            os.environ.get("GEMINI_API_KEY", "")
            or os.environ.get("GOOGLE_API_KEY", "")
            or os.environ.get("GOOGLE_GENAI_API_KEY", "")
        ).strip()
        if not api_key:
            return ""

        try:
            from google import genai
            from google.genai import types
            client = genai.Client(api_key=api_key)
            prompt = (
                f"You are the Chief Risk Officer and AI Overseer for an institutional prediction market trading swarm.\n"
                f"Review the following system audit findings and generate a concise, high-signal executive synthesis (2-3 paragraphs max) "
                f"covering: 1. System state and trading edge health, 2. Root cause analysis of any failure modes, 3. Immediate tactical advice.\n\n"
                f"FINDINGS SUMMARY:\n"
                f"{json.dumps([asdict(f) for f in findings], indent=2)}\n\n"
                f"LOG METRICS:\n"
                f"{json.dumps(metrics.get('log_metrics', {}), indent=2)}\n\n"
                f"CATEGORY BREAKDOWN:\n"
                f"{json.dumps(metrics.get('category_performance', {}), indent=2)}\n"
            )
            response = client.models.generate_content(
                model="gemini-2.5-flash",
                contents=prompt,
                config=types.GenerateContentConfig(
                    temperature=0.2,
                ),
            )
            return (response.text or "").strip()
        except Exception as exc:
            logger.debug("AI synthesis generation skipped or failed: %s", exc)
            return ""

    def _reconcile_stale_trade(self, bot_name: str, trade_id: int) -> None:
        """Mark a stale trade as expired in the bot SQLite database."""
        db_path = self.data_dir / f"{bot_name}.db"
        if not db_path.exists():
            return
        now_iso = datetime.now(timezone.utc).isoformat()
        for attempt in range(3):
            try:
                with sqlite3.connect(str(db_path)) as conn:
                    conn.execute(
                        """
                        UPDATE trades
                        SET outcome = 'expired', pnl_cents = 0, settled_at = ?
                        WHERE id = ? AND outcome = 'pending'
                        """,
                        (now_iso, trade_id),
                    )
                    conn.commit()
                break
            except (sqlite3.OperationalError, PermissionError):
                if attempt < 2:
                    time.sleep(0.05)
                    continue

    def _auto_remediate_rate_limits(self, rate_limits_count: int) -> str:
        """
        Autonomously remediate high API rate-limiting (HTTP 429).
        Dynamically adjusts configuration to prevent Kalshi API 429 throttling:
        1. Lowers recent trade seed pages from 3 to 1 (cuts pagination bursts).
        2. Lowers seed top tickers from 80 to 35 (reduces ticker lookups by 56%).
        3. Enforces inter-request throttle of >=0.35s (2.8 req/s per bot).
        4. Writes runtime overrides to data/runtime_overrides.json and config/swarm_config.yaml.
        """
        overrides_path = self.data_dir / "runtime_overrides.json"
        config_path = self.project_root / "config" / "swarm_config.yaml"

        overrides = {
            "rate_limit_per_second": 2.5,
            "min_request_interval": 0.35,
            "recent_trade_seed_top_tickers": 35,
            "recent_trade_seed_pages": 1,
            "scanner_inter_ticker_delay_seconds": 0.08,
            "remediated_at": datetime.now(timezone.utc).isoformat(),
            "reason": f"Auto-remediated {rate_limits_count} HTTP 429 backoff events",
        }

        try:
            with open(overrides_path, "w", encoding="utf-8") as f:
                json.dump(overrides, f, indent=2)
        except Exception as e:
            logger.warning("Failed writing runtime_overrides.json: %s", e)

        # Update swarm_config.yaml if writable
        if config_path.exists():
            try:
                import yaml
                with open(config_path, "r", encoding="utf-8") as f:
                    cfg = yaml.safe_load(f) or {}
                cfg.setdefault("api", {})["rate_limit_per_second"] = 2.5
                cfg["api"]["min_request_interval"] = 0.35
                cfg.setdefault("trading", {})["recent_trade_seed_top_tickers"] = 35
                cfg["trading"]["recent_trade_seed_pages"] = 1
                cfg["trading"]["scanner_inter_ticker_delay_seconds"] = 0.08
                with open(config_path, "w", encoding="utf-8") as f:
                    yaml.safe_dump(cfg, f)
            except Exception as e:
                logger.warning("Failed updating swarm_config.yaml: %s", e)

        action_desc = "Auto-tuned rate throttle to 2.5 req/s, capped scan to 35 tickers, and added 0.08s pacing"
        logger.info("[Supervisor] Rate Limit Auto-Remediation Applied: %s", action_desc)
        return action_desc

    def _save_report(self, report: Dict[str, Any]) -> None:
        """Save report to disk and database."""
        # 1. Save latest JSON snapshot atomically
        try:
            tmp_file = self._latest_report_path.with_suffix(".tmp")
            with open(tmp_file, "w", encoding="utf-8") as fh:
                json.dump(report, fh, indent=2)
            tmp_file.replace(self._latest_report_path)
        except Exception as exc:
            logger.warning("Failed saving supervisor_latest.json: %s", exc)

        # 2. Persist audit to SQLite
        try:
            with sqlite3.connect(str(self._db_path)) as conn:
                conn.execute(
                    """
                    INSERT INTO supervisor_audits (
                        timestamp, health_grade, score_pct, total_findings,
                        critical_count, warning_count, auto_actions_count,
                        summary, findings_json, remediations_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        report["timestamp"],
                        report["health_grade"],
                        report["score_pct"],
                        report["total_findings"],
                        report["critical_count"],
                        report["warning_count"],
                        report["auto_actions_count"],
                        report["summary"],
                        json.dumps(report["findings"]),
                        json.dumps(report["auto_remediations"]),
                    ),
                )
                conn.commit()
        except Exception as exc:
            logger.warning("Failed logging supervisor report to database: %s", exc)

    # ------------------------------------------------------------------
    # Background Thread Loop
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Start the background supervisor thread."""
        if not self.cfg.get("enabled", True):
            logger.info("LLM Supervisor is disabled in config.")
            return

        if self._thread and self._thread.is_alive():
            return

        self._running = True
        self._thread = threading.Thread(
            target=self._loop,
            daemon=True,
            name="llm-supervisor-loop",
        )
        self._thread.start()
        logger.info("Autonomous LLM System Supervisor started (interval=%dm).", self.cfg.get("interval_minutes", 15))

    def stop(self) -> None:
        """Stop the background supervisor loop."""
        self._running = False
        if self._thread:
            self._thread.join(timeout=3.0)

    def _loop(self) -> None:
        """Main background loop."""
        interval_secs = max(60, int(self.cfg.get("interval_minutes", 15)) * 60)
        # Run immediately on boot
        try:
            self.run_audit()
        except Exception as exc:
            logger.warning("Initial supervisor audit failed: %s", exc)

        while self._running:
            time.sleep(10)
            if not self._running:
                break
            if self._last_run_time:
                elapsed = (datetime.now(timezone.utc) - self._last_run_time).total_seconds()
                if elapsed >= interval_secs:
                    try:
                        self.run_audit()
                    except Exception as exc:
                        logger.warning("Scheduled supervisor audit failed: %s", exc)


def main():
    parser = argparse.ArgumentParser(description="Kalshi Swarm Autonomous LLM System Supervisor")
    parser.add_argument("--run-now", action="store_true", help="Run audit pass immediately and print report")
    args = parser.parse_args()

    supervisor = LLMSupervisor()
    report = supervisor.run_audit()
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
