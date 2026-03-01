from __future__ import annotations
import datetime

from dataapi import AlpacaPaperTrading
from mathmagic import frac_cut_norm_log, frac_closed_norm_log
from typing import Optional, Literal
from models import Candle, PositionState, ExecCfg, Side
from dataclasses import dataclass
import time
from typing import Any, Optional

from time_mgmt import TimeMgr



class OrderNotFilled(RuntimeError):
    pass


class OrderRejected(RuntimeError):
    pass


@dataclass
class FillResult:
    order_id: str
    symbol: str
    side: str
    requested_qty: float
    filled_qty: float
    avg_fill_price: Optional[float]
    status: str
    order_raw: Any


def _safe_float(x) -> Optional[float]:
    if x is None:
        return None
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def _safe_str(x) -> str:
    return "" if x is None else str(x)



# --------- order direction helpers ---------
# IMPORTANT: You may need to flip these depending on how your AlpacaPaperTrading implements `long=`.
# Assumption used here:
#   place_market_order(..., long=True)  => buy (increase long / cover short)
#   place_market_order(..., long=False) => sell (reduce long / increase short)

def open_long_flag(side: Side) -> bool:
    # to OPEN long => buy; to OPEN short => sell/short
    return True if side == "long" else False

def close_long_flag(side: Side) -> bool:
    # to CLOSE long => sell; to CLOSE short => buy/cover
    return False if side == "long" else True

def place_and_confirm_fill(
    paper: AlpacaPaperTrading,
    *,
    symbol: str,
    qty: float,
    side: str,  # "long" / "short" or whatever your open_long_flag expects
    extended_hours: bool,
    timeout_s: float = 20.0,
    poll_s: float = 0.5,
) -> FillResult:
    """
    1) Place market order
    2) Poll order until terminal state
    3) Return fill info (avg_fill_price, filled_qty, status)
    """
    print(f"Placing market order for {qty} of {symbol} ({side}), extended_hours={extended_hours}")

    # 1) place
    placed = paper.place_market_order(
        symbol=symbol,
        qty=qty,
        long=open_long_flag(side),
        extended_hours=extended_hours,
    )

    # You may get back an object or a dict; support both
    order_id = getattr(placed, "id", None) or (placed.get("id") if isinstance(placed, dict) else None)
    if not order_id:
        raise RuntimeError(f"place_market_order returned no order id: {placed!r}")

    deadline = time.time() + timeout_s
    last = None

    # 2) poll
    while time.time() < deadline:
        last = paper.get_order_by_id(order_id)
        status = _safe_str(getattr(last, "status", None) or (last.get("status") if isinstance(last, dict) else None)).lower()

        # Terminal success
        if status in {"filled"}:
            filled_qty = _safe_float(getattr(last, "filled_qty", None) or (last.get("filled_qty") if isinstance(last, dict) else None)) or 0.0
            avg_fill_price = _safe_float(getattr(last, "filled_avg_price", None) or (last.get("filled_avg_price") if isinstance(last, dict) else None))
            return FillResult(
                order_id=order_id,
                symbol=symbol,
                side=side,
                requested_qty=float(qty),
                filled_qty=float(filled_qty),
                avg_fill_price=avg_fill_price,
                status=status,
                order_raw=last,
            )

        # Terminal failure
        if status in {"rejected"}:
            reason = getattr(last, "reject_reason", None) or (last.get("reject_reason") if isinstance(last, dict) else None)
            raise OrderRejected(f"Order rejected ({order_id}): {reason or last!r}")

        if status in {"canceled", "cancelled", "expired"}:
            raise OrderNotFilled(f"Order not filled; status={status} ({order_id}). Last={last!r}")

        # Still working: new/accepted/partially_filled/pending_* etc.
        time.sleep(poll_s)

    # Timeout: check if partially filled
    status = _safe_str(getattr(last, "status", None) or (last.get("status") if isinstance(last, dict) else None)).lower()
    filled_qty = _safe_float(getattr(last, "filled_qty", None) or (last.get("filled_qty") if isinstance(last, dict) else None)) or 0.0
    avg_fill_price = _safe_float(getattr(last, "filled_avg_price", None) or (last.get("filled_avg_price") if isinstance(last, dict) else None))

    raise OrderNotFilled(
        f"Timeout waiting for fill ({timeout_s}s). status={status}, filled_qty={filled_qty}, avg_fill_price={avg_fill_price}, last={last!r}"
    )


