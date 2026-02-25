from __future__ import annotations

from mathmagic import frac_cut_norm_log, frac_closed_norm_log
from typing import Optional, Literal
from models import PositionState, ExecCfg, Side
from sizing import compute_qty
import time_mgmt




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


def get_entry_price(md, symbol: str) -> float:
    resp = md._get_latest_quote(symbol)

    quotes = resp.get("quotes", {})
    q = quotes.get(symbol, {})

    bid = q.get("bp")
    ask = q.get("ap")

    # Prefer mid-price if NBBO is valid
    if bid is not None and ask is not None and bid > 0 and ask > 0 and ask >= bid:
        return (float(bid) + float(ask)) / 2.0

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
    paper,                 # AlpacaPaperTrading
    symbol: str,
    fvg_dir: Literal["bull", "bear"],
    entry_price: float,    # choose from quote mid/last, etc. passed by caller
    signal_low: float,     # b2.low
    signal_high: float,    # b2.high
    tp_r: float,           # fixed TP multiple
    equity: float,
    cfg: ExecCfg,
    extended_hours: bool = False,
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

    qty = compute_qty(
        equity=equity,
        risk_pct=cfg.risk_pct,
        risk_per_share=risk_ps,
        entry=entry_price,
        side=side,
        max_pos_value_mult=cfg.max_pos_value_mult,
    )
    print(f"Entering position {side}, with quantity {qty}")
    if qty <= 0:
        return None

    # Place entry MARKET
    paper.place_market_order(
        symbol=symbol,
        qty=qty,
        long=open_long_flag(side),
        extended_hours=extended_hours,
    )

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

    # compute favorable excursion in R for THIS bar
    if pos.side == "long":
        mfe = float(bar_high) - pos.entry
        cur_r = mfe / pos.risk_per_share
    else:
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

    to_close = min(to_close, pos.remaining_qty)

    # Execute immediate reduction using MARKET (since you have no cancel/replace interface).
    paper.place_market_order(
        symbol=pos.symbol,
        qty=to_close,
        long=close_long_flag(pos.side),
        extended_hours=extended_hours,
    )
    print(f"TAke profit closing position {pos.side}, quantity: {to_close}")
    pos.remaining_qty -= to_close
    return float(to_close)


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
    print("Cut loss runnig")
    if not cfg.enable_loss_ladder:
        return 0.0
    if pos.remaining_qty <= 0:
        return 0.0

    # compute adverse excursion in R for THIS bar (>=0)
    if pos.side == "long":
        mae = pos.entry - float(bar_low)
        neg_r = mae / pos.risk_per_share
    else:
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

    to_cut = min(to_cut, pos.remaining_qty)

    # Execute reduction using MARKET
    paper.place_market_order(
        symbol=pos.symbol,
        qty=to_cut,
        long=close_long_flag(pos.side),
        extended_hours=extended_hours,
    )
    print(f"Cut loss closing position {pos.side}, quantity: {to_cut}")
    pos.remaining_qty -= to_cut
    return float(to_cut)


def hard_exit(
    *,
    paper,
    pos: PositionState,
    bar_high: float,
    bar_low: float,
    extended_hours: bool = False,
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


    # stop-first (conservative)
    stop_hit = (l <= pos.stop) if pos.side == "long" else (h >= pos.stop)
    if stop_hit:
        paper.place_market_order(
            symbol=pos.symbol,
            qty=pos.remaining_qty,
            long=close_long_flag(pos.side),
            extended_hours=extended_hours,
        )
        pos.remaining_qty = 0.0
        return "stop"

    tp_hit = (h >= pos.tp) if pos.side == "long" else (l <= pos.tp)
    if tp_hit:
        paper.place_market_order(
            symbol=pos.symbol,
            qty=pos.remaining_qty,
            long=close_long_flag(pos.side),
            extended_hours=extended_hours,
        )
        pos.remaining_qty = 0.0
        return "tp"

    # end of day
    if time_mgmt.market_closed_yet():
        paper.place_market_order(
            symbol=pos.symbol,
            qty=pos.remaining_qty,
            long=close_long_flag(pos.side),
            extended_hours=extended_hours,
        )
        pos.remaining_qty = 0.0
        return "eod"

    return None