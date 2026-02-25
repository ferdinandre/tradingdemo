from typing import List
import dataapi
from models import FVG, Candle, PositionState, ExecCfg
from time_mgmt import TimeMgr
import fvg
import live_exec
import tomllib
import datetime

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
    while trading:
        #print(paper_trading.get_account())
        next_candle = market_data.get_latest_1min_candle(SYMBOL)
        if next_candle is None:
            print("No candle yet → skipping iteration")
            timemgr.wait_until_next_minute()
            continue
        current_equity = float(paper_trading.get_account()["cash"]) / 100
        print(f"Current equity {current_equity}")
        fvg.stack_pop_invalidated(fvg_stack, next_candle.low, next_candle.high)

        if in_position:
            live_exec.take_profit(paper=paper_trading, pos=pos, bar_high=next_candle.high, bar_low=next_candle.low, cfg=cfg)
            live_exec.cut_loss(paper=paper_trading, pos=pos, bar_high=next_candle.high, bar_low=next_candle.low, cfg=cfg)
            reason = live_exec.hard_exit(
                paper=paper_trading, pos=pos,
                bar_high=next_candle.high, bar_low=next_candle.low, extended_hours=False
            )
            if reason is not None:
                in_position = False
                pos = None
            print(f"Evaluated current position at {datetime.now()}")
        else:
            if not (candle0 is None or candle1 is None): 
                current_fvg = fvg.detect_fvg(candle0, candle1, next_candle)
                if current_fvg is not None:
                    pushed = False
                    if fvg.should_push(fvg_stack, current_fvg.dir, gap_low=current_fvg.gap_low, gap_high=current_fvg.gap_high):
                        fvg_stack.append(current_fvg)
                        pushed = True
                    if pushed:
                        allow_entry = True
                        if current_fvg.dir == "bear" and not SHORT_ENABLED:
                                print("Shorting not enabled")
                                allow_entry = False
                        else:
                            if allow_entry:
                                pos = live_exec.enter_position(
                                    paper=paper_trading,
                                    symbol=SYMBOL,
                                    fvg_dir=current_fvg.dir,
                                    entry_price=live_exec.get_entry_price(market_data, SYMBOL),
                                    signal_low=next_candle.low,
                                    signal_high=next_candle.high,
                                    tp_r=TP_R,
                                    equity=current_equity,
                                    cfg=cfg,
                                )
                                in_position = pos is not None
                                if pos:
                                    print(f"Entered new position at {datetime.datetime.now()}")
                else:
                    print(f"Finished dry leg at {datetime.datetime.now()}")
            else:
                print(f"Finished dry leg at {datetime.datetime.now()}")
        on_new_candle(next_candle)
        trading = timemgr.market_still_open()
        print(f"set trading to {trading}")
        timemgr.wait_until_next_minute()
        
if __name__ == "__main__":
    main()
