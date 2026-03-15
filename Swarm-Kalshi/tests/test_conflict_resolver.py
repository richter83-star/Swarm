"""
tests/test_conflict_resolver.py
================================

Unit tests for ConflictResolver — the module that prevents two bots from
trading the same market ticker simultaneously.
"""

import sys
import tempfile
from pathlib import Path

import pytest

# Allow imports from the project root
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from swarm.conflict_resolver import ConflictResolver


@pytest.fixture()
def resolver(tmp_path):
    """Fresh in-memory-backed resolver for each test."""
    db = tmp_path / "test_claims.db"
    cr = ConflictResolver(db_path=str(db), stale_claim_hours=24.0)
    yield cr
    cr.close()


# ---------------------------------------------------------------------------
# Basic claim / release
# ---------------------------------------------------------------------------

def test_claim_succeeds_when_ticker_is_free(resolver):
    assert resolver.claim_ticker("sentinel", "TICKER-A") is True


def test_claim_is_idempotent_for_same_bot(resolver):
    resolver.claim_ticker("sentinel", "TICKER-A")
    assert resolver.claim_ticker("sentinel", "TICKER-A") is True


def test_second_bot_cannot_claim_held_ticker(resolver):
    resolver.claim_ticker("sentinel", "TICKER-A")
    assert resolver.claim_ticker("oracle", "TICKER-A") is False


def test_release_frees_ticker_for_other_bot(resolver):
    resolver.claim_ticker("sentinel", "TICKER-A")
    resolver.release_ticker("sentinel", "TICKER-A")
    assert resolver.claim_ticker("oracle", "TICKER-A") is True


def test_non_owner_cannot_release_ticker(resolver):
    resolver.claim_ticker("sentinel", "TICKER-A")
    resolver.release_ticker("oracle", "TICKER-A")  # no-op
    assert resolver.get_owner("TICKER-A") == "sentinel"


# ---------------------------------------------------------------------------
# is_claimed / get_owner / get_bot_claims
# ---------------------------------------------------------------------------

def test_is_claimed_false_initially(resolver):
    assert resolver.is_claimed("TICKER-X") is False


def test_is_claimed_true_after_claim(resolver):
    resolver.claim_ticker("pulse", "TICKER-X")
    assert resolver.is_claimed("TICKER-X") is True


def test_get_owner_returns_none_when_unclaimed(resolver):
    assert resolver.get_owner("TICKER-Z") is None


def test_get_owner_returns_correct_bot(resolver):
    resolver.claim_ticker("vanguard", "TICKER-Z")
    assert resolver.get_owner("TICKER-Z") == "vanguard"


def test_get_bot_claims_lists_all_held_tickers(resolver):
    resolver.claim_ticker("sentinel", "TICK-1")
    resolver.claim_ticker("sentinel", "TICK-2")
    resolver.claim_ticker("oracle", "TICK-3")
    claims = set(resolver.get_bot_claims("sentinel"))
    assert claims == {"TICK-1", "TICK-2"}


# ---------------------------------------------------------------------------
# release_all
# ---------------------------------------------------------------------------

def test_release_all_frees_every_ticker_for_bot(resolver):
    resolver.claim_ticker("sentinel", "TICK-1")
    resolver.claim_ticker("sentinel", "TICK-2")
    resolver.claim_ticker("oracle", "TICK-3")
    count = resolver.release_all("sentinel")
    assert count == 2
    assert resolver.get_claim_count() == 1
    assert resolver.get_owner("TICK-3") == "oracle"


def test_release_all_returns_zero_when_bot_has_no_claims(resolver):
    assert resolver.release_all("nonexistent") == 0


# ---------------------------------------------------------------------------
# filter_available
# ---------------------------------------------------------------------------

def test_filter_available_excludes_rival_claims(resolver):
    resolver.claim_ticker("oracle", "TICK-A")
    resolver.claim_ticker("sentinel", "TICK-B")
    available = resolver.filter_available("sentinel", ["TICK-A", "TICK-B", "TICK-C"])
    # TICK-A is taken by oracle; TICK-B and TICK-C are ok for sentinel
    assert set(available) == {"TICK-B", "TICK-C"}


# ---------------------------------------------------------------------------
# get_claim_count / get_claims_per_bot / status
# ---------------------------------------------------------------------------

def test_get_claim_count(resolver):
    assert resolver.get_claim_count() == 0
    resolver.claim_ticker("sentinel", "T1")
    resolver.claim_ticker("oracle", "T2")
    assert resolver.get_claim_count() == 2


def test_get_claims_per_bot(resolver):
    resolver.claim_ticker("sentinel", "T1")
    resolver.claim_ticker("sentinel", "T2")
    resolver.claim_ticker("oracle", "T3")
    per_bot = resolver.get_claims_per_bot()
    assert per_bot["sentinel"] == 2
    assert per_bot["oracle"] == 1


def test_status_structure(resolver):
    resolver.claim_ticker("pulse", "T1")
    s = resolver.status()
    assert s["total_claims"] == 1
    assert "claims_per_bot" in s
    assert "all_claims" in s


# ---------------------------------------------------------------------------
# SQLite persistence: claims survive a resolver restart
# ---------------------------------------------------------------------------

def test_claims_survive_restart(tmp_path):
    db = tmp_path / "persist.db"
    cr1 = ConflictResolver(db_path=str(db))
    cr1.claim_ticker("sentinel", "PERSIST-TICK")
    cr1.close()

    cr2 = ConflictResolver(db_path=str(db))
    assert cr2.get_owner("PERSIST-TICK") == "sentinel"
    cr2.close()
