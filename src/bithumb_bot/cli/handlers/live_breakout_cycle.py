"""`bt live breakout-cycle` handler — one-cycle live-trading dispatcher.

Restart-safe: the outer polling loop (a PowerShell script) invokes this
command once per tick. This handler NEVER daemonizes, opens a WebSocket
feed, or spawns a scheduler. It runs exactly one cycle then exits.

Safety envelope:

* Default ``--mode dry-run`` uses :class:`MockBroker`, loads NO trade
  credentials, and makes NO private HTTP request. ``--starting-cash-krw``
  is REQUIRED for dry-run mode (and forbidden in live mode, which reads
  cash from the venue).
* Live mode requires BOTH ``--mode live`` AND ``--enable-live-orders``.
  For live mode, the handler first validates the dataset, snapshot,
  and live-execution readiness of the snapshot. Only when the
  snapshot's live surface reports ``ready`` does the handler load
  credentials, create ``state_dir``, and construct the live broker.
  Missing credentials / unresolved readiness leave ``state_dir``
  nonexistent and make ZERO private-endpoint calls.
* ``--help`` / ``--version`` never load credentials — argparse handles
  them before the handler runs; every heavy import is deferred inside
  the handler function.
* No withdrawal endpoint, permission, or credential exists in this
  project (D-69). No withdrawal path is loaded, referenced, or
  imported anywhere here.
"""

from __future__ import annotations

import argparse
import os
import sys
from decimal import Decimal, InvalidOperation
from pathlib import Path