def get_entry_price(md, symbol: str, side: Side) -> float:
    resp = md._get_latest_quote(symbol)

    quotes = resp.get("quotes", {})
    q = quotes.get(symbol, {})

    bid = q.get("bp")
    ask = q.get("ap")

    # LONG: size from ASK (worst case)
    if side == "long" and ask is not None and ask > 0:
        return float(ask)

    # SHORT: size from BID (worst case)
    if side == "short" and bid is not None and bid > 0:
        return float(bid)

    # Fallback: last trade
    trade_resp = md.get_latest_trade(symbol)
    trade = trade_resp.get("trade", {})
    px = trade.get("p")

    if px is None:
        raise RuntimeError("Could not determine entry price from quote or trade")

    return float(px)

# --------- core actions ---------

def enter_position(
    *,
    paper : AlpacaPaperTrading,                 # AlpacaPaperTrading
    symbol: str,
    fvg_dir: Literal["bull", "bear"],
    entry_price: float,    # choose from quote mid/last, etc. passed by caller
    signal_low: float,     # b2.low
    signal_high: float,    # b2.high
    tp_r: float,           # fixed TP multiple
    equity: float,
    cfg: ExecCfg,
    extended_hours: bool = False,
    qty: float = None,         # if None, compute from equity/risk; else use as-is
) -> Optional[PositionState]:
    """
    Enter on market, compute stop/tp from signal candle extreme and tp_r.
    Returns PositionState if order sent, else None.
    """
    side: Side = "long" if fvg_dir == "bull" else "short"

    if side == "long":
        stop = float(signal_low)
        risk_ps = entry_price - stop
        if risk_ps <= 0:
            return None
        tp = entry_price + tp_r * risk_ps
    else:
        stop = float(signal_high)
        risk_ps = stop - entry_price
        if risk_ps <= 0:
            return None
        tp = entry_price - tp_r * risk_ps
    print(f"Entering position {side}, with quantity {qty}")
    if qty <= 0:
        return None

    # Place entry MARKET
    fill = place_and_confirm_fill(
        paper,
        symbol=symbol,
        qty=qty,
        side=side,
        extended_hours=extended_hours,
        timeout_s=30,
        poll_s=0.5,
    )
    print("FILLED", fill.symbol, fill.filled_qty, "@", fill.avg_fill_price)

    return PositionState(
        symbol=symbol,
        side=side,
        entry=float(entry_price),
        stop=float(stop),
        tp=float(tp),
        risk_per_share=float(risk_ps),
        init_qty=float(qty),
        remaining_qty=float(qty),
    )


def take_profit(
    *,
    paper,
    pos: PositionState,
    bar_high: float,
    bar_low: float,
    cfg: ExecCfg,
    extended_hours: bool = False,
) -> float:
    """
    Profit ladder: scale out according to normalized log fraction.
    Returns qty_closed_this_call.
    """
    print("Take profit running")
    if pos.remaining_qty <= 0:
        return 0.0

    if pos.risk_per_share <= 0:
        # can't compute R properly; safest is do nothing
        return 0.0

    # compute favorable excursion in R for THIS bar
    if pos.side == "long":
        mfe = float(bar_high) - pos.entry
        cur_r = mfe / pos.risk_per_share
    else:  # short
        mfe = pos.entry - float(bar_low)
        cur_r = mfe / pos.risk_per_share

    if cur_r > pos.max_r_seen:
        pos.max_r_seen = cur_r

    desired_closed_frac = frac_closed_norm_log(pos.max_r_seen, cfg.alpha, cfg.r_max)
    desired_closed_qty = float(pos.init_qty) * desired_closed_frac
    already_closed_qty = pos.init_qty - pos.remaining_qty
    to_close = desired_closed_qty - already_closed_qty

    if to_close <= 0:
        print("Nothing to take")
        return 0.0

    to_close = min(float(to_close), float(pos.remaining_qty))

    # EXIT side is opposite of position side
    exit_side = "short" if pos.side == "long" else "long"

    fill = place_and_confirm_fill(
        paper,
        symbol=pos.symbol,
        qty=to_close,
        side=exit_side,
        extended_hours=extended_hours,
        timeout_s=30,
        poll_s=0.5,
    )

    print("FILLED", fill.symbol, fill.filled_qty, "@", fill.avg_fill_price)

    filled = float(fill.filled_qty or 0.0)
    pos.remaining_qty -= filled

    print(f"Take profit closed {filled} of {pos.symbol} (pos was {pos.side})")
    return filled


