from typing import List
import dataapi
import dummy_dataapi
from models import FVG, Candle, PositionState, ExecCfg, Side, PendingEntry
import sizing
from time_mgmt import TimeMgr
from dummy_timemgmt import DummyTimeMgr
import fvg
import live_exec
import tomllib
import datetime
from datetime import timezone, timedelta
import time
from typing import Optional
#TODO New strategy: first 5 min candle: look for a fvg that breaks throguh first5min low or high, wait for retest? i guess a candle high going within the same fvg, then an engulfing "??" then enter 3:1 RR"
with open("creds.toml", "rb") as f:
    toml = tomllib.load(f)

API_KEY = toml["key_id"]

API_SECRET = toml["secret_key"]

SHORT_ENABLED = bool(toml["short_enabled"])

print(SHORT_ENABLED)

SYMBOL = "AAPL"

print(SHORT_ENABLED)

market_data = dataapi.AlpacaMarketData(api_key=API_KEY, api_secret=API_SECRET, feed="sip")

paper_trading = dataapi.AlpacaPaperTrading(api_key=API_KEY, api_secret=API_SECRET)

timemgr = TimeMgr()

fvg_stack: List[FVG] = []

today_1st_5min = None

pos: PositionState | None = None 
candle0: Candle | None = None #third youngest candle
candle1: Candle | None = None #second youngest candle

def print_ohlc(candle: Candle) -> None:
    if candle is None:
        print("Candle: None")
        return
    print(
        f"[{candle.symbol} {candle.ts}] "
        f"O={candle.open:.2f} "
        f"H={candle.high:.2f} "
        f"L={candle.low:.2f} "
        f"C={candle.close:.2f}"
    )

TP_R = 2

cfg = ExecCfg(
    risk_pct=0.0025,            # 0.25% per trade (live-safe with high frequency)
    max_pos_value_mult=1.0,     # don’t exceed 1x equity notional on longs
    alpha=2.0,
    r_max=2.0,
    beta=1.5,
    r_stop=1.0,
    enable_loss_ladder=True,    # turn off if it chops too much
)

def on_new_candle(candle):
    global candle0
    global candle1
    print("Candle 0")
    print_ohlc(candle0)
    print("Candle 1")
    print_ohlc(candle1)
    print("Candle current")
    print_ohlc(candle)
    candle0 = candle1
    candle1 = candle

def _floor_to_minute_utc(dt: datetime) -> datetime:
    return dt.replace(second=0, microsecond=0)

def wait_for_next_minute_and_get_entry(market_data, symbol: str, side: Side) -> float:
    """
    Block until we are inside the next minute, then take first quote as entry proxy.
    - long  -> enter at ask
    - short -> enter at bid
    """
    # wait until the next minute boundary (UTC)
    now = datetime.now(timezone.utc)
    next_min = _floor_to_minute_utc(now) + timedelta(minutes=1)
    sleep_s = (next_min - now).total_seconds()
    if sleep_s > 0:
        time.sleep(sleep_s)

    # now we are at/just after the minute boundary; grab first quote
    q = market_data._get_latest_quote(symbol)  # you already have this
    qq = q.get("quotes", {}).get(symbol, {})
    bid = qq.get("bp")
    ask = qq.get("ap")

    if bid is None or ask is None:
        # fallback: mid if one side missing; last resort close-ish
        if bid is not None:
            return float(bid)
        if ask is not None:
            return float(ask)
        # if quote is empty, you can fallback to last close (not ideal)
        bar = market_data.get_latest_1min_candle(symbol)
        return float(bar.close) if bar else 0.0

    return float(ask) if side == "long" else float(bid)

pending: Optional[PendingEntry] = None

def main():
    if timemgr.current_dt < timemgr.today_930 or timemgr.current_dt > timemgr.today_1630:
        print("Trading hasnt begun yet today, waiting until")
        if timemgr.current_dt < timemgr.today_930:
            print("Today 09:35 EST")
            timemgr.wait_until(timemgr.today_930)
        elif timemgr.current_dt > timemgr.today_1630:
            print("Tomorrow 09:35 EST")
            timemgr.wait_until(timemgr.next_day_935)
    else:
        print("trading has begun")
        timemgr.wait_until(timemgr.today_935)
    trading = True 
    in_position = False
    last_ts = None  # last processed candle timestamp (UTC)

    while trading:
        c = market_data.get_latest_1min_candle(SYMBOL)

        if c is None:
            time.sleep(0.2)
            continue

        # only process each candle once
        if last_ts is not None and c.ts == last_ts:
            time.sleep(0.2)
            continue

        last_ts = c.ts  # mark processed ASAP

        # ---- NOW process candle c immediately ----
        account = paper_trading.get_account()
        current_equity = float(account["equity"])
        bp = float(account["buying_power"])
        print(f"Got candle w timestamp: {c.ts}")
        print(f"Current equity: {current_equity} | BP: {bp}")

        fvg.stack_pop_invalidated(fvg_stack, c.low, c.high)

        if in_position and pos is not None:
            live_exec.take_profit(paper=paper_trading, pos=pos, bar_high=c.high, bar_low=c.low, cfg=cfg)
            live_exec.cut_loss(paper=paper_trading, pos=pos, bar_high=c.high, bar_low=c.low, cfg=cfg)
            reason = live_exec.hard_exit(paper=paper_trading, pos=pos, bar_high=c.high, bar_low=c.low, extended_hours=False)
            if reason is not None:
                in_position = False
                pos = None

        else:
            # update 3-bar window first
            if candle0 is not None and candle1 is not None:
                current_fvg = fvg.detect_fvg(candle0, candle1, c)
                if current_fvg is not None:
                    if fvg.should_push(fvg_stack, current_fvg.dir, gap_low=current_fvg.gap_low, gap_high=current_fvg.gap_high):
                        fvg_stack.append(current_fvg)

                        if current_fvg.dir == "bear" and not SHORT_ENABLED:
                            print("Shorting not enabled")
                        else:
                            side: Side = "long" if current_fvg.dir == "bull" else "short"

                            # ENTRY NOW (right after bar close) -> this is effectively "next bar open" in live time
                            # because you're executing at the boundary when the new minute begins.
                            entry_est = live_exec.get_entry_price(market_data, SYMBOL, side=side)

                            qty = sizing.compute_live_qty(
                                paper_trading=paper_trading,
                                cfg=cfg,
                                side=side,
                                entry_est=entry_est,
                                signal_low=float(c.low),
                                signal_high=float(c.high),
                            )

                            pos = live_exec.enter_position(
                                paper=paper_trading,
                                symbol=SYMBOL,
                                fvg_dir=current_fvg.dir,
                                entry_price=entry_est,
                                signal_low=float(c.low),
                                signal_high=float(c.high),
                                tp_r=TP_R,
                                equity=float(paper_trading.get_account()["equity"]),
                                cfg=cfg,
                                qty=qty,
                                extended_hours=False,
                            )
                            in_position = pos is not None
            else:
                print("Warming up 3-bar window...")

        on_new_candle(c)

        trading = timemgr.market_still_open()
            
            
if __name__ == "__main__":
        main()
