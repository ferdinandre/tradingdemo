#!/usr/bin/env python3
"""
Live trading bot: Overextension Reversal Strategy.
Mirrors backtest_second.py exactly.

State machine per candle (when flat):
  IDLE → WAITING_BREAK → WAITING_RETRACE → WAITING_ENTRY → enter

Credentials read from .env in the current directory.
Interruptable by SIGINT / SIGTERM (graceful position flatten on exit).
"""
from __future__ import annotations

import os
import signal
import threading
from dataclasses import dataclass
from enum import Enum, auto
from typing import Optional

import numpy as np

import dataapi
import live_exec
import mylogger
import pos_manager_loop
import sizing
from models import ExecCfg, PositionState
from shared_pos_state import SharedPosState
from time_mgmt import TimeMgr


# ── credentials ───────────────────────────────────────────────────────────────

def _load_dotenv(path: str = ".env") -> None:
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, _, val = line.partition("=")
                    os.environ.setdefault(key.strip(), val.strip())
    except FileNotFoundError:
        pass

_load_dotenv()

API_KEY       = os.environ["ALPACA_API_KEY"]
API_SECRET    = os.environ["ALPACA_SECRET_KEY"]
SHORT_ENABLED = os.environ.get("SHORT_ENABLED", "false").lower() in ("1", "true", "yes", "on", "enabled")

SYMBOL = "SPY"

# ── strategy parameters (match backtest_second.py defaults) ──────────────────

EXT_LOOKBACK     = 15    # bars in rolling window
CONFIRM_BARS     = 3     # extreme must be within last N bars
EXT_THRESHOLD    = 0.50  # min extension size in price units
ZONE_BARS        = 3     # bars forming the zone around the extreme
BREAK_TIMEOUT    = 20    # give up waiting for break after N bars
RETRACE_TIMEOUT  = 30    # give up waiting for 50% retrace after N bars
ENTRY_TIMEOUT    = 10    # give up waiting for entry confirmation after N bars
MAX_TRADES_PER_DAY = 4

cfg = ExecCfg(
    risk_pct=0.01,
    max_pos_value_mult=1.0,
    enable_loss_ladder=False,
)

# ── state machine types ───────────────────────────────────────────────────────

class State(Enum):
    IDLE            = auto()
    WAITING_BREAK   = auto()
    WAITING_RETRACE = auto()
    WAITING_ENTRY   = auto()


@dataclass
class Setup:
    direction:  str    # "up" → short trade | "down" → long trade
    ext_start:  float  # price at the beginning of the extension (TP target)
    ext_end:    float  # price at the extreme
    retrace_50: float  # midpoint of the extension
    zone_high:  float  # zone high around the extreme
    zone_low:   float  # zone low  around the extreme


def _detect_extension(
    highs: list[float],
    lows:  list[float],
    confirm_bars:  int,
    ext_threshold: float,
) -> Optional[dict]:
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
        if idx_max < n - confirm_bars:
            return None
        return {"direction": "up",   "ext_start": float(la[idx_min]), "ext_end": float(ha[idx_max])}
    else:
        if idx_min < n - confirm_bars:
            return None
        return {"direction": "down", "ext_start": float(ha[idx_max]), "ext_end": float(la[idx_min])}


# ── infrastructure ────────────────────────────────────────────────────────────

_logger       = mylogger.Logger()
market_data   = dataapi.AlpacaMarketData(api_key=API_KEY, api_secret=API_SECRET, feed="sip", logger=_logger)
paper_trading = dataapi.AlpacaPaperTrading(api_key=API_KEY, api_secret=API_SECRET, _logger=_logger)
timemgr       = TimeMgr()
executor      = live_exec.LiveExecutor(paper_trading=paper_trading, timemgr=timemgr, cfg=cfg, logger=_logger)
pos_state     = SharedPosState()
position_mgr_stop = threading.Event()

# ── shutdown ──────────────────────────────────────────────────────────────────

shutdown_requested = False

def _handle_shutdown(signum, frame):
    global shutdown_requested
    _logger.log("Shutdown signal received — stopping bot gracefully")
    shutdown_requested = True
    position_mgr_stop.set()

signal.signal(signal.SIGINT,  _handle_shutdown)
signal.signal(signal.SIGTERM, _handle_shutdown)


# ── main loop ─────────────────────────────────────────────────────────────────