def cut_loss(
    *,
    paper,
    pos: PositionState,
    bar_high: float,
    bar_low: float,
    cfg: ExecCfg,
    extended_hours: bool = False,
) -> float:
    """
    Loss ladder: reduce exposure as adverse excursion increases.
    Returns qty_cut_this_call.
    """
    print("Cut loss running")
    if not cfg.enable_loss_ladder:
        return 0.0
    if pos.remaining_qty <= 0:
        return 0.0
    if pos.risk_per_share <= 0:
        return 0.0

    # compute adverse excursion in R for THIS bar (>=0)
    if pos.side == "long":
        mae = pos.entry - float(bar_low)
        neg_r = mae / pos.risk_per_share
    else:  # short
        mae = float(bar_high) - pos.entry
        neg_r = mae / pos.risk_per_share

    if neg_r > pos.max_neg_r_seen:
        pos.max_neg_r_seen = neg_r

    desired_cut_frac = frac_cut_norm_log(pos.max_neg_r_seen, cfg.beta, cfg.r_stop)
    desired_cut_qty = float(pos.init_qty) * desired_cut_frac
    already_cut_qty = pos.init_qty - pos.remaining_qty
    to_cut = desired_cut_qty - already_cut_qty

    if to_cut <= 0:
        print("Nothing to cut")
        return 0.0

    to_cut = min(float(to_cut), float(pos.remaining_qty))

    # EXIT side is opposite of position side
    exit_side = "short" if pos.side == "long" else "long"

    fill = place_and_confirm_fill(
        paper,
        symbol=pos.symbol,
        qty=to_cut,
        side=exit_side,
        extended_hours=extended_hours,
        timeout_s=30,
        poll_s=0.5,
    )

    filled = float(fill.filled_qty or 0.0)
    pos.remaining_qty -= filled

    print(f"Cut loss closed {filled} of {pos.symbol} (pos was {pos.side})")
    return filled

def hard_exit(
    *,
    paper,
    pos: PositionState,
    bar_high: float,
    bar_low: float,
    extended_hours: bool = False,
    timemgr: TimeMgr,
) -> Optional[str]:
    """
    Enforce stop/TP/EOD exits with MARKET orders.
    Returns exit_reason if position flattened else None.
    """
    print("hard exit running")
    if pos.remaining_qty <= 0:
        return "flat"

    h = float(bar_high)
    l = float(bar_low)

    # EXIT side is opposite of current position
    exit_side = "short" if pos.side == "long" else "long"

    def _exit_all(reason: str) -> str:
        fill = place_and_confirm_fill(
            paper,
            symbol=pos.symbol,
            qty=float(pos.remaining_qty),
            side=exit_side,
            extended_hours=extended_hours,
            timeout_s=30,
            poll_s=0.5,
        )

        filled = float(fill.filled_qty or 0.0)
        pos.remaining_qty -= filled

        # If we didn't fully flatten (partial fill), keep position open.
        if pos.remaining_qty > 0:
            print(f"Hard-exit {reason}: PARTIAL fill {filled}, remaining {pos.remaining_qty}")
            return f"{reason}_partial"

        pos.remaining_qty = 0.0
        print(f"Hard-exit {reason}: FLAT @ {fill.avg_fill_price}")
        return reason

    # stop-first (conservative)
    stop_hit = (l <= pos.stop) if pos.side == "long" else (h >= pos.stop)
    if stop_hit:
        return _exit_all("stop")

    tp_hit = (h >= pos.tp) if pos.side == "long" else (l <= pos.tp)
    if tp_hit:
        return _exit_all("tp")

    # end of day
    if not timemgr.market_still_open():
        return _exit_all("eod")

    return None
"""Test hard_exit in isolation
timemgr = TimeMgr()
paper_trading = AlpacaPaperTrading(api_key="your_key", api_secret="your_secret")
pos = PositionState(
    symbol="AAPL",
    side="long",
    entry=150.0,
    stop=145.0,
    tp=160.0,
    risk_per_share=5.0,
    init_qty=10.0,
    remaining_qty=10.0,
)

next_candle = Candle(symbol="AAPL", ts=datetime.datetime.now(datetime.timezone.utc),
    open=151.0, high=161.0, low=149.0, close=155.0, volume=1000000, vwap=155.0, trade_count=100)

reason = hard_exit(
    paper=paper_trading,
    pos=pos,
    bar_high=next_candle.high,
    bar_low=next_candle.low,
    extended_hours=False,
    timemgr=timemgr,
)

print(f"Exit reason: {reason}, remaining qty: {pos.remaining_qty}")
"""