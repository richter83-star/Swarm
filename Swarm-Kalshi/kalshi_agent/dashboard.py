"""
dashboard.py
============

Generates performance reports and analytics from the learning engine's
SQLite database.  Outputs include plain-text summaries, CSV exports, and
optional Matplotlib charts saved to disk.

The dashboard can be invoked standalone (``python -m kalshi_agent.dashboard``)
or programmatically from the orchestrator.
"""

from __future__ import annotations

import csv
import io
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class Dashboard:
    """
    Performance dashboard backed by a ``LearningEngine`` instance.

    Parameters
    ----------
    learning_engine : LearningEngine
        Provides access to trade logs and analytics queries.
    output_dir : str
        Directory where reports and charts are saved.
    """

    def __init__(self, learning_engine, output_dir: str = "data/reports"):
        self.le = learning_engine
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # Text report
    # ------------------------------------------------------------------

    def generate_text_report(self) -> str:
        """
        Build a comprehensive plain-text performance report.
        """
        perf = self.le.get_performance()
        rolling = self.le.get_performance(last_n=self.le.cfg.get("rolling_window", 50))
        calibration = self.le.get_confidence_calibration()
        weight_hist = self.le.get_weight_history(limit=5)

        lines: List[str] = []
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        lines.append("=" * 70)
        lines.append(f"  KALSHI AI TRADING AGENT — PERFORMANCE REPORT")
        lines.append(f"  Generated: {ts}")
        lines.append("=" * 70)

        # --- Lifetime metrics ---
        lines.append("")
        lines.append("LIFETIME PERFORMANCE")
        lines.append("-" * 40)
        lines.append(f"  Total trades:       {perf['total_trades']}")
        lines.append(f"  Wins / Losses:      {perf['wins']} / {perf['losses']}")
        lines.append(f"  Win rate:           {perf['win_rate']:.1f}%")
        lines.append(f"  Total P&L:          {perf['total_pnl']:+d}¢  (${perf['total_pnl'] / 100:+.2f})")
        lines.append(f"  Avg P&L per trade:  {perf['avg_pnl']:+.1f}¢")
        lines.append(f"  ROI:                {perf['roi_pct']:+.2f}%")
        lines.append(f"  Sharpe-like ratio:  {perf['sharpe']:.4f}")
        lines.append(f"  Avg confidence:     {perf['avg_confidence']:.1f}")

        # --- Rolling window ---
        lines.append("")
        window = self.le.cfg.get("rolling_window", 50)
        lines.append(f"ROLLING PERFORMANCE (last {window} trades)")
        lines.append("-" * 40)
        lines.append(f"  Trades:             {rolling['total_trades']}")
        lines.append(f"  Win rate:           {rolling['win_rate']:.1f}%")
        lines.append(f"  Total P&L:          {rolling['total_pnl']:+d}¢")
        lines.append(f"  Avg P&L per trade:  {rolling['avg_pnl']:+.1f}¢")
        lines.append(f"  Sharpe-like ratio:  {rolling['sharpe']:.4f}")

        # --- Confidence calibration ---
        lines.append("")
        lines.append("CONFIDENCE CALIBRATION")
        lines.append("-" * 40)
        lines.append(f"  {'Bucket':<12} {'Trades':>7} {'Wins':>6} {'Win Rate':>9}")
        for b in calibration:
            lines.append(f"  {b['bucket']:<12} {b['trades']:>7} {b['wins']:>6} {b['win_rate']:>8.1f}%")

        # --- Weight history ---
        if weight_hist:
            lines.append("")
            lines.append("RECENT WEIGHT ADJUSTMENTS")
            lines.append("-" * 40)
            lines.append(f"  {'Timestamp':<22} {'Edge':>6} {'Liq':>6} {'Vol':>6} {'Time':>6} {'WR%':>6}")
            for w in weight_hist:
                ts_short = w["timestamp"][:19]
                lines.append(
                    f"  {ts_short:<22} {w['edge']:>6.3f} {w['liquidity']:>6.3f} "
                    f"{w['volume']:>6.3f} {w['timing']:>6.3f} {(w['win_rate'] or 0):>5.1f}%"
                )

        # --- Daily summaries ---
        daily = self.le.get_daily_summaries(limit=14)
        if daily:
            lines.append("")
            lines.append("DAILY P&L (last 14 days)")
            lines.append("-" * 40)
            lines.append(f"  {'Date':<12} {'Trades':>7} {'W':>4} {'L':>4} {'P&L':>10} {'Avg Conf':>9}")
            for d in daily:
                pnl_str = f"{d['gross_pnl_cents']:+d}¢"
                lines.append(
                    f"  {d['date']:<12} {d['trades']:>7} {d['wins']:>4} {d['losses']:>4} "
                    f"{pnl_str:>10} {(d['avg_confidence'] or 0):>8.1f}"
                )

        # --- Category performance ---
        cat_perf = self.le.get_category_performance()
        if cat_perf:
            lines.append("")
            lines.append("CATEGORY PERFORMANCE")
            lines.append("-" * 40)
            lines.append(f"  {'Category':<20} {'Trades':>7} {'Wins':>5} {'Win%':>6} {'P&L':>10}")
            for c in cat_perf[:10]:
                pnl_str = f"{c['total_pnl_cents']:+d}\u00a2"
                lines.append(
                    f"  {(c['category'] or 'unknown'):<20} {c['trades']:>7} {c['wins']:>5} "
                    f"{(c['win_rate'] or 0):>5.1f}% {pnl_str:>10}"
                )

        # --- Feature importance ---
        trend = self.le.trend
        if trend.feature_importance:
            lines.append("")
            lines.append("FEATURE IMPORTANCE (point-biserial correlation with wins)")
            lines.append("-" * 40)
            for dim, score in sorted(trend.feature_importance.items(), key=lambda x: -x[1]):
                bar = "█" * max(0, int((score + 1) * 10))
                lines.append(f"  {dim:<12} {score:>+.4f}  {bar}")

        lines.append("")
        lines.append("=" * 70)
        report = "\n".join(lines)
        return report

    def save_text_report(self) -> Path:
        """Generate and save the text report to disk."""
        report = self.generate_text_report()
        ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        path = self.output_dir / f"report_{ts}.txt"
        path.write_text(report, encoding="utf-8")
        logger.info("Text report saved to %s", path)
        return path

    # ------------------------------------------------------------------
    # CSV export
    # ------------------------------------------------------------------

    def export_trades_csv(self, limit: int = 5000) -> Path:
        """Export trade history to a CSV file."""
        trades = self.le.get_all_trades(limit=limit)
        ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        path = self.output_dir / f"trades_{ts}.csv"

        if not trades:
            path.write_text("No trades recorded.\n")
            return path

        fieldnames = list(trades[0].keys())
        with open(path, "w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(trades)

        logger.info("Trades CSV exported to %s (%d rows)", path, len(trades))
        return path

    # ------------------------------------------------------------------
    # Chart generation (optional — requires matplotlib)
    # ------------------------------------------------------------------

    def generate_charts(self) -> List[Path]:
        """
        Generate performance charts and save as PNG files.

        Returns a list of file paths.  If matplotlib is not installed the
        method returns an empty list without raising.
        """
        try:
            import matplotlib
            matplotlib.use("Agg")
            import matplotlib.pyplot as plt
            import matplotlib.dates as mdates
        except ImportError:
            logger.info("matplotlib not installed — skipping chart generation.")
            return []

        charts: List[Path] = []

        # --- Cumulative P&L chart ---
        trades = self.le.get_all_trades(limit=5000)
        settled = [t for t in reversed(trades) if t["outcome"] in ("win", "loss")]
        if len(settled) >= 2:
            cum_pnl = []
            running = 0
            indices = []
            for i, t in enumerate(settled):
                running += t.get("pnl_cents") or 0
                cum_pnl.append(running / 100.0)
                indices.append(i + 1)

            fig, ax = plt.subplots(figsize=(10, 5))
            ax.plot(indices, cum_pnl, linewidth=1.5, color="#2ecc71")
            ax.axhline(0, color="gray", linewidth=0.5, linestyle="--")
            ax.set_xlabel("Trade #")
            ax.set_ylabel("Cumulative P&L ($)")
            ax.set_title("Cumulative P&L Over Time")
            ax.grid(True, alpha=0.3)
            fig.tight_layout()
            path = self.output_dir / "cumulative_pnl.png"
            fig.savefig(path, dpi=150)
            plt.close(fig)
            charts.append(path)

        # --- Win rate over time (rolling) ---
        if len(settled) >= 10:
            window = min(20, len(settled))
            rolling_wr = []
            for i in range(window, len(settled) + 1):
                chunk = settled[i - window:i]
                wr = sum(1 for t in chunk if t["outcome"] == "win") / len(chunk) * 100
                rolling_wr.append(wr)

            fig, ax = plt.subplots(figsize=(10, 5))
            ax.plot(range(window, len(settled) + 1), rolling_wr, linewidth=1.5, color="#3498db")
            ax.axhline(50, color="red", linewidth=0.5, linestyle="--", label="50% baseline")
            ax.set_xlabel("Trade #")
            ax.set_ylabel("Win Rate (%)")
            ax.set_title(f"Rolling Win Rate (window={window})")
            ax.legend()
            ax.grid(True, alpha=0.3)
            fig.tight_layout()
            path = self.output_dir / "rolling_win_rate.png"
            fig.savefig(path, dpi=150)
            plt.close(fig)
            charts.append(path)

        # --- Confidence vs outcome scatter ---
        if len(settled) >= 5:
            confs = [t["confidence"] for t in settled]
            pnls = [(t.get("pnl_cents") or 0) / 100.0 for t in settled]
            colors = ["#2ecc71" if t["outcome"] == "win" else "#e74c3c" for t in settled]

            fig, ax = plt.subplots(figsize=(10, 5))
            ax.scatter(confs, pnls, c=colors, alpha=0.6, s=30)
            ax.axhline(0, color="gray", linewidth=0.5, linestyle="--")
            ax.set_xlabel("Confidence Score")
            ax.set_ylabel("P&L ($)")
            ax.set_title("Confidence Score vs. Trade Outcome")
            ax.grid(True, alpha=0.3)
            fig.tight_layout()
            path = self.output_dir / "confidence_vs_pnl.png"
            fig.savefig(path, dpi=150)
            plt.close(fig)
            charts.append(path)

        # --- Daily P&L bar chart ---
        daily = self.le.get_daily_summaries(limit=30)
        if len(daily) >= 2:
            daily = list(reversed(daily))
            dates = [d["date"] for d in daily]
            pnls = [d["gross_pnl_cents"] / 100.0 for d in daily]
            colors = ["#2ecc71" if p >= 0 else "#e74c3c" for p in pnls]

            fig, ax = plt.subplots(figsize=(10, 5))
            ax.bar(dates, pnls, color=colors, width=0.7)
            ax.axhline(0, color="gray", linewidth=0.5)
            ax.set_xlabel("Date")
            ax.set_ylabel("P&L ($)")
            ax.set_title("Daily P&L")
            ax.tick_params(axis="x", rotation=45)
            fig.tight_layout()
            path = self.output_dir / "daily_pnl.png"
            fig.savefig(path, dpi=150)
            plt.close(fig)
            charts.append(path)

        logger.info("Generated %d chart(s).", len(charts))
        return charts

    # ------------------------------------------------------------------
    # Full report bundle
    # ------------------------------------------------------------------

    def full_report(self) -> Dict[str, Any]:
        """
        Generate all reports and charts, returning paths and summary data.
        """
        text_path = self.save_text_report()
        csv_path = self.export_trades_csv()
        chart_paths = self.generate_charts()
        perf = self.le.get_performance()

        return {
            "text_report": str(text_path),
            "csv_export": str(csv_path),
            "charts": [str(p) for p in chart_paths],
            "performance": perf,
        }


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main():
    """Run the dashboard from the command line."""
    import yaml
    from kalshi_agent.learning_engine import LearningEngine

    config_path = Path("config.yaml")
    if not config_path.exists():
        print("config.yaml not found. Run from the project root.")
        return

    with open(config_path) as fh:
        cfg = yaml.safe_load(fh)

    le = LearningEngine(cfg.get("learning", {}))
    dash = Dashboard(le)
    result = dash.full_report()

    print(dash.generate_text_report())
    print(f"\nFiles saved:")
    print(f"  Report: {result['text_report']}")
    print(f"  CSV:    {result['csv_export']}")
    for c in result["charts"]:
        print(f"  Chart:  {c}")

    le.close()


if __name__ == "__main__":
    main()