def handler(args: argparse.Namespace) -> int:
    """Entry point for ``bt live breakout-cycle``."""
    # Local imports so `--help` / `--version` pays no cost for them.
    from bithumb_bot.artifact.timestamps import utc_now
    from bithumb_bot.config.validator import REPO_ROOT_ENV, validate

    _result = validate(("live", "breakout-cycle"))
    if not _result.ok:
        print(
            f"bt live breakout-cycle: refusal: {_result.reason} "
            f"(missing: {', '.join(_result.missing) or 'unspecified'})",
            file=sys.stderr,
        )
        return 1

    dataset_path = Path(args.dataset)
    snapshot_path = Path(args.snapshot)
    state_dir = Path(args.state_dir)
    try:
        max_notional = Decimal(args.max_notional_krw)
    except (InvalidOperation, TypeError, ValueError) as exc:
        print(
            f"bt live breakout-cycle: --max-notional-krw not a Decimal "
            f"({exc})",
            file=sys.stderr,
        )
        return 1
    if not max_notional.is_finite() or max_notional <= 0:
        print(
            f"bt live breakout-cycle: --max-notional-krw must be > 0, got "
            f"{max_notional}",
            file=sys.stderr,
        )
        return 1

    mode = args.mode
    enable_live = bool(args.enable_live_orders)
    adopt_existing_btc = bool(getattr(args, "adopt_existing_btc", False))
    starting_cash_raw = getattr(args, "starting_cash_krw", None)

    if mode not in ("dry-run", "live"):
        print(
            f"bt live breakout-cycle: --mode must be dry-run|live, got "
            f"{mode!r}",
            file=sys.stderr,
        )
        return 1
    if mode == "live" and not enable_live:
        print(
            "bt live breakout-cycle: --mode live requires --enable-live-orders",
            file=sys.stderr,
        )
        return 1
    if mode == "dry-run" and enable_live:
        print(
            "bt live breakout-cycle: --enable-live-orders requires --mode live",
            file=sys.stderr,
        )
        return 1

    starting_cash: Decimal | None = None
    if mode == "dry-run":
        if starting_cash_raw is None:
            print(
                "bt live breakout-cycle: --mode dry-run requires "
                "--starting-cash-krw",
                file=sys.stderr,
            )
            return 1
        try:
            starting_cash = Decimal(starting_cash_raw)
        except (InvalidOperation, TypeError, ValueError) as exc:
            print(
                f"bt live breakout-cycle: --starting-cash-krw not a Decimal "
                f"({exc})",
                file=sys.stderr,
            )
            return 1
        if not starting_cash.is_finite() or starting_cash <= 0:
            print(
                f"bt live breakout-cycle: --starting-cash-krw must be > 0, "
                f"got {starting_cash}",
                file=sys.stderr,
            )
            return 1
    else:
        if starting_cash_raw is not None:
            print(
                "bt live breakout-cycle: --starting-cash-krw is forbidden "
                "in --mode live (cash is read from the venue)",
                file=sys.stderr,
            )
            return 1
        if adopt_existing_btc is False and getattr(args, "adopt_existing_btc", None) is not None:
            pass  # no-op, kept for clarity

    # Deferred imports — kept out of the module top level so `--help`
    # and `--version` on the parent parser pay no import cost.
    from bithumb_bot.bithumb_spec.snapshot import load_snapshot
    from bithumb_bot.broker.interface import Broker
    from bithumb_bot.broker.mock import MockBroker
    from bithumb_bot.core.money import Money
    from bithumb_bot.execution.readiness import check_execution_readiness
    from bithumb_bot.market_data.dataset import load_dataset
    from bithumb_bot.order_flow.live_cycle import (
        CycleInputs,
        CycleRefused,
        CycleResult,
        run_one_cycle,
    )

    try:
        dataset = load_dataset(dataset_path)
    except Exception as exc:
        print(
            f"bt live breakout-cycle: dataset refused ({type(exc).__name__}): {exc}",
            file=sys.stderr,
        )
        return 1
    try:
        snapshot = load_snapshot(snapshot_path)
    except Exception as exc:
        print(
            f"bt live breakout-cycle: snapshot refused ({type(exc).__name__}): {exc}",
            file=sys.stderr,
        )
        return 1

    if mode == "live":
        readiness = check_execution_readiness(
            snapshot, allow_provisional_fee_model=False
        )
        if readiness.live_execution_readiness != "ready":
            print(
                "bt live breakout-cycle: snapshot is not live-execution-ready "
                f"(missing: {', '.join(readiness.live_missing_requirements)})",
                file=sys.stderr,
            )
            return 1

        # Load credentials BEFORE creating state_dir or the broker so a
        # missing/partial credential leaves state_dir nonexistent and
        # zero private-endpoint calls occur.
        from bithumb_bot.secrets.live import (
            TradeCredentialsMissingError,
            load_trade_credentials,
        )

        repo_root = Path(os.environ.get(REPO_ROOT_ENV) or Path.cwd())
        try:
            creds = load_trade_credentials(repo_root)
        except TradeCredentialsMissingError as exc:
            print(
                f"bt live breakout-cycle: {exc}",
                file=sys.stderr,
            )
            return 1

    # Only now — after all guards passed — do we materialize state_dir
    # and (in live mode) the live broker.
    state_dir.mkdir(parents=True, exist_ok=True)

    broker: Broker
    balances_fetcher = None
    price_fetcher = None
    candle_fetcher = None
    venue_open_lister = None

    if mode == "live":
        from bithumb_bot.broker.live import LiveBithumbBroker

        live_broker = LiveBithumbBroker(
            access_key=creds.access_key,
            secret_key=creds.secret_key,
            state_dir=state_dir,
        )
        del creds
        broker = live_broker
        balances_fetcher = live_broker.fetch_balances
        price_fetcher = live_broker.fetch_current_price
        candle_fetcher = live_broker.fetch_current_candle
        venue_open_lister = live_broker.list_open_venue_orders
    else:
        broker = MockBroker(store_root=state_dir / "mock_broker")

    inputs = CycleInputs(
        dataset=dataset,
        snapshot=snapshot,
        state_dir=state_dir,
        max_notional_krw=Money(max_notional),
        broker=broker,
        now_utc=utc_now(),
        current_price_fetcher=price_fetcher,
        current_candle_fetcher=candle_fetcher,
        balances_fetcher=balances_fetcher,
        venue_open_lister=venue_open_lister,
        starting_cash_krw=(
            Money(starting_cash) if starting_cash is not None else None
        ),
        adopt_existing_btc=adopt_existing_btc,
    )

    try:
        result: CycleResult = run_one_cycle(inputs)
    except CycleRefused as exc:
        print(
            f"bt live breakout-cycle: refusal: {exc}",
            file=sys.stderr,
        )
        return 1
    except Exception as exc:  # noqa: BLE001 — surface any live-broker refusal cleanly
        print(
            f"bt live breakout-cycle: refusal ({type(exc).__name__}): {exc}",
            file=sys.stderr,
        )
        return 1

    print(f"mode:                 {mode}")
    print(f"status:               {result.status}")
    print(f"stopped_out_lockout:  {result.stopped_out_lockout}")
    if result.submitted_order is not None:
        print(
            f"submitted_client_order_id: "
            f"{result.submitted_order.client_order_id[:12]}…"
        )
    if result.active_order is not None:
        print(
            f"active_client_order_id:    "
            f"{result.active_order.client_order_id[:12]}…"
        )
    if result.notes:
        print(f"notes:                {'; '.join(result.notes)}")
    return 0


__all__ = ["handler"]
