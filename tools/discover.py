#!/usr/bin/env python3
"""Read-only Angel One SmartAPI diagnostics. This script never places or modifies orders."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dotenv import load_dotenv

from terminal.brokers.angel_one import AngelOneInstruments, AngelOneOrders, AngelOneSession
from terminal.config import load_config


def report(label, fn):
    try:
        value = fn()
        print(f"[OK] {label}: {value}")
        return value
    except Exception as exc:
        print(f"[ERROR] {label}: {type(exc).__name__}: {str(exc)[:220]}")
        return None


def main() -> int:
    load_dotenv()
    cfg = load_config()
    session = AngelOneSession()
    totp = input("Enter the current six-digit Angel One TOTP (leave blank if TOTP secret is configured): ").strip()
    try:
        session.login(totp or None)
        print("[OK] Angel One SmartAPI login and profile")
        instruments = AngelOneInstruments(session, cfg)
        rows = report("download instrument master", lambda: (instruments.load(force=True), len(instruments._rows))[1])
        if rows is None:
            return 1
        for item in cfg.dashboard_indices:
            report(f"index token {item['label']}", lambda item=item: (
                lambda sub: f"{sub.segment}|{sub.token} ({sub.symbol})"
            )(instruments.index_subscription(item["label"])))

        orders = AngelOneOrders(session, instruments, cfg)
        report("order book rows", lambda: len(orders.order_book()))
        report("trade book rows", lambda: len(orders.trade_book()))
        report("positions", lambda: len(orders.positions() or {}))
        report("holdings rows", lambda: len(orders.holdings()))
        report("funds response fields", lambda: sorted(orders.funds().keys()))
        print("\nRead-only diagnostics finished. No orders were sent, modified or cancelled.")
        return 0
    except Exception as exc:
        print(f"[ERROR] Angel One diagnostics stopped: {type(exc).__name__}: {str(exc)[:220]}")
        return 1
    finally:
        try:
            session.logout()
        except Exception:
            pass


if __name__ == "__main__":
    raise SystemExit(main())
