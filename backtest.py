#!/usr/bin/env python3
"""
Backtest for the FVG strategy on 1-minute OHLC bars.
Mirrors the live bot (main_unstable.py) exactly:
  - Same fvg.py detection and stack logic
  - Stop at signal candle's (candle1) low/high
  - Enter at open of the bar after the FVG bar
  - Hard stop / hard TP per bar (stop checked first if both triggered)
  - EOD close at 15:55 ET
  - Max 4 trades per day (PDT rule)

Usage:
    python backtest.py bars.parquet --equity 10000
    python backtest.py bars.parquet --equity 10000 --risk-pct 0.01 --tp-r 2 --out trades.csv
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import time as dtime
from typing import Optional
from zoneinfo import ZoneInfo

import pandas as pd

from fvg import detect_fvg, should_push, stack_pop_invalidated
from models import FVG, Candle, ExecCfg

EASTERN = ZoneInfo("America/New_York")
SESSION_START  = dtime(9, 31)
EOD_CLOSE_TIME = dtime(15, 55)
MAX_TRADES_PER_DAY = 4


@dataclass
class Trade:
    entry_ts: pd.Timestamp
    exit_ts: pd.Timestamp
    side: str           # "long" / "short"
    entry: float
    stop: float
    tp: float
    qty: float
    exit_price: float
    exit_reason: str    # "stop" | "tp" | "eod"
    pnl: float


def load_bars(path: str) -> pd.DataFrame:
    """
    Load a parquet file of 1-min OHLC bars.
    Accepts Alpaca short names (t/o/h/l/c/v) or full names
    (timestamp/open/high/low/close/volume). Converts timestamps to ET.
    """
    df = pd.read_parquet(path)
    df.columns = [c.lower() for c in df.columns]

    rename = {
        "t": "ts", "o": "open", "h": "high", "l": "low",
        "c": "close", "v": "volume", "timestamp": "ts", "datetime": "ts",
    }
    df = df.rename(columns={k: v for k, v in rename.items() if k in df.columns})

    if "ts" not in df.columns:
        df = df.reset_index()
        df.columns = ["ts"] + list(df.columns[1:])

    df["ts"] = pd.to_datetime(df["ts"])
    if df["ts"].dt.tz is None:
        df["ts"] = df["ts"].dt.tz_localize("UTC")
    df["ts"] = df["ts"].dt.tz_convert(EASTERN)

    return df.sort_values("ts").reset_index(drop=True)


def _make_candle(row: pd.Series) -> Candle:
    return Candle(
        symbol="BT",
        ts=row["ts"],
        open=float(row["open"]),
        high=float(row["high"]),
        low=float(row["low"]),
        close=float(row["close"]),
        volume=int(row["volume"]) if "volume" in row.index else 0,
    )


def run_backtest(
    df: pd.DataFrame,
    start_equity: float,
    cfg: ExecCfg,
    tp_r: float = 2.0,
    short_enabled: bool = False,
) -> tuple[list[Trade], float]:
    equity = start_equity
    trades: list[Trade] = []

    fvg_stack: list[FVG] = []
    candle0: Optional[Candle] = None
    candle1: Optional[Candle] = None

    pos_side: Optional[str] = None
    pos_entry = pos_stop = pos_tp = pos_qty = 0.0
    pos_entry_ts: Optional[pd.Timestamp] = None

    trades_today = 0
    current_day = None

    for _, row in df.iterrows():
        ts: pd.Timestamp = row["ts"]
        t = ts.time()

        day = ts.date()
        if day != current_day:
            current_day = day
            trades_today = 0
            candle0 = None
            candle1 = None
            fvg_stack.clear()
            pos_side = None  # EOD close should have handled this; safety reset

        if t < SESSION_START or t >= EOD_CLOSE_TIME:
            continue

        bar_high  = float(row["high"])
        bar_low   = float(row["low"])
        bar_close = float(row["close"])

        stack_pop_invalidated(fvg_stack, bar_low, bar_high)

        # --- Manage open position ---
        if pos_side is not None:
            exit_px: Optional[float] = None
            exit_reason: Optional[str] = None

            if pos_side == "long":
                if bar_low <= pos_stop:            # stop first (conservative)
                    exit_px, exit_reason = pos_stop, "stop"
                elif bar_high >= pos_tp:
                    exit_px, exit_reason = pos_tp, "tp"
                elif t >= dtime(15, 54):
                    exit_px, exit_reason = bar_close, "eod"
            else:
                if bar_high >= pos_stop:
                    exit_px, exit_reason = pos_stop, "stop"
                elif bar_low <= pos_tp:
                    exit_px, exit_reason = pos_tp, "tp"
                elif t >= dtime(15, 54):
                    exit_px, exit_reason = bar_close, "eod"

            if exit_px is not None:
                pnl = (
                    (exit_px - pos_entry) * pos_qty if pos_side == "long"
                    else (pos_entry - exit_px) * pos_qty
                )
                equity += pnl
                trades.append(Trade(
                    entry_ts=pos_entry_ts, exit_ts=ts,
                    side=pos_side,
                    entry=pos_entry, stop=pos_stop, tp=pos_tp, qty=pos_qty,
                    exit_price=exit_px, exit_reason=exit_reason, pnl=pnl,
                ))
                trades_today += 1
                pos_side = None

        # --- FVG detection + immediate entry when flat ---
        if pos_side is None and candle0 is not None and candle1 is not None:
            cur = _make_candle(row)
            detected = detect_fvg(candle0, candle1, cur)
            if detected is not None:
                was_empty = len(fvg_stack) == 0
                if should_push(fvg_stack, detected.dir, gap_low=detected.gap_low, gap_high=detected.gap_high):
                    fvg_stack.append(detected)
                    if not was_empty and trades_today < MAX_TRADES_PER_DAY and not (detected.dir == "bear" and not short_enabled):
                        entry_px = bar_close
                        if detected.dir == "bull":
                            stop = bar_low
                            risk_ps = entry_px - stop
                            if risk_ps > 0:
                                tp = entry_px + tp_r * risk_ps
                                qty = float(int((equity * cfg.risk_pct) / risk_ps))
                                qty = min(qty, float(int((equity * cfg.max_pos_value_mult) / entry_px)))
                                if qty > 0:
                                    pos_side = "long"
                                    pos_entry, pos_stop, pos_tp, pos_qty = entry_px, stop, tp, qty
                                    pos_entry_ts = ts
                        else:
                            stop = bar_high
                            risk_ps = stop - entry_px
                            if risk_ps > 0:
                                tp = entry_px - tp_r * risk_ps
                                qty = float(int((equity * cfg.risk_pct) / risk_ps))
                                qty = min(qty, float(int((equity * cfg.max_pos_value_mult) / entry_px)))
                                if qty > 0:
                                    pos_side = "short"
                                    pos_entry, pos_stop, pos_tp, pos_qty = entry_px, stop, tp, qty
                                    pos_entry_ts = ts

        # Advance the 3-bar window
        candle0 = candle1
        candle1 = _make_candle(row)

    return trades, equity


def print_results(trades: list[Trade], start_equity: float, end_equity: float) -> None:
    print(f"\n{'='*48}")
    print("BACKTEST RESULTS")
    print(f"{'='*48}")

    if not trades:
        print("No trades taken.")
        print(f"{'='*48}\n")
        return

    wins   = [t for t in trades if t.pnl > 0]
    losses = [t for t in trades if t.pnl <= 0]
    stops  = [t for t in trades if t.exit_reason == "stop"]
    tps    = [t for t in trades if t.exit_reason == "tp"]
    eods   = [t for t in trades if t.exit_reason == "eod"]
    longs  = [t for t in trades if t.side == "long"]
    shorts = [t for t in trades if t.side == "short"]

    print(f"Trades:       {len(trades)}  ({len(longs)} long, {len(shorts)} short)")
    print(f"Win rate:     {len(wins)/len(trades)*100:.1f}%  ({len(wins)} wins / {len(losses)} losses)")
    print(f"Exit — stop: {len(stops)}  |  tp: {len(tps)}  |  eod: {len(eods)}")
    print(f"Start equity: ${start_equity:,.2f}")
    print(f"End equity:   ${end_equity:,.2f}")
    print(f"Total P&L:    ${end_equity - start_equity:,.2f}  ({(end_equity / start_equity - 1) * 100:.1f}%)")
    if wins:
        print(f"Avg win:      ${sum(t.pnl for t in wins) / len(wins):,.2f}")
    if losses:
        print(f"Avg loss:     ${sum(t.pnl for t in losses) / len(losses):,.2f}")
    print(f"{'='*48}\n")


def main():
    ap = argparse.ArgumentParser(description="Backtest FVG strategy on 1-min bar parquet")
    ap.add_argument("parquet",    help="Path to parquet file")
    ap.add_argument("--equity",   type=float, default=10_000.0, help="Starting equity (default: 10000)")
    ap.add_argument("--risk-pct", type=float, default=0.01,     help="Risk per trade as fraction of equity (default: 0.01 = 1%%)")
    ap.add_argument("--tp-r",     type=float, default=2.0,      help="Take-profit R multiple (default: 2.0)")
    ap.add_argument("--short",    action="store_true",           help="Enable short trades")
    ap.add_argument("--out",      default="",                    help="Save trade log to this CSV path (optional)")
    args = ap.parse_args()

    cfg = ExecCfg(
        risk_pct=args.risk_pct,
        max_pos_value_mult=1.0,
        enable_loss_ladder=False,
    )

    print(f"Loading {args.parquet} ...")
    df = load_bars(args.parquet)
    print(f"  {len(df):,} bars  |  {df['ts'].iloc[0].date()} → {df['ts'].iloc[-1].date()}")

    trades, end_equity = run_backtest(
        df=df,
        start_equity=args.equity,
        cfg=cfg,
        tp_r=args.tp_r,
        short_enabled=args.short,
    )

    print_results(trades, args.equity, end_equity)

    if args.out and trades:
        pd.DataFrame([t.__dict__ for t in trades]).to_csv(args.out, index=False)
        print(f"Trade log saved to {args.out}")


if __name__ == "__main__":
    main()
