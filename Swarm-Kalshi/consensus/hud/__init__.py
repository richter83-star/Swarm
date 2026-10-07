"""
WHALE-OS HUD: a read-only Flask app over the shadow ledger.

    python -m consensus.hud                       # http://127.0.0.1:8890
    python -m consensus.hud --port 8891 --config config/whale_os.yaml
"""

from __future__ import annotations

import argparse
import os
from typing import Optional

from consensus.ledger import DecisionLedger
from consensus.reliability import ReliabilityStore
from consensus.settings import WhaleOSSettings, load_settings
from consensus.hud.state import build_state

STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")


def create_app(settings: WhaleOSSettings, ledger: Optional[DecisionLedger] = None,
               reliability: Optional[ReliabilityStore] = None):
    from flask import Flask, jsonify, send_from_directory

    app = Flask(__name__, static_folder=None)
    ledger = ledger or DecisionLedger(settings.ledger_path, mode=settings.mode)
    reliability = reliability or ReliabilityStore(settings.reliability_path)
    refresh = int((settings.hud or {}).get("refresh_seconds", 10))

    @app.get("/")
    def index():
        return send_from_directory(STATIC_DIR, "index.html")

    @app.get("/api/state")
    def state():
        data = build_state(settings, ledger, reliability)
        data["refresh_seconds"] = refresh
        return jsonify(data)

    @app.get("/healthz")
    def healthz():
        return {"ok": True, "mode": settings.mode}

    return app


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="WHALE-OS HUD (read-only)")
    ap.add_argument("--config", default=None)
    ap.add_argument("--host", default=None)
    ap.add_argument("--port", type=int, default=None)
    args = ap.parse_args(argv)
    settings = load_settings(args.config)
    hud = settings.hud or {}
    app = create_app(settings)
    app.run(host=args.host or hud.get("host", "127.0.0.1"),
            port=args.port or int(hud.get("port", 8890)), debug=False)
    return 0
