import threading

import mylogger
from typing import List
import dataapi
from models import FVG, Candle, ExecCfg, PositionState
from shared_pos_state import SharedPosState
import sizing
from time_mgmt import TimeMgr
import fvg
import live_exec
import tomllib
import datetime
import pos_manager_loop

with open("creds.toml", "rb") as f:
    toml = tomllib.load(f)

_logger = mylogger.Logger()

API_KEY = toml["key_id"]

API_SECRET = toml["secret_key"]

SHORT_ENABLED = bool(toml["short_enabled"])

_logger.log(SHORT_ENABLED)

SYMBOL = "SPY"


#TODO find the best config somehow
cfg = ExecCfg(
    risk_pct=0.01,            # 1% per trade (live-safe with high frequency)
    max_pos_value_mult=1.0,     # don’t exceed 1x equity notional on longs
    alpha=2.0,
    r_max=2.0,
    beta=1.5,
    r_stop=1.0,
    enable_loss_ladder=False  # turn off if it chops too much
)

market_data = dataapi.AlpacaMarketData(api_key=API_KEY, api_secret=API_SECRET, feed="sip", logger=_logger)

paper_trading = dataapi.AlpacaPaperTrading(api_key=API_KEY, api_secret=API_SECRET, _logger=_logger)

timemgr = TimeMgr()

executor = live_exec.LiveExecutor(paper_trading=paper_trading, timemgr=timemgr, cfg=cfg, logger=_logger)

fvg_stack: List[FVG] = []

pos: PositionState | None = None 

candle0: Candle | None = None #third youngest candle
candle1: Candle | None = None #second youngest candle

pos_state = SharedPosState(initial=pos) # shared variable for position state between main loop and position manager loop
position_mgr_stop = threading.Event()

def print_ohlc(candle: Candle) -> None:
    if candle is None:
        _logger.log("Candle: None")
        return
    _logger.log(
        f"[{candle.symbol} {candle.ts}] "
        f"O={candle.open:.2f} "
        f"H={candle.high:.2f} "
        f"L={candle.low:.2f} "
        f"C={candle.close:.2f}"
    )

TP_R = 2



def on_new_candle(candle, should_print = False):
    global candle0
    global candle1
    if should_print:
        _logger.log("Candle 0")
        print_ohlc(candle0)
        _logger.log("Candle 1")
        print_ohlc(candle1)
        _logger.log("Candle current")
        print_ohlc(candle)
    candle0 = candle1
    candle1 = candle


#TODO implement Take profit and stop loss every 15s within the minute based on quotes
# maybe make it part of wait till next minute 

#TODO accumulation, manipulate, IFVG, Distribution

def main():
    needs_historical = False
    if timemgr.current_dt < timemgr.today_931 or timemgr.current_dt > timemgr.today_1630:
        _logger.log("Trading hasnt begun yet today, waiting until")
        if timemgr.current_dt < timemgr.today_931:
            _logger.log("Today 09:31 EST")
            timemgr.wait_until(timemgr.today_931)
        elif timemgr.current_dt > timemgr.today_1630:
            _logger.log("Tomorrow 09:31 EST")
            timemgr.wait_until(timemgr.next_day_931)
    else:
        _logger.log("trading has begun")
        #needs_historical = True
    trading = True 
    in_position = False
    need_to_enter = False
    
    if needs_historical:
        _logger.log("Getting historical data for today")
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
    position_mgr_thread = threading.Thread(
        target=pos_manager_loop.position_manager_loop,
        kwargs=dict(
            live_executor=executor,
            shared_pos=pos_state,
            stop_event=position_mgr_stop,
            market_data=market_data,
            paper_trading=paper_trading,
            cfg=cfg,
            poll_seconds=5.0,
            timemgr=timemgr
        ),
        daemon=True,
    )
    position_mgr_thread.start()
    trades_made_today = 0
    while trading:

        if need_to_enter:
            if trades_made_today < 4:
            
                _logger.log(f"Attempting to enter pos at: {datetime.datetime.now()}")
                side = "long" if candle1.high > candle1 .low else "short"
                enter_price = executor.get_entry_price(md = market_data, symbol= SYMBOL,side=side)
                current_equity = float(paper_trading.get_account()["equity"])
                qty = sizing.compute_live_qty(
                    paper_trading=paper_trading,
                    cfg=cfg,
                    entry=enter_price,
                    stop = candle1.low if candle1.low < candle1.high else candle1.high,
                    side=fvg_stack[-1].dir,
                    _logger = _logger,
                    
                )
                _logger.log(f"Computed quantity: {qty}")
                pos = executor.enter_position(
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
                need_to_enter = False
                in_position = pos is not None
                if in_position:
                    _logger.log(f"Entered position with quantity {qty}, side: {side}, average fill price: {pos.average_fill_price}")
                    pos_state.set(pos)
            else:
                print("Already over the 4 daytrade limit for today. Not entering anymore trades")
        c = market_data.get_latest_1min_candle(SYMBOL)
        fvg.stack_pop_invalidated(fvg_stack, c.low, c.high)
        account = paper_trading.get_account()
        current_equity = float(account["equity"])
        bp = float(account["buying_power"])
        _logger.log(f"Got candle w timestamp: {c.ts}")
        _logger.log(f"Current equity: {current_equity} | BP: {bp}")
        # update 3-bar window first
        if candle0 is not None and candle1 is not None:
            current_fvg = fvg.detect_fvg(candle0, candle1, c)
            if current_fvg is not None:
                _logger.log(f"Detected FVG at {datetime.datetime.now()}")
                if fvg.should_push(fvg_stack, current_fvg.dir, gap_low=current_fvg.gap_low, gap_high=current_fvg.gap_high):
                    fvg_stack.append(current_fvg)
                    _logger.log("FVG pushed to stack")
                    if current_fvg.dir == "bear" and not SHORT_ENABLED:
                        _logger.log("Shorting not enabled")
                    else:
                        with pos_state.locked() as pos:
                            if pos is None or pos.remaining_qty == 0:
                                need_to_enter = True #Entering on the next bar
                                _logger.log("Entering on next bar")
                else:
                    _logger.log(f"FVG irrelevant (smaller than the previous)")
            else:
                _logger.log(f"No FVG detected at {datetime.datetime.now()}")
        else:
            _logger.log("Warming up 3-bar window...")

        on_new_candle(c, True)
        timemgr.wait_until_next_minute()
        trading = timemgr.market_still_open()
        if not trading:
            position_mgr_stop.set() # signal the position manager thread to stop
            break
            
            
if __name__ == "__main__":
        main()
