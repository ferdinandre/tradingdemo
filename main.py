import mylogger
from typing import List
import dataapi
from models import FVG, Candle, PositionState, ExecCfg
import sizing
from time_mgmt import TimeMgr
import fvg
import live_exec
import tomllib
import datetime
#TODO: add logging (python logging module) but keep _logger.log statements, with log levels and timestamps. For now, _logger.logs are fine for simplicity.
with open("creds.toml", "rb") as f:
    toml = tomllib.load(f)

_logger = mylogger.Logger()

API_KEY = toml["key_id"]

API_SECRET = toml["secret_key"]

SHORT_ENABLED = bool(toml["short_enabled"])

_logger.log(SHORT_ENABLED)

SYMBOL = "AAPL"

_logger.log(SHORT_ENABLED)

cfg = ExecCfg(
    risk_pct=0.005,            # 0.25% per trade (live-safe with high frequency)
    max_pos_value_mult=1.0,     # don’t exceed 1x equity notional on longs
    alpha=2.0,
    r_max=2.0,
    beta=1.5,
    r_stop=1.0,
    enable_loss_ladder=False    # turn off if it chops too much
)

market_data = dataapi.AlpacaMarketData(api_key=API_KEY, api_secret=API_SECRET, feed="sip", logger=_logger)

paper_trading = dataapi.AlpacaPaperTrading(api_key=API_KEY, api_secret=API_SECRET, _logger=_logger)

timemgr = TimeMgr()

executor = live_exec.LiveExecutor(paper_trading=paper_trading, timemgr=timemgr, cfg=cfg, logger=_logger)

fvg_stack: List[FVG] = []

today_1st_5min = None

pos: PositionState | None = None 
candle0: Candle | None = None #third youngest candle
candle1: Candle | None = None #second youngest candle

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

def main():
    needs_historical = False
    if timemgr.current_dt < timemgr.today_930 or timemgr.current_dt > timemgr.today_1630:
        _logger.log("Trading hasnt begun yet today, waiting until")
        if timemgr.current_dt < timemgr.today_930:
            _logger.log("Today 09:30 EST")
            timemgr.wait_until(timemgr.today_930)
        elif timemgr.current_dt > timemgr.today_1630:
            _logger.log("Tomorrow 09:30 EST")
            timemgr.wait_until(timemgr.next_day_930)
    else:
        _logger.log("trading has begun")
        needs_historical = True
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
    while trading:
        c = market_data.get_latest_1min_candle(SYMBOL)
        just_entered = False
        
        if need_to_enter:
            _logger.log(f"Attempting to enter pos at: {datetime.datetime.now()}")
            side = "long" if candle1.high > candle1 .low else "short"
            ##TODO
            enter_price = executor.get_entry_price(md = market_data, symbol= SYMBOL,side=side)
            current_equity = float(paper_trading.get_account()["equity"])
            qty = sizing.compute_live_qty(
                paper_trading=paper_trading,
                cfg=cfg,
                entry=enter_price,
                stop = candle1.low if candle1.low < candle1.high else candle1.high,
                side=fvg_stack[-1].dir,
                _logger = _logger
            )
            _logger.log(f"Computed quantity: {qty}")
            ##TODO
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
                just_entered = True
                
        
        if not just_entered:
            account = paper_trading.get_account()
            current_equity = float(account["equity"])
            bp = float(account["buying_power"])
            _logger.log(f"Got candle w timestamp: {c.ts}")
            _logger.log(f"Current equity: {current_equity} | BP: {bp}")

            fvg.stack_pop_invalidated(fvg_stack, c.low, c.high)
            
            #Manage existing position
            if in_position and pos is not None:
                # 1) hard exit
                reason = executor.hard_exit(
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
                    executor.cut_loss(
                        paper=paper_trading, pos=pos,
                        bar_high=c.high, bar_low=c.low,
                        cfg=cfg
                    )
                    if pos.remaining_qty <= 0:
                        in_position = False
                        pos = None
                    else:
                        # 3) take profit
                        executor.take_profit(
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
                        _logger.log(f"Detected FVG at {datetime.datetime.now()}")
                        if fvg.should_push(fvg_stack, current_fvg.dir, gap_low=current_fvg.gap_low, gap_high=current_fvg.gap_high):
                            fvg_stack.append(current_fvg)
                            _logger.log("FVG pushed to stack")
                            if current_fvg.dir == "bear" and not SHORT_ENABLED:
                                _logger.log("Shorting not enabled")
                            else:
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
            break
            
            
if __name__ == "__main__":
        main()
