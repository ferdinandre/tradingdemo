"""
Opening Range (first 5 minutes) + 1-min Fair Value Gap (FVG) breakout strategy for Alpaca.

What it does (daily):
- At 9:30 AM US/Eastern, fetch the first 5-minute bar (9:30–9:35) and record its high/low (opening range).
- After 9:35, poll 1-minute bars and detect a simple 3-candle FVG:
    Bullish FVG if:  current_low  > high_two_bars_ago
    Bearish FVG if:  current_high < low_two_bars_ago
- Only trade if the FVG forms *beyond* the opening range:
    Bullish FVG: current_low > opening_high  -> go LONG
    Bearish FVG: current_high < opening_low  -> go SHORT
- Stop loss: on the "first candle outside the range" (the breakout candle):
    Long: stop = breakout_candle.low
    Short: stop = breakout_candle.high
- Take profit: fixed 2R (2:1 reward:risk).
- Uses bracket orders so TP/SL are attached.
- After TP/SL fills, continue scanning for the next FVG until EOD.

Requirements:
    pip install alpaca-py

creds.toml (assumed to exist) example:
    [alpaca]
    key_id = "YOUR_KEY"
    secret_key = "YOUR_SECRET"
    paper = true
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from typing import Optional, List

# Alpaca SDK (alpaca-py)
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import MarketOrderRequest, TakeProfitRequest, StopLossRequest
from alpaca.trading.enums import OrderSide, TimeInForce, OrderClass
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit

# TOML reader (py3.11+: tomllib; older: tomli)
try:
    import tomllib  # py3.11+
except ModuleNotFoundError:
    import tomli as tomllib  # type: ignore


ET = ZoneInfo("America/New_York")


@dataclass(frozen=True)
class Bar:
    t: datetime
    o: float
    h: float
    l: float
    c: float


@dataclass(frozen=True)
class FVGSignal:
    direction: str  # "long" or "short"
    breakout_bar: Bar
    stop_price: float
    take_profit: float


def read_creds(path: str = "creds.toml") -> dict:
    with open(path, "rb") as f:
        data = tomllib.load(f)
    if "alpaca" not in data:
        raise ValueError("creds.toml must contain [alpaca] section.")
    cfg = data["alpaca"]
    for k in ("key_id", "secret_key"):
        if k not in cfg:
            raise ValueError(f"Missing alpaca.{k} in creds.toml")
    cfg.setdefault("paper", True)
    return cfg


def now_et() -> datetime:
    return datetime.now(tz=ET)


def sleep_until(target_et: datetime) -> None:
    while True:
        n = now_et()
        if n >= target_et:
            return
        # sleep in short chunks so Ctrl+C is responsive
        time.sleep(min(1.0, (target_et - n).total_seconds()))


def is_trading_day(trading_client: TradingClient) -> bool:
    clock = trading_client.get_clock()
    return bool(clock.is_open) or bool(clock.next_open)  # simple sanity check


def pick_shortable_symbol(trading_client: TradingClient, candidates: List[str]) -> str:
    """
    Chooses the first symbol that is tradable + shortable on your account.
    Alpaca assets expose .shortable (bool) and .tradable (bool). :contentReference[oaicite:0]{index=0}
    """
    for sym in candidates:
        asset = trading_client.get_asset(sym)
        if getattr(asset, "tradable", False) and getattr(asset, "shortable", False):
            return sym
    raise RuntimeError(f"No shortable+tradable symbol found in candidates: {candidates}")


def get_bars(
    data_client: StockHistoricalDataClient,
    symbol: str,
    start_utc: datetime,
    end_utc: datetime,
    timeframe: TimeFrame,
) -> List[Bar]:
    req = StockBarsRequest(
        symbol_or_symbols=symbol,
        timeframe=timeframe,
        start=start_utc,
        end=end_utc,
    )
    resp = data_client.get_stock_bars(req)
    df = resp.df
    if df is None or len(df) == 0:
        return []

    # df index usually: (symbol, timestamp). Normalize.
    if "symbol" in df.index.names:
        df_sym = df.xs(symbol)
    else:
        df_sym = df

    bars: List[Bar] = []
    for ts, row in df_sym.iterrows():
        # ts is timezone-aware (often UTC); ensure UTC aware
        ts_dt = ts.to_pydatetime()
        if ts_dt.tzinfo is None:
            ts_dt = ts_dt.replace(tzinfo=timezone.utc)
        bars.append(Bar(t=ts_dt, o=float(row["open"]), h=float(row["high"]), l=float(row["low"]), c=float(row["close"])))
    bars.sort(key=lambda b: b.t)
    return bars


def detect_fvg_signal(
    last3: List[Bar],
    opening_high: float,
    opening_low: float,
) -> Optional[FVGSignal]:
    """
    Simple 3-bar FVG definition:
      Bullish FVG if bar2.low > bar0.high
      Bearish FVG if bar2.high < bar0.low
    Then require it to be beyond the opening range boundary:
      Bullish: bar2.low > opening_high
      Bearish: bar2.high < opening_low
    """
    if len(last3) != 3:
        return None

    b0, b1, b2 = last3  # b2 is newest
    # Bullish gap
    if b2.l > b0.h and b2.l > opening_high:
        entry = b2.c  # use close of breakout bar as a proxy
        stop = b2.l   # "first candle outside range" low
        risk = entry - stop
        if risk <= 0:
            return None
        tp = entry + 2.0 * risk
        return FVGSignal(direction="long", breakout_bar=b2, stop_price=stop, take_profit=tp)

    # Bearish gap
    if b2.h < b0.l and b2.h < opening_low:
        entry = b2.c
        stop = b2.h   # "first candle outside range" high
        risk = stop - entry
        if risk <= 0:
            return None
        tp = entry - 2.0 * risk
        return FVGSignal(direction="short", breakout_bar=b2, stop_price=stop, take_profit=tp)

    return None


def has_open_position(trading_client: TradingClient, symbol: str) -> bool:
    try:
        pos = trading_client.get_open_position(symbol)
        # If it exists and qty != 0, we have a position
        return float(pos.qty) != 0.0
    except Exception:
        return False


def submit_bracket(
    trading_client: TradingClient,
    symbol: str,
    qty: int,
    signal: FVGSignal,
) -> None:
    """
    Bracket order example in alpaca-py docs. :contentReference[oaicite:1]{index=1}
    """
    side = OrderSide.BUY if signal.direction == "long" else OrderSide.SELL
    order = MarketOrderRequest(
        symbol=symbol,
        qty=qty,
        side=side,
        time_in_force=TimeInForce.DAY,
        order_class=OrderClass.BRACKET,
        take_profit=TakeProfitRequest(limit_price=round(signal.take_profit, 2)),
        stop_loss=StopLossRequest(stop_price=round(signal.stop_price, 2)),
    )
    trading_client.submit_order(order_data=order)


def main() -> None:
    cfg = read_creds("creds.toml")
    trading_client = TradingClient(cfg["key_id"], cfg["secret_key"], paper=bool(cfg.get("paper", True)))
    data_client = StockHistoricalDataClient(cfg["key_id"], cfg["secret_key"])

    # Change these to whatever you want to trade
    candidates = ["SPY", "AAPL", "TSLA", "NVDA", "AMD"]
    symbol = pick_shortable_symbol(trading_client, candidates)

    qty = 1                 # adjust position sizing yourself
    poll_seconds = 2.0      # polling loop (we still only act on new minute bars)
    no_new_entries_after = (15, 55)  # ET (avoid late entries)

    print(f"[config] paper={cfg.get('paper', True)} symbol={symbol} qty={qty}")

    while True:
        # Wait for market open day and time
        clock = trading_client.get_clock()
        if not clock.is_open:
            next_open = clock.next_open.astimezone(ET)
            print(f"[wait] market closed. next open: {next_open.isoformat()}")
            sleep_until(next_open + timedelta(seconds=1))
            continue

        # Align to 09:30 ET of the current day
        today_et = now_et().date()
        open_930 = datetime(today_et.year, today_et.month, today_et.day, 9, 30, tzinfo=ET)
        # If it's already past 9:30, just proceed; otherwise wait until 9:30
        if now_et() < open_930:
            print(f"[wait] waiting until 09:30 ET: {open_930.isoformat()}")
            sleep_until(open_930)

        # Fetch first 5-minute bar: 09:30–09:35 ET
        start_et = open_930
        end_et = open_930 + timedelta(minutes=5)
        start_utc = start_et.astimezone(timezone.utc)
        end_utc = end_et.astimezone(timezone.utc)

        five_min = get_bars(
            data_client=data_client,
            symbol=symbol,
            start_utc=start_utc,
            end_utc=end_utc,
            timeframe=TimeFrame(5, TimeFrameUnit.Minute),
        )

        if not five_min:
            print("[warn] no 5-min bar yet; retrying in 5s")
            time.sleep(5)
            continue

        opening = five_min[0]
        opening_high, opening_low = opening.h, opening.l
        print(f"[OR] {symbol} 09:30–09:35 ET high={opening_high:.2f} low={opening_low:.2f}")

        # Switch to 1-min scanning from 09:35
        scan_from_et = end_et
        scan_from_utc = scan_from_et.astimezone(timezone.utc)

        seen_minute_ts: Optional[datetime] = None
        last_bars: List[Bar] = []

        print("[scan] starting 1-min scan...")

        while True:
            n = now_et()
            # Stop at EOD (market close)
            clock = trading_client.get_clock()
            if not clock.is_open:
                print("[eod] market closed; restarting outer loop for next session")
                break

            # No new entries late in the day, but still let existing brackets manage exits
            if (n.hour, n.minute) >= no_new_entries_after and not has_open_position(trading_client, symbol):
                print("[eod] reached cutoff for new entries; waiting for close")
                time.sleep(10)
                continue

            # If we have a position open, just wait until it's closed (TP/SL via bracket)
            if has_open_position(trading_client, symbol):
                time.sleep(2)
                continue

            # Pull recent 1-min bars (from scan_from_utc to now)
            bars_1m = get_bars(
                data_client=data_client,
                symbol=symbol,
                start_utc=scan_from_utc,
                end_utc=datetime.now(timezone.utc),
                timeframe=TimeFrame(1, TimeFrameUnit.Minute),
            )

            # Keep only new minute bars
            new_bars = []
            for b in bars_1m:
                if seen_minute_ts is None or b.t > seen_minute_ts:
                    new_bars.append(b)

            if new_bars:
                seen_minute_ts = new_bars[-1].t
                for b in new_bars:
                    last_bars.append(b)
                    last_bars = last_bars[-3:]  # keep last 3 for FVG detection

                    if len(last_bars) == 3:
                        sig = detect_fvg_signal(last_bars, opening_high, opening_low)
                        if sig is not None:
                            print(
                                f"[signal] {sig.direction.upper()} @ {b.t.astimezone(ET).strftime('%H:%M')} "
                                f"stop={sig.stop_price:.2f} tp={sig.take_profit:.2f}"
                            )
                            try:
                                submit_bracket(trading_client, symbol, qty, sig)
                                print("[order] bracket submitted")
                            except Exception as e:
                                print(f"[error] submit_order failed: {e}")
                            # After submitting, go back to loop; bracket manages exit.
                            break

            time.sleep(poll_seconds)

        # Wait a bit before trying the next day/session
        time.sleep(5)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n[exit] stopped by user")
