#!/usr/bin/env python3
"""
Backtest: Overextension Reversal Strategy on 1-minute OHLC bars.

Strategy logic (4-step state machine):

  1. DETECT overextension: a significant directional move over `ext_lookback` bars
     whose peak/trough occurred within the last `confirm_bars` bars.
     `zone_bars` candles around that extreme mark out a supply/demand zone.

  2. WAIT FOR BREAK: price must close THROUGH the zone in the reversal direction
     (down through zone_low if extension was up; up through zone_high if down).

  3. WAIT FOR RETRACE TO 50%: after the break, price bounces BACK toward
     the extension origin and touches the 50% midpoint of the full move.
       - Up extension broke down → wait for bar HIGH  >= retrace_50
       - Down extension broke up → wait for bar LOW   <= retrace_50

  4. WAIT FOR ENTRY (the "smaller move"): at the 50% level, wait for a bar
     that CLOSES back through retrace_50 in the trade direction — the small
     rejection at the key level.
       - Up extension (short): bar close < retrace_50
       - Down extension (long): bar close > retrace_50

  Stop loss:   at the extreme (ext_end — peak for short, trough for long).
  Take profit: at the start of the extension (ext_start).

Usage:
    python backtest_second.py bars.parquet --equity 10000
    python backtest_second.py bars.parquet --equity 10000 --ext_threshold 0.5 --ext_lookback 15
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import time as dtime
from enum import Enum, auto
from typing import Optional
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

EASTERN         = ZoneInfo("America/New_York")
SESSION_START   = dtime(9, 31)
EOD_CLOSE_TIME  = dtime(15, 55)
MAX_TRADES_PER_DAY = 90


class State(Enum):
    IDLE            = auto()
    WAITING_BREAK   = auto()
    WAITING_RETRACE = auto()
    WAITING_ENTRY   = auto()


@dataclass
class Setup:
    direction:  str    # "up" (bullish extension → short trade) | "down" (→ long trade)
    ext_start:  float  # price at the beginning of the extension
    ext_end:    float  # price at the extreme (peak for "up", trough for "down")
    retrace_50: float  # midpoint = ext_start + 0.5 * (ext_end - ext_start)
    zone_high:  float  # zone high (marking candles around the extreme)
    zone_low:   float  # zone low


@dataclass
class Trade:
    entry_ts:    pd.Timestamp
    exit_ts:     pd.Timestamp
    side:        str
    entry:       float
    stop:        float
    tp:          float
    qty:         float
    exit_price:  float
    exit_reason: str
    pnl:         float


# ── helpers ──────────────────────────────────────────────────────────────────

def load_bars(path: str) -> pd.DataFrame:
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


def _detect_extension(
    highs: list[float],
    lows:  list[float],
    confirm_bars:  int,
    ext_threshold: float,
) -> Optional[dict]:
    """
    Scans the rolling window for a clear extension whose extreme occurred
    within the last `confirm_bars` bars. Returns direction and ext_start/end,
    or None if no qualifying extension is found.
    """
    n = len(highs)
    if n < 2:
        return None

    ha = np.array(highs)
    la = np.array(lows)

    idx_max = int(np.argmax(ha))
    idx_min = int(np.argmin(la))

    if ha[idx_max] - la[idx_min] < ext_threshold:
        return None

    if idx_max > idx_min:
        # High is more recent than low → upward extension, look for short
        if idx_max < n - confirm_bars:
            return None  # peak is stale
        return {
            "direction": "up",
            "ext_start": float(la[idx_min]),
            "ext_end":   float(ha[idx_max]),
        }
    else:
        # Low is more recent than high → downward extension, look for long
        if idx_min < n - confirm_bars:
            return None  # trough is stale
        return {
            "direction": "down",
            "ext_start": float(ha[idx_max]),
            "ext_end":   float(la[idx_min]),
        }


# ── backtest ─────────────────────────────────────────────────────────────────

def run_backtest(
    df: pd.DataFrame,
    start_equity:    float,
    risk_pct:        float = 0.01,
    ext_lookback:    int   = 15,
    confirm_bars:    int   = 3,
    ext_threshold:   float = 0.50,
    zone_bars:       int   = 3,
    break_timeout:   int   = 20,
    retrace_timeout: int   = 30,
    entry_timeout:   int   = 10,
) -> tuple[list[Trade], float]:

    equity = start_equity
    trades: list[Trade] = []

    state      = State.IDLE
    setup:     Optional[Setup]           = None
    state_bars = 0

    pos_side:     Optional[str]           = None
    pos_entry     = pos_stop = pos_tp = pos_qty = 0.0
    pos_entry_ts: Optional[pd.Timestamp] = None

    trades_today = 0
    current_day  = None

    roll_highs: list[float] = []
    roll_lows:  list[float] = []

    for _, row in df.iterrows():
        ts: pd.Timestamp = row["ts"]
        t  = ts.time()

        # ── day reset ────────────────────────────────────────────────────────
        day = ts.date()
        if day != current_day:
            current_day  = day
            trades_today = 0
            roll_highs.clear()
            roll_lows.clear()
            state      = State.IDLE
            setup      = None
            state_bars = 0
            pos_side   = None

        if t < SESSION_START or t >= EOD_CLOSE_TIME:
            continue

        bar_high  = float(row["high"])
        bar_low   = float(row["low"])
        bar_close = float(row["close"])

        # always maintain the rolling price window
        roll_highs.append(bar_high)
        roll_lows.append(bar_low)
        if len(roll_highs) > ext_lookback:
            roll_highs.pop(0)
            roll_lows.pop(0)

        # ── position management ──────────────────────────────────────────────
        if pos_side is not None:
            exit_px:     Optional[float] = None
            exit_reason: Optional[str]   = None

            if pos_side == "long":
                if bar_low   <= pos_stop: exit_px, exit_reason = pos_stop,  "stop"
                elif bar_high >= pos_tp:  exit_px, exit_reason = pos_tp,    "tp"
                elif t >= dtime(15, 54): exit_px, exit_reason = bar_close, "eod"
            else:
                if bar_high  >= pos_stop: exit_px, exit_reason = pos_stop,  "stop"
                elif bar_low  <= pos_tp:  exit_px, exit_reason = pos_tp,    "tp"
                elif t >= dtime(15, 54): exit_px, exit_reason = bar_close, "eod"

            if exit_px is not None:
                pnl = (
                    (exit_px - pos_entry) * pos_qty if pos_side == "long"
                    else (pos_entry - exit_px) * pos_qty
                )
                equity += pnl
                trades.append(Trade(
                    entry_ts=pos_entry_ts, exit_ts=ts, side=pos_side,
                    entry=pos_entry, stop=pos_stop, tp=pos_tp, qty=pos_qty,
                    exit_price=exit_px, exit_reason=exit_reason, pnl=pnl,
                ))
                trades_today += 1
                pos_side   = None
                state      = State.IDLE
                setup      = None
                state_bars = 0

            continue  # don't run state machine while in (or just exiting) a position

        # ── state machine ────────────────────────────────────────────────────

        if state == State.IDLE:
            if len(roll_highs) >= ext_lookback:
                ext = _detect_extension(roll_highs, roll_lows, confirm_bars, ext_threshold)
                if ext is not None:
                    zone_high  = max(roll_highs[-zone_bars:])
                    zone_low   = min(roll_lows[-zone_bars:])
                    retrace_50 = ext["ext_start"] + 0.5 * (ext["ext_end"] - ext["ext_start"])
                    setup = Setup(
                        direction  = ext["direction"],
                        ext_start  = ext["ext_start"],
                        ext_end    = ext["ext_end"],
                        retrace_50 = retrace_50,
                        zone_high  = zone_high,
                        zone_low   = zone_low,
                    )
                    state      = State.WAITING_BREAK
                    state_bars = 0

        elif state == State.WAITING_BREAK:
            state_bars += 1
            # invalidate if price pushes the extreme even further
            if setup.direction == "up"   and bar_high > setup.ext_end:
                state = State.IDLE; setup = None; state_bars = 0
            elif setup.direction == "down" and bar_low  < setup.ext_end:
                state = State.IDLE; setup = None; state_bars = 0
            elif state_bars > break_timeout:
                state = State.IDLE; setup = None; state_bars = 0
            elif setup.direction == "up"   and bar_close < setup.zone_low:
                state = State.WAITING_RETRACE; state_bars = 0
            elif setup.direction == "down"  and bar_close > setup.zone_high:
                state = State.WAITING_RETRACE; state_bars = 0

        elif state == State.WAITING_RETRACE:
            state_bars += 1
            if state_bars > retrace_timeout:
                state = State.IDLE; setup = None; state_bars = 0
            # up extension broke down → wait for price to bounce back UP to 50%
            elif setup.direction == "up"   and bar_high >= setup.retrace_50:
                state = State.WAITING_ENTRY; state_bars = 0
            # down extension broke up → wait for price to drop back DOWN to 50%
            elif setup.direction == "down"  and bar_low  <= setup.retrace_50:
                state = State.WAITING_ENTRY; state_bars = 0

        elif state == State.WAITING_ENTRY:
            state_bars += 1
            if state_bars > entry_timeout:
                state = State.IDLE; setup = None; state_bars = 0
            elif trades_today < MAX_TRADES_PER_DAY:
                # SHORT: up extension, price touched 50% and now closes BELOW it (rejection)
                if setup.direction == "up" and bar_close < setup.retrace_50:
                    entry_px = bar_close
                    # Stop at zone_low: if price climbs back into the broken zone, thesis is wrong
                    #sl       = setup.ext_end #the peak of the overextened zone
                    sl       = setup.zone_low
                    tp       = setup.ext_start  # start of extension
                    risk_ps  = sl - entry_px
                    if risk_ps > 0 and tp < entry_px:
                        qty = float(int((equity * risk_pct) / risk_ps))
                        if qty > 0:
                            pos_side = "short"
                            pos_entry, pos_stop, pos_tp, pos_qty = entry_px, sl, tp, qty
                            pos_entry_ts = ts

                # LONG: down extension, price touched 50% and now closes ABOVE it (rejection)
                elif setup.direction == "down" and bar_close > setup.retrace_50:
                    entry_px = bar_close
                    # Stop at zone_high: if price falls back into the broken zone, thesis is wrong
                    #sl = setup.ext_end #the trough of the overextened zone
                    sl       = setup.zone_high
                    tp       = setup.ext_start  # start of extension
                    risk_ps  = entry_px - sl
                    if risk_ps > 0 and tp > entry_px:
                        qty = float(int((equity * risk_pct) / risk_ps))
                        if qty > 0:
                            pos_side = "long"
                            pos_entry, pos_stop, pos_tp, pos_qty = entry_px, sl, tp, qty
                            pos_entry_ts = ts

    return trades, equity


# ── reporting ─────────────────────────────────────────────────────────────────

def print_results(trades: list[Trade], start_equity: float, end_equity: float) -> None:
    print(f"\n{'='*52}")
    print("BACKTEST RESULTS — Overextension Reversal")
    print(f"{'='*52}")

    if not trades:
        print("No trades taken.")
        print(f"{'='*52}\n")
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

    avg_r = None
    if trades:
        rs = []
        for t in trades:
            risk = abs(t.entry - t.stop)
            if risk > 0:
                rs.append((t.exit_price - t.entry if t.side == "long" else t.entry - t.exit_price) / risk)
        if rs:
            avg_r = sum(rs) / len(rs)
            print(f"Avg R:        {avg_r:.2f}R")

    print(f"{'='*52}\n")


def main():
    ap = argparse.ArgumentParser(description="Backtest overextension reversal on 1-min bar parquet")
    ap.add_argument("parquet",          help="Path to parquet file")
    ap.add_argument("--equity",         type=float, default=10_000.0, help="Starting equity (default: 10000)")
    ap.add_argument("--risk_pct",       type=float, default=0.01,     help="Risk per trade as fraction of equity (default: 0.01)")
    ap.add_argument("--ext_lookback",   type=int,   default=15,       help="Bars to look back for the extension (default: 15)")
    ap.add_argument("--confirm_bars",   type=int,   default=3,        help="Extreme must be within last N bars (default: 3)")
    ap.add_argument("--ext_threshold",  type=float, default=0.50,     help="Min extension size in price units (default: 0.50)")
    ap.add_argument("--zone_bars",      type=int,   default=3,        help="Bars around extreme that form the zone (default: 3)")
    ap.add_argument("--break_timeout",  type=int,   default=20,       help="Give up waiting for break after N bars (default: 20)")
    ap.add_argument("--retrace_timeout",type=int,   default=30,       help="Give up waiting for 50%% retrace after N bars (default: 30)")
    ap.add_argument("--entry_timeout",  type=int,   default=10,       help="Give up waiting for entry bar after N bars (default: 10)")
    ap.add_argument("--out",            default="",                    help="Save trade log to CSV path (optional)")
    args = ap.parse_args()

    print(f"Loading {args.parquet} ...")
    df = load_bars(args.parquet)
    print(f"  {len(df):,} bars  |  {df['ts'].iloc[0].date()} → {df['ts'].iloc[-1].date()}")

    trades, end_equity = run_backtest(
        df               = df,
        start_equity     = args.equity,
        risk_pct         = args.risk_pct,
        ext_lookback     = args.ext_lookback,
        confirm_bars     = args.confirm_bars,
        ext_threshold    = args.ext_threshold,
        zone_bars        = args.zone_bars,
        break_timeout    = args.break_timeout,
        retrace_timeout  = args.retrace_timeout,
        entry_timeout    = args.entry_timeout,
    )

    print_results(trades, args.equity, end_equity)

    if args.out and trades:
        pd.DataFrame([t.__dict__ for t in trades]).to_csv(args.out, index=False)
        print(f"Trade log saved to {args.out}")


if __name__ == "__main__":
    main()
