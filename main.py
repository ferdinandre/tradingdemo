from typing import List
import dataapi
from models import FVG, Candle, PositionState, ExecCfg, Side, PendingEntry
import sizing
from time_mgmt import TimeMgr
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
    risk_pct=0.005,            # 0.25% per trade (live-safe with high frequency)
    max_pos_value_mult=1.0,     # don’t exceed 1x equity notional on longs
    alpha=2.0,
    r_max=2.0,
    beta=1.5,
    r_stop=1.0,
    enable_loss_ladder=False,    # turn off if it chops too much
)

def on_new_candle(candle, should_print = False):
    global candle0
    global candle1
    if should_print:
        print("Candle 0")
        print_ohlc(candle0)
        print("Candle 1")
        print_ohlc(candle1)
        print("Candle current")
        print_ohlc(candle)
    candle0 = candle1
    candle1 = candle

def main():
    needs_historical = False
    if timemgr.current_dt < timemgr.today_930 or timemgr.current_dt > timemgr.today_1630:
        print("Trading hasnt begun yet today, waiting until")
        if timemgr.current_dt < timemgr.today_930:
            print("Today 09:30 EST")
            timemgr.wait_until(timemgr.today_930)
        elif timemgr.current_dt > timemgr.today_1630:
            print("Tomorrow 09:30 EST")
            timemgr.wait_until(timemgr.next_day_930)
    else:
        print("trading has begun")
        needs_historical = True
    trading = True 
    in_position = False
    need_to_enter = False
    if needs_historical:
        print("Getting historical data for today")
        start = timemgr.today_930
        end = datetime.datetime.now().replace(second=0, microsecond=0)
        start_utc = start.astimezone(timemgr.UTC)
        end_utc = end.astimezone(timemgr.UTC)
        todays_1min_candles = market_data.get_historical_1min_candles(SYMBOL, start_utc=start_utc, end_utc=end_utc)
        for candle in todays_1min_candles:
            fvg.stack_pop_invalidated(fvg_stack, candle.low, candle.high)
            if candle0 is not None and candle1 is not None:
                current_fvg = fvg.detect_fvg(candle0, candle1, candle)
                if current_fvg is not None:
                    if fvg.should_push(fvg_stack, current_fvg.dir, gap_low=current_fvg.gap_low, gap_high=current_fvg.gap_high):
                        fvg_stack.append(current_fvg)
            on_new_candle(candle=candle, should_print=False)
    while trading:
        c = market_data.get_latest_1min_candle(SYMBOL)
        just_entered = False
        
        if need_to_enter:
            print(f"Attempting to enter pos at: {datetime.datetime.now()}")
            side = "long" if candle1.high > candle1 .low else "short"
            enter_price = live_exec.get_entry_price(market_data, SYMBOL,side=side)
            current_equity = float(paper_trading.get_account()["equity"])
            qty = sizing.compute_live_qty(
                paper_trading=paper_trading,
                cfg=cfg,
                entry=enter_price,
                stop = candle1.low if candle1.low < candle1.high else candle1.high,
                side=fvg_stack[-1].dir
            )
            pos = live_exec.enter_position(
                paper=paper_trading,
                symbol=SYMBOL,
                fvg_dir=fvg_stack[-1].dir,
                entry_price=enter_price,
                signal_low=float(candle1.low),
                signal_high=float(candle1.high),
                tp_r=TP_R,
                equity=current_equity,
                cfg=cfg,
                extended_hours=False,
                qty=qty,
            )
            
            in_position = pos is not None
            if in_position:
                print(f"Entered position with quantity {qty}, side: {side}, average fill price: {pos.average_fill_price}")
                just_entered = True
                need_to_enter = False
        
        if not just_entered:
            account = paper_trading.get_account()
            current_equity = float(account["equity"])
            bp = float(account["buying_power"])
            print(f"Got candle w timestamp: {c.ts}")
            print(f"Current equity: {current_equity} | BP: {bp}")

            fvg.stack_pop_invalidated(fvg_stack, c.low, c.high)
            
            #Manage existing position
            if in_position and pos is not None:
                # 1) hard exit
                reason = live_exec.hard_exit(
                    paper=paper_trading,
                    pos=pos,
                    bar_high=c.high,
                    bar_low=c.low,
                    extended_hours=False,
                    timemgr=timemgr
                )
                if reason is not None:
                    in_position = False
                    pos = None
                else:
                    # 2) cut loss (priority over TP)
                    live_exec.cut_loss(
                        paper=paper_trading, pos=pos,
                        bar_high=c.high, bar_low=c.low,
                        cfg=cfg
                    )
                    if pos.remaining_qty <= 0:
                        in_position = False
                        pos = None
                    else:
                        # 3) take profit
                        live_exec.take_profit(
                            paper=paper_trading, pos=pos,
                            bar_high=c.high, bar_low=c.low,
                            cfg=cfg
                        )
                        if pos.remaining_qty <= 0:
                            in_position = False
                            pos = None


            else:
                # update 3-bar window first
                if candle0 is not None and candle1 is not None:
                    current_fvg = fvg.detect_fvg(candle0, candle1, c)
                    if current_fvg is not None:
                        print(f"Detected FVG at {datetime.datetime.now()}")
                        if fvg.should_push(fvg_stack, current_fvg.dir, gap_low=current_fvg.gap_low, gap_high=current_fvg.gap_high):
                            fvg_stack.append(current_fvg)
                            print("FVG pushed to stack")
                            if current_fvg.dir == "bear" and not SHORT_ENABLED:
                                print("Shorting not enabled")
                            else:
                                need_to_enter = True #Entering on the next bar
                                print("Entering on next bar")
                        else:
                            print(f"FVG irrelevant (smaller than the previous)")
                    else:
                        print(f"No FVG detected at {datetime.datetime.now()}")
                else:
                    print("Warming up 3-bar window...")

        on_new_candle(c, True)
        timemgr.wait_until_next_minute()
        trading = timemgr.market_still_open()
        if not trading:
            break
            
            
if __name__ == "__main__":
        main()
