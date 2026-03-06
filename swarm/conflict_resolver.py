"""
conflict_resolver.py
====================

Prevents position conflicts across the swarm -- ensures no two bots
trade the same market ticker simultaneously.

v4 changes
----------
* Claims are persisted to SQLite so coordinator restarts don't lose state.
* In-memory cache is kept for fast lookups; DB is the source of truth.
* WAL mode enabled for safe concurrent reads from the dashboard.
* Stale claim pruning runs automatically on startup and periodically.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

logger = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS ticker_claims (
    ticker      TEXT PRIMARY KEY,
    bot_name    TEXT NOT NULL,
    claimed_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_claims_bot ON ticker_claims(bot_name);
"""


class ConflictResolver:
    """
    Prevents duplicate positions across swarm bots.

    Thread-safe.  Claims survive coordinator restarts via SQLite persistence.

    Parameters
    ----------
    db_path : str
        Path to the SQLite database file for persisting claims.
    stale_claim_hours : float
        Claims older than this are automatically pruned.
    """

    def __init__(
        self,
        db_path: str = "data/conflict_claims.db",
        stale_claim_hours: float = 24.0,
    ):
        self._lock = threading.Lock()
        self._stale_hours = stale_claim_hours

        db_file = Path(db_path)
        db_file.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(db_file), check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

        pruned = self._prune_db(stale_claim_hours)
        if pruned:
            logger.info("ConflictResolver: pruned %d stale claims from previous run.", pruned)

        self._claims: Dict[str, Tuple[str, datetime]] = {}
        self._bot_tickers: Dict[str, Set[str]] = {}
        self._load_from_db()

    def _load_from_db(self) -> None:
        rows = self._conn.execute(
            "SELECT ticker, bot_name, claimed_at FROM ticker_claims"
        ).fetchall()
        for ticker, bot_name, claimed_at_str in rows:
            try:
                claimed_at = datetime.fromisoformat(claimed_at_str)
            except ValueError:
                claimed_at = datetime.now(timezone.utc)
            self._claims[ticker] = (bot_name, claimed_at)
            self._bot_tickers.setdefault(bot_name, set()).add(ticker)
        logger.debug("ConflictResolver: loaded %d claims from DB.", len(self._claims))

    def _prune_db(self, max_age_hours: float) -> int:
        from datetime import timedelta
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=max_age_hours)).isoformat()
        cur = self._conn.execute(
            "DELETE FROM ticker_claims WHERE claimed_at < ?", (cutoff,)
        )
        self._conn.commit()
        return cur.rowcount

    def claim_ticker(self, bot_name: str, ticker: str) -> bool:
        with self._lock:
            if ticker in self._claims:
                owner, _ = self._claims[ticker]
                if owner != bot_name:
                    logger.debug("Ticker %s already claimed by %s. Denied for %s.", ticker, owner, bot_name)
                    return False
                return True

            now = datetime.now(timezone.utc)
            self._claims[ticker] = (bot_name, now)
            self._bot_tickers.setdefault(bot_name, set()).add(ticker)
            try:
                self._conn.execute(
                    "INSERT OR REPLACE INTO ticker_claims (ticker, bot_name, claimed_at) VALUES (?, ?, ?)",
                    (ticker, bot_name, now.isoformat()),
                )
                self._conn.commit()
            except Exception as exc:
                logger.warning("ConflictResolver: failed to persist claim for %s: %s", ticker, exc)
            logger.debug("Ticker %s claimed by %s", ticker, bot_name)
            return True

    def release_ticker(self, bot_name: str, ticker: str) -> None:
        with self._lock:
            if ticker in self._claims:
                owner, _ = self._claims[ticker]
                if owner == bot_name:
                    del self._claims[ticker]
                    if bot_name in self._bot_tickers:
                        self._bot_tickers[bot_name].discard(ticker)
                    try:
                        self._conn.execute("DELETE FROM ticker_claims WHERE ticker = ?", (ticker,))
                        self._conn.commit()
                    except Exception as exc:
                        logger.warning("ConflictResolver: failed to remove claim for %s: %s", ticker, exc)
                    logger.debug("Ticker %s released by %s", ticker, bot_name)
                else:
                    logger.warning("Bot %s tried to release ticker %s owned by %s", bot_name, ticker, owner)

    def release_all(self, bot_name: str) -> int:
        with self._lock:
            tickers = list(self._bot_tickers.pop(bot_name, set()))
            count = 0
            for ticker in tickers:
                if ticker in self._claims and self._claims[ticker][0] == bot_name:
                    del self._claims[ticker]
                    count += 1
            if tickers:
                try:
                    placeholders = ",".join("?" * len(tickers))
                    self._conn.execute(
                        f"DELETE FROM ticker_claims WHERE ticker IN ({placeholders})", tickers
                    )
                    self._conn.commit()
                except Exception as exc:
                    logger.warning("ConflictResolver: failed to bulk-release for %s: %s", bot_name, exc)
            logger.info("Released %d ticker claims for %s", count, bot_name)
            return count

    def is_claimed(self, ticker: str) -> bool:
        with self._lock:
            return ticker in self._claims

    def get_owner(self, ticker: str) -> Optional[str]:
        with self._lock:
            return self._claims[ticker][0] if ticker in self._claims else None

    def get_bot_claims(self, bot_name: str) -> List[str]:
        with self._lock:
            return list(self._bot_tickers.get(bot_name, set()))

    def get_all_claims(self) -> Dict[str, str]:
        with self._lock:
            return {ticker: owner for ticker, (owner, _) in self._claims.items()}

    def get_claim_count(self) -> int:
        with self._lock:
            return len(self._claims)

    def get_claims_per_bot(self) -> Dict[str, int]:
        with self._lock:
            return {bot: len(tickers) for bot, tickers in self._bot_tickers.items()}

    def filter_available(self, bot_name: str, tickers: List[str]) -> List[str]:
        with self._lock:
            return [
                t for t in tickers
                if t not in self._claims or self._claims[t][0] == bot_name
            ]

    def prune_stale_claims(self, max_age_hours: float = 24.0) -> int:
        with self._lock:
            from datetime import timedelta
            now = datetime.now(timezone.utc)
            stale = [
                (ticker, owner)
                for ticker, (owner, claimed_at) in self._claims.items()
                if (now - claimed_at).total_seconds() / 3600.0 > max_age_hours
            ]
            for ticker, owner in stale:
                del self._claims[ticker]
                if owner in self._bot_tickers:
                    self._bot_tickers[owner].discard(ticker)
            if stale:
                cutoff = (now - timedelta(hours=max_age_hours)).isoformat()
                try:
                    self._conn.execute("DELETE FROM ticker_claims WHERE claimed_at < ?", (cutoff,))
                    self._conn.commit()
                except Exception as exc:
                    logger.warning("ConflictResolver: failed to prune DB: %s", exc)
                logger.info("Pruned %d stale ticker claims", len(stale))
            return len(stale)

    def status(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "total_claims": len(self._claims),
                "claims_per_bot": {bot: len(t) for bot, t in self._bot_tickers.items()},
                "all_claims": {ticker: owner for ticker, (owner, _) in self._claims.items()},
            }

    def close(self) -> None:
        try:
            self._conn.close()
        except Exception:
            pass
