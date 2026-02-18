"""
Backtest: FVG STACK push/pop strategy (no Alpaca calls) on 1-minute Parquet data.

You asked for:
- Read 1 year of 1-min candles from parquet (no timestamp column is OK).
- Maintain a STACK of active FVGs (push new ones, pop when invalidated).
- Always compare new FVG to the one at the top of the stack.
- Start with $1000, trade through the dataset, print ending balance.

Key definitions used here:
- 3-candle FVG:
    Bullish FVG if b2.low  > b0.high   => gap = [b0.high, b2.low]
    Bearish FVG if b2.high < b0.low    => gap = [b2.high, b0.low]

Stack rules:
- POP (invalidate) top while it's "filled":
    - Bull gap invalid if bar.low <= gap_low
    - Bear gap invalid if bar.high >= gap_high

- PUSH:
    - If stack empty: push first detected FVG (creates current "structure")
    - If same direction as top:
        - Bull continuation: new.gap_low > top.gap_low  => push
        - Bear continuation: new.gap_high < top.gap_high => push
    - If opposite direction while stack not empty: ignore (you only flip after invalidation pops stack)

Trade rule (simple, aligned with stack):
- When a continuation FVG is pushed (or first FVG if you set trade_on_first=True),
  enter next bar OPEN in direction of that FVG.
- Stop: signal bar extreme (b2.low for long, b2.high for short)
- Take profit: fixed R multiple (default 2.0R)
- Position sizing: risk a fixed % of equity per trade (default 1%).
  shares = floor((equity * risk_pct) / risk_per_share), min 1 if affordable.
- Conservative intrabar execution: if stop and tp both touched in same candle => STOP first.
- Force exit at 15:59 ET close.

Install:
  pip install pandas pyarrow

Run:
  python backtest_fvg_stack.py --file data.parquet
  python backtest_fvg_stack.py --file data.parquet --risk_pct 0.02 --tp_r 1.3
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import time as dtime
from math import floor
from typing import Optional, Literal, List, Tuple, Dict
from zoneinfo import ZoneInfo

import pandas as pd
from math import log

ET = ZoneInfo("America/New_York")



@dataclass
class FVG:
    dir: Literal["bull", "bear"]
    gap_low: float
    gap_high: float
    created_ts: pd.Timestamp  # ET timestamp of signal bar (b2)


@dataclass
class Trade:
    entry_ts: pd.Timestamp
    exit_ts: pd.Timestamp
    direction: Literal["long", "short"]
    entry: float
    stop: float
    tp: float
    shares: int
    exit_price: float
    exit_reason: str
    pnl: float
    equity_after: float


# ---------- parquet timestamp extraction ----------

def in_rth(ts_et: pd.Timestamp) -> bool:
    t = ts_et.time()
    return dtime(9, 30) <= t <= dtime(15, 59)


# ---------- strategy ----------

def detect_fvg(b0: pd.Series, b1: pd.Series, b2: pd.Series) -> Optional[Dict]:
    # bullish
    if float(b2["low"]) > float(b0["high"]):
        return {"dir": "bull", "gap_low": float(b0["high"]), "gap_high": float(b2["low"])}
    # bearish
    if float(b2["high"]) < float(b0["low"]):
        return {"dir": "bear", "gap_low": float(b2["high"]), "gap_high": float(b0["low"])}
    return None


def stack_pop_invalidated(stack: List[FVG], bar_low: float, bar_high: float) -> None:
    # Pop while the top is invalidated (filled)
    while stack:
        top = stack[-1]
        if top.dir == "bull":
            if bar_low <= top.gap_low:
                stack.pop()
                continue
        else:  # bear
            if bar_high >= top.gap_high:
                stack.pop()
                continue
        break


def should_push(stack: List[FVG], new_dir: str, gap_low: float, gap_high: float) -> bool:
    if not stack:
        return True

    top = stack[-1]
    if new_dir != top.dir:
        # opposite direction while structure still active -> ignore (wait for pops)
        return False

    # same direction continuation condition
    if new_dir == "bull":
        return gap_low > top.gap_low
    else:
        return gap_high < top.gap_high


def backtest(
    df: pd.DataFrame,
    start_equity: float = 1000.0,
    risk_pct: float = 0.01,
    tp_r: float = 2.0,
    min_gap: float = 0.0,
    trade_on_first: bool = False,
    slippage: float = 0.01,
    monthly_deposit: float = 1000.0,
    alpha: float = 2.0,
    r_max: float = 2.0,
) -> Tuple[float, List[Trade]]:
    equity = start_equity

    def frac_closed_norm_log(r: float) -> float:
        # normalized log curve in [0,1]
        if r <= 0:
            return 0.0
        r = min(r, r_max)
        return log(1.0 + alpha * r) / log(1.0 + alpha * r_max)

    trades: List[Trade] = []

    # group by ET date
    df = df.sort_values("ts_et").reset_index(drop=True)
    df["day"] = df["ts_et"].dt.strftime("%Y-%m-%d")

    # position state
    in_pos = False
    pos_dir: Optional[Literal["long", "short"]] = None
    entry = stop = tp = 0.0
    shares = 0
    entry_ts: Optional[pd.Timestamp] = None
    init_shares = 0
    remaining_shares = 0
    max_r_seen = 0.0
    realized_pnl_current_trade = 0.0
    risk_per_share = 0.0
    last_deposit_yyyymm: Optional[str] = None
    # FVG stack per day (reset daily)
    for day, day_df in df.groupby("day", sort=True):
        day_df = day_df.reset_index(drop=True)
        yyyymm = day[:7]  # "YYYY-MM"
        i = 1
        if last_deposit_yyyymm != yyyymm:
            print(f"Added deposit for the {i}. time")
            equity += monthly_deposit
            last_deposit_yyyymm = yyyymm
        if len(day_df) < 10:
            continue

        stack: List[FVG] = []

        i = 2  # need 3 bars
        while i < len(day_df):
            row = day_df.iloc[i]
            ts = row["ts_et"]

            o = float(row["open"])
            h = float(row["high"])
            l = float(row["low"])
            c = float(row["close"])

            # Pop invalidated FVGs using current bar
            stack_pop_invalidated(stack, bar_low=l, bar_high=h)

            # Manage position exits
            if in_pos:
                if pos_dir == "long":
                    mfe = h - entry
                    cur_r = mfe / risk_per_share
                else:
                    mfe = entry - l
                    cur_r = mfe / risk_per_share

                if cur_r > max_r_seen:
                    max_r_seen = cur_r

                desired_closed = frac_closed_norm_log(max_r_seen)
                desired_closed_shares = int(floor(init_shares * desired_closed))
                to_close = desired_closed_shares - (init_shares - remaining_shares)

                if to_close > 0 and remaining_shares > 0:
                    to_close = min(to_close, remaining_shares)

                    # Fill at the R-level price implied by desired fraction.
                    # (Approx: assume we can fill at the level reached; apply slippage worst-case.)
                    # Use the price corresponding to max_r_seen (capped to r_max inside frac func).
                    exec_r = min(max_r_seen, r_max)

                    if pos_dir == "long":
                        fill_price = (entry + exec_r * risk_per_share) - slippage  # selling -> worse
                        pnl_per_share = fill_price - entry
                    else:
                        fill_price = (entry - exec_r * risk_per_share) + slippage  # buy-to-cover -> worse
                        pnl_per_share = entry - fill_price

                    pnl = pnl_per_share * to_close
                    equity += pnl
                    realized_pnl_current_trade += pnl
                    remaining_shares -= to_close

                    # If fully scaled out, end trade here (no further stop/tp checks needed)
                    if remaining_shares == 0:
                        trades.append(Trade(
                            entry_ts=entry_ts,
                            exit_ts=ts,
                            direction=pos_dir,
                            entry=entry,
                            stop=stop,
                            tp=tp,
                            shares=init_shares,
                            exit_price=fill_price,
                            exit_reason="scaled_out",
                            pnl=realized_pnl_current_trade,
                            equity_after=equity,
                        ))
                        in_pos = False
                        pos_dir = None
                        entry_ts = None
                        shares = 0
                        i += 1
                        continue

                 # >>> INSERT LOSS LADDER HERE <<<
                # --- Loss ladder (COMMENTED OUT FOR NOW) ---
                # # Compute worst adverse excursion (MAE) within this bar in R
                # if pos_dir == "long":
                #     mae = entry - l           # price went against us down to l
                #     neg_r = mae / risk_per_share
                # else:
                #     mae = h - entry           # price went against us up to h
                #     neg_r = mae / risk_per_share
                #
                # # Example: use the same normalized log idea on adverse R,
                # # but map it to "fraction to CUT" (0..1). You'd define a function like:
                # # frac_cut_norm_log(neg_r) where neg_r >= 0 means how far against you.
                # #
                # # desired_cut = frac_cut_norm_log(neg_r)
                # # desired_cut_shares = int(floor(init_shares * desired_cut))
                # # to_cut = desired_cut_shares - (init_shares - remaining_shares_cut_so_far)
                # #
                # # ... then execute reduction at a conservative fill price and update:
                # # equity += pnl_from_cut
                # # remaining_shares -= to_cut



                exit_reason = None
                exit_price = None

                # conservative: if both touched in same bar => stop first
                if pos_dir == "long":
                    if l <= stop:
                        exit_reason, exit_price = "stop", (stop - slippage) if pos_dir == "long" else (stop + slippage)
                    elif h >= tp:
                        exit_reason, exit_price = "tp", (tp - slippage) if pos_dir == "long" else (tp + slippage)
                else:
                    if h >= stop:
                        exit_reason, exit_price = "stop", (stop - slippage) if pos_dir == "long" else (stop + slippage)
                    elif l <= tp:
                        exit_reason, exit_price = "tp", (tp - slippage) if pos_dir == "long" else (tp + slippage)

                # EOD force close
                if ts.time() == dtime(15, 59) and exit_reason is None:
                    exit_reason, exit_price = "eod", (c - slippage) if pos_dir == "long" else (c + slippage)

                if exit_reason is not None:
                    pnl_per_share = (exit_price - entry) if pos_dir == "long" else (entry - exit_price)
                    pnl = pnl_per_share * remaining_shares
                    equity += pnl

                    trades.append(Trade(
                        entry_ts=entry_ts,  # type: ignore[arg-type]
                        exit_ts=ts,
                        direction=pos_dir,  # type: ignore[arg-type]
                        entry=entry,
                        stop=stop,
                        tp=tp,
                        shares=shares,
                        exit_price=exit_price,
                        exit_reason=exit_reason,
                        pnl=realized_pnl_current_trade,
                        equity_after=equity,
                    ))

                    in_pos = False
                    pos_dir = None
                    entry_ts = None
                    shares = 0
                    remaining_shares = 0
                    init_shares = 0
                    max_r_seen = 0.0
                    realized_pnl_current_trade = 0.0

                i += 1
                continue

            # Flat: detect FVG on (i-2, i-1, i)
            b0 = day_df.iloc[i - 2]
            b1 = day_df.iloc[i - 1]
            b2 = day_df.iloc[i]
            fvg = detect_fvg(b0, b1, b2)

            pushed = False
            signaled = False
            signal_dir: Optional[Literal["long", "short"]] = None
            signal_stop = 0.0
            signal_tp = 0.0
            signal_rps = 0.0

            if fvg is not None:
                gap_low = float(fvg["gap_low"])
                gap_high = float(fvg["gap_high"])
                if (gap_high - gap_low) >= min_gap:
                    if should_push(stack, fvg["dir"], gap_low, gap_high):
                        # Decide if this push should trigger a trade:
                        # - If stack was empty and trade_on_first=False => just anchor structure, no trade
                        was_empty = (len(stack) == 0)
                        stack.append(FVG(
                            dir=fvg["dir"],
                            gap_low=gap_low,
                            gap_high=gap_high,
                            created_ts=ts,
                        ))
                        pushed = True

                        if (not was_empty) or trade_on_first:
                            # enter NEXT bar open (i+1) to avoid lookahead
                            if i + 1 < len(day_df):
                                next_bar = day_df.iloc[i + 1]
                                raw_entry = float(next_bar["open"])
                                entry_price = (raw_entry + slippage) if signal_dir == "long" else (raw_entry - slippage)
                                entry_time = next_bar["ts_et"]

                                if fvg["dir"] == "bull":
                                    signal_dir = "long"
                                    signal_stop = float(b2["low"])
                                    signal_rps = entry_price - signal_stop
                                    if signal_rps > 0:
                                        signal_tp = entry_price + tp_r * signal_rps
                                        signaled = True
                                else:
                                    signal_dir = "short"
                                    signal_stop = float(b2["high"])
                                    signal_rps = signal_stop - entry_price
                                    if signal_rps > 0:
                                        signal_tp = entry_price - tp_r * signal_rps
                                        signaled = True

                                if signaled:
                                    # position sizing by risk
                                    risk_dollars = equity * risk_pct
                                    sh = int(floor(risk_dollars / signal_rps)) if signal_rps > 0 else 0
                                    if sh < 1:
                                        sh = 1

                                    # affordability check (simple): for long require cash >= entry*shares
                                    # (for short, ignore margin details; this is a backtest simplification)
                                    if signal_dir == "long" and (entry_price * sh) > equity:
                                        # scale down if needed
                                        sh = int(floor(equity / entry_price))
                                    if sh >= 1:
                                        init_shares = sh
                                        remaining_shares = sh
                                        max_r_seen = 0.0
                                        realized_pnl_current_trade = 0.0
                                        in_pos = True
                                        pos_dir = signal_dir
                                        entry = entry_price
                                        stop = signal_stop
                                        tp = signal_tp
                                        shares = sh
                                        entry_ts = entry_time
                                        risk_per_share = signal_rps

            i += 1

        # If still in position at end of day, it would have been closed by 15:59 logic.

    return equity, trades


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", required=True, help="Parquet path with 1-min bars")
    ap.add_argument("--assume_naive_tz", default="UTC", help="If timestamps tz-naive, assume this TZ (default UTC)")
    ap.add_argument("--start", type=float, default=1000.0, help="Starting equity (default 1000)")
    ap.add_argument("--risk_pct", type=float, default=0.01, help="Risk per trade as fraction of equity (default 0.01)")
    ap.add_argument("--tp_r", type=float, default=2.0, help="Take-profit multiple in R (default 2.0)")
    ap.add_argument("--min_gap", type=float, default=0.0, help="Min FVG gap size (price units)")
    ap.add_argument("--trade_on_first", action="store_true", help="Also trade the first FVG that anchors an empty stack")
    ap.add_argument("--out_trades", default="trades_stack.csv")
    ap.add_argument("--slip", type=float, default=0.01, help="Slippage in price units per fill (default 0.01)")
    ap.add_argument("--monthly_deposit", type=float, default=1000.0, help="Monthly deposit added on first trading day seen (default 1000)")
    ap.add_argument("--alpha", type=float, default=2.0, help="Normalized log curve aggressiveness (default 2.0)")
    ap.add_argument("--r_max", type=float, default=2.0, help="R ")
    args = ap.parse_args()

    df = pd.read_parquet(args.file)

    df = df.reset_index()
    idx_col = df.columns[0]          # the index column becomes the first column
    df = df.rename(columns={idx_col: "ts"})

    df["ts"] = pd.to_datetime(df["ts"], errors="raise")

    # make tz-aware and create ts_et
    if df["ts"].dt.tz is None:
        df["ts_utc"] = df["ts"].dt.tz_localize("UTC")
    else:
        df["ts_utc"] = df["ts"].dt.tz_convert("UTC")

    df["ts_et"] = df["ts_utc"].dt.tz_convert(ET)

    # keep only RTH
    df = df[df["ts_et"].apply(in_rth)].copy()

    end_equity, trades = backtest(
        df,
        start_equity=args.start,
        risk_pct=args.risk_pct,
        tp_r=args.tp_r,
        min_gap=args.min_gap,
        trade_on_first=args.trade_on_first,
        slippage=args.slip,
        monthly_deposit=args.monthly_deposit,
        alpha=args.alpha,
        r_max=args.r_max,
    )

    print(f"Start equity: ${args.start:,.2f}")
    print(f"End equity:   ${end_equity:,.2f}")
    if trades:
        tdf = pd.DataFrame([t.__dict__ for t in trades])
        tdf.to_csv(args.out_trades, index=False)
        win_rate = float((tdf["pnl"] > 0).mean())
        total_pnl = float(tdf["pnl"].sum())
        print(f"Trades: {len(trades)} | Win rate: {win_rate*100:.1f}% | Total PnL: ${total_pnl:,.2f}")
        print(f"Saved trades -> {args.out_trades}")
    else:
        print("No trades taken (try --trade_on_first or lower --min_gap).")


if __name__ == "__main__":
    main()