def main():
    # Wait for market open
    if timemgr.current_dt < timemgr.today_931:
        _logger.log("Waiting until 09:31 ET today...")
        timemgr.wait_until(timemgr.today_931)
    elif timemgr.current_dt > timemgr.today_1630:
        _logger.log("Waiting until 09:31 ET tomorrow...")
        timemgr.wait_until(timemgr.next_day_931)
    else:
        _logger.log("Market already open, starting immediately")

    # Start position manager thread (handles stop / TP / EOD every 5 s)
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
            timemgr=timemgr,
            logger=_logger,
        ),
        daemon=False,
    )
    position_mgr_thread.start()

    # State machine
    state      = State.IDLE
    setup:     Optional[Setup] = None
    state_bars = 0

    roll_highs: list[float] = []
    roll_lows:  list[float] = []

    trades_today = 0

    _logger.log(f"Overextension bot started — symbol={SYMBOL} short_enabled={SHORT_ENABLED}")
    _logger.log(f"Warming up rolling window ({EXT_LOOKBACK} bars needed)...")

    while timemgr.market_still_open() and not shutdown_requested:

        # ── fetch candle ─────────────────────────────────────────────────────
        c = market_data.get_latest_1min_candle(SYMBOL)
        _logger.log(f"Candle {c.ts}  O={c.open:.2f} H={c.high:.2f} L={c.low:.2f} C={c.close:.2f}")

        account = paper_trading.get_account()
        _logger.log(f"Equity={float(account['equity']):.2f}  BP={float(account['buying_power']):.2f}")

        # ── update rolling window ────────────────────────────────────────────
        roll_highs.append(c.high)
        roll_lows.append(c.low)
        if len(roll_highs) > EXT_LOOKBACK:
            roll_highs.pop(0)
            roll_lows.pop(0)

        # ── skip state machine while position manager holds an open position ─
        with pos_state.locked() as cur_pos:
            in_position = cur_pos is not None and cur_pos.remaining_qty > 0

        if in_position:
            _logger.log("In position — position manager is handling exits")
            timemgr.wait_until_next_minute(stop_event=position_mgr_stop)
            continue

        # ── state machine ────────────────────────────────────────────────────

        if state == State.IDLE:
            if len(roll_highs) < EXT_LOOKBACK:
                _logger.log(f"Warming up: {len(roll_highs)}/{EXT_LOOKBACK} bars")
            else:
                ext = _detect_extension(roll_highs, roll_lows, CONFIRM_BARS, EXT_THRESHOLD)
                if ext is not None:
                    zone_high  = max(roll_highs[-ZONE_BARS:])
                    zone_low   = min(roll_lows[-ZONE_BARS:])
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
                    _logger.log(
                        f"Extension detected: dir={setup.direction}  "
                        f"range=[{setup.ext_start:.2f}, {setup.ext_end:.2f}]  "
                        f"zone=[{zone_low:.2f}, {zone_high:.2f}]  "
                        f"retrace50={retrace_50:.2f}"
                    )
                else:
                    _logger.log("No extension detected")

        elif state == State.WAITING_BREAK:
            state_bars += 1
            if setup.direction == "up" and c.high > setup.ext_end:
                _logger.log(f"Invalidated: new high {c.high:.2f} > ext_end {setup.ext_end:.2f}")
                state = State.IDLE; setup = None; state_bars = 0
            elif setup.direction == "down" and c.low < setup.ext_end:
                _logger.log(f"Invalidated: new low {c.low:.2f} < ext_end {setup.ext_end:.2f}")
                state = State.IDLE; setup = None; state_bars = 0
            elif state_bars > BREAK_TIMEOUT:
                _logger.log(f"Break timeout ({BREAK_TIMEOUT} bars). Resetting.")
                state = State.IDLE; setup = None; state_bars = 0
            elif setup.direction == "up" and c.close < setup.zone_low:
                _logger.log(f"Break DOWN: close={c.close:.2f} < zone_low={setup.zone_low:.2f}")
                state = State.WAITING_RETRACE; state_bars = 0
            elif setup.direction == "down" and c.close > setup.zone_high:
                _logger.log(f"Break UP: close={c.close:.2f} > zone_high={setup.zone_high:.2f}")
                state = State.WAITING_RETRACE; state_bars = 0
            else:
                _logger.log(
                    f"Waiting for break [{state_bars}/{BREAK_TIMEOUT}]  "
                    f"zone=[{setup.zone_low:.2f}, {setup.zone_high:.2f}]"
                )

        elif state == State.WAITING_RETRACE:
            state_bars += 1
            if state_bars > RETRACE_TIMEOUT:
                _logger.log(f"Retrace timeout ({RETRACE_TIMEOUT} bars). Resetting.")
                state = State.IDLE; setup = None; state_bars = 0
            elif setup.direction == "up" and c.high >= setup.retrace_50:
                _logger.log(f"50% retrace touched: high={c.high:.2f} >= {setup.retrace_50:.2f}")
                state = State.WAITING_ENTRY; state_bars = 0
            elif setup.direction == "down" and c.low <= setup.retrace_50:
                _logger.log(f"50% retrace touched: low={c.low:.2f} <= {setup.retrace_50:.2f}")
                state = State.WAITING_ENTRY; state_bars = 0
            else:
                _logger.log(
                    f"Waiting for retrace to {setup.retrace_50:.2f} [{state_bars}/{RETRACE_TIMEOUT}]"
                )

        elif state == State.WAITING_ENTRY:
            state_bars += 1
            if state_bars > ENTRY_TIMEOUT:
                _logger.log(f"Entry timeout ({ENTRY_TIMEOUT} bars). Resetting.")
                state = State.IDLE; setup = None; state_bars = 0

            elif trades_today >= MAX_TRADES_PER_DAY:
                _logger.log(f"Max trades/day ({MAX_TRADES_PER_DAY}) reached. Resetting.")
                state = State.IDLE; setup = None; state_bars = 0

            else:
                triggered = False
                if setup.direction == "up" and c.close < setup.retrace_50:
                    triggered = True
                    side = "short"
                    sl   = setup.zone_low
                    tp   = setup.ext_start
                elif setup.direction == "down" and c.close > setup.retrace_50:
                    triggered = True
                    side = "long"
                    sl   = setup.zone_high
                    tp   = setup.ext_start

                if triggered:
                    _logger.log(
                        f"Entry triggered ({side.upper()})  "
                        f"sl={sl:.2f}  tp={tp:.2f}  [{state_bars}/{ENTRY_TIMEOUT}]"
                    )
                    try:
                        enter_price = executor.get_entry_price(md=market_data, symbol=SYMBOL, side=side)
                        risk_ps = (sl - enter_price) if side == "short" else (enter_price - sl)

                        if risk_ps <= 0:
                            _logger.log(f"Invalid risk_ps={risk_ps:.4f} at entry={enter_price:.2f}, skipping")
                            state = State.IDLE; setup = None; state_bars = 0
                        elif (side == "short" and tp >= enter_price) or (side == "long" and tp <= enter_price):
                            _logger.log(f"TP={tp:.2f} wrong side of entry={enter_price:.2f}, skipping")
                            state = State.IDLE; setup = None; state_bars = 0
                        else:
                            qty = sizing.compute_live_qty(
                                paper_trading=paper_trading,
                                cfg=cfg,
                                entry=enter_price,
                                stop=sl,
                                side=side,
                                _logger=_logger,
                            )
                            if qty <= 0:
                                _logger.log("Qty=0, skipping entry")
                                state = State.IDLE; setup = None; state_bars = 0
                            else:
                                fill = executor.place_and_confirm_fill(
                                    paper_trading,
                                    symbol=SYMBOL,
                                    qty=qty,
                                    side=side,
                                    extended_hours=False,
                                    timeout_s=30,
                                    poll_s=0.5,
                                )
                                new_pos = PositionState(
                                    symbol=SYMBOL,
                                    side=side,
                                    entry=float(enter_price),
                                    stop=float(sl),
                                    tp=float(tp),
                                    risk_per_share=float(risk_ps),
                                    init_qty=float(qty),
                                    remaining_qty=float(qty),
                                    average_fill_price=fill.avg_fill_price or enter_price,
                                )
                                pos_state.set(new_pos)
                                trades_today += 1
                                _logger.log(
                                    f"Entered {side.upper()} {qty} @ {fill.avg_fill_price:.2f}  "
                                    f"sl={sl:.2f}  tp={tp:.2f}  "
                                    f"trade {trades_today}/{MAX_TRADES_PER_DAY} today"
                                )
                                state = State.IDLE; setup = None; state_bars = 0

                    except Exception as e:
                        _logger.log(f"Entry failed: {e}")
                        state = State.IDLE; setup = None; state_bars = 0
                else:
                    _logger.log(
                        f"Waiting for entry close past {setup.retrace_50:.2f} [{state_bars}/{ENTRY_TIMEOUT}]"
                    )

        timemgr.wait_until_next_minute(stop_event=position_mgr_stop)

    # ── shutdown ──────────────────────────────────────────────────────────────
    _logger.log("Stopping — signalling position manager thread...")
    position_mgr_stop.set()
    position_mgr_thread.join(timeout=30)
    _logger.log("Overextension bot stopped.")


if __name__ == "__main__":
    main()
