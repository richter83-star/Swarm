"""WHALE-OS settings (``config/whale_os.yaml``)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any, Dict, List, Optional

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = PROJECT_ROOT / "config" / "whale_os.yaml"


@dataclass
class WhaleOSSettings:
    mode: str = "shadow"
    data_dir: str = "data/whale_os"
    cycle_seconds: int = 300
    series: List[str] = field(default_factory=list)
    max_markets_per_series: int = 12
    max_markets_per_cycle: int = 60
    min_hours_to_close: float = 0.25
    max_hours_to_close: float = 48.0
    max_expensive_calls_per_cycle: int = 5
    expensive_trigger_cents: float = 3.0
    record_interval_s: int = 3600
    settle_batch: int = 60
    settle_max_misses: int = 6
    agent_run_retention_days: int = 7
    consensus: Dict[str, Any] = field(default_factory=dict)
    agents: Dict[str, Any] = field(default_factory=dict)
    hud: Dict[str, Any] = field(default_factory=dict)
    calibration: Dict[str, Any] = field(default_factory=dict)

    def _path(self, name: str) -> str:
        base = Path(self.data_dir)
        if not base.is_absolute():
            base = PROJECT_ROOT / base
        base.mkdir(parents=True, exist_ok=True)
        return str(base / name)

    @property
    def ledger_path(self) -> str:
        return self._path("ledger.db")

    @property
    def reliability_path(self) -> str:
        return self._path("reliability.db")

    @property
    def status_path(self) -> str:
        return self._path("status.json")

    @property
    def calibration_path(self) -> str:
        return self._path("calibration.json")

    @classmethod
    def from_dict(cls, data: Optional[Dict[str, Any]]) -> "WhaleOSSettings":
        data = dict(data or {})
        known = {f.name for f in fields(cls)}
        s = cls(**{k: v for k, v in data.items() if k in known})
        s.series = [str(x).strip().upper() for x in (s.series or []) if str(x).strip()]
        hist = s.agents.setdefault("history", {})
        if isinstance(hist, dict) and not hist.get("table_path"):
            hist["table_path"] = s.calibration_path
        elif isinstance(hist, dict) and not os.path.isabs(hist["table_path"]):
            hist["table_path"] = str(PROJECT_ROOT / hist["table_path"])
        return s


def load_settings(path: Optional[str] = None) -> WhaleOSSettings:
    import yaml

    p = Path(path) if path else DEFAULT_CONFIG
    if not p.is_absolute() and not p.exists():
        p = PROJECT_ROOT / p
    with open(p, "r", encoding="utf-8") as fh:
        return WhaleOSSettings.from_dict(yaml.safe_load(fh) or {})
