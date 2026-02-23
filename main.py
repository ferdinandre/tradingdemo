from typing import List
import dataapi
from models import FVG, Candle, PositionState, ExecCfg
from time_mgmt import TimeMgr
import fvg
import live_exec
import tomllib

with open("creds.toml", "rb") as f:
    toml = tomllib.load(f)

API_KEY = toml["key_id"]

API_SECRET = toml["secret_key"]

print(API_KEY)

market_data = dataapi.AlpacaMarketData(api_key="", api_secret="", feed="iex")

paper_trading = dataapi.AlpacaPaperTrading(api_key="", api_secret="")

timemgr = TimeMgr()

fvg_stack: List[FVG] = []

today_1st_5min = None

in_position = False

pos: PositionState | None = None 
candle0: Candle | None = None #third youngest candle
candle1: Candle | None = None #second youngest candle


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
    candle0 = candle1
    candle1 = candle

def main():
    trading = False
    if not trading:
        if timemgr.current_dt < timemgr.today_930 or timemgr.current_dt > timemgr.today_1630:
            print("Trading hasnt begun yet today, waiting until")
            if timemgr.current_dt < timemgr.today_930:
                print("Today 09:35 EST")
                timemgr.wait_until(timemgr.today_935)
            elif timemgr.current_dt > timemgr.today_1630:
                print("Tomorrow 09:35 EST")
                timemgr.wait_until(timemgr.next_day_935)
        else:
            print("trading has begun")
            timemgr.wait_until(timemgr.today_935)
            trading = True
        today_1st_5min = market_data.get_today_open_5min_candle()
        trading = True  
    else:
        while trading:
            current_equity = float(paper_trading.get_account()["cash"])
            next_candle = market_data.get_latest_1min_candle()
            fvg.stack_pop_invalidated(fvg_stack, next_candle.low, next_candle.high)
            if in_position:
                live_exec.take_profit(paper=paper_trading, pos=pos, bar_high=next_candle.high, bar_low=next_candle.low, cfg=cfg)
                live_exec.cut_loss(paper=paper_trading, pos=pos, bar_high=next_candle.high, bar_low=next_candle.low, cfg=cfg)
                reason = live_exec.hard_exit(
                    paper=paper_trading, pos=pos,
                    bar_high=next_candle.high, bar_low=next_candle.low, bar_close=next_candle.close,
                    time_mgmt=timemgr, cfg=cfg
                )
                if reason is not None:
                    in_position = False
                    pos = None
            else:
                if not (candle0 is None or candle1 is None): 
                    current_fvg = fvg.detect_fvg(candle0, candle1, next_candle)
                    if current_fvg is not None:
                        pushed = False
                        if fvg.should_push(fvg_stack, current_fvg.dir, gap_low=current_fvg.gap_low, gap_high=current_fvg.gap_high):
                            fvg_stack.append(current_fvg)
                            pushed = True
                        if pushed:
                            pos = live_exec.enter_position(
                                paper=paper_trading,
                                symbol="SPY",
                                fvg_dir=current_fvg.dir,
                                entry_price=live_exec.get_entry_price(market_data, "SPY"),
                                signal_low=next_candle.low,
                                signal_high=next_candle.high,
                                tp_r=TP_R,
                                equity=current_equity,
                                cfg=cfg,
                            )
            on_new_candle(next_candle)
            trading = not timemgr.market_closed_yet()
            
if __name__ == "__main__":
    main()
