from __future__ import annotations
from typing import Optional, Dict
from math import log
from typing import List
from models import FVG, Candle
from pandas import Timestamp

def detect_fvg(b0: Candle, b1: Candle, b2: Candle) -> Optional[FVG]:
    # b1 is unused in classic 3-bar FVG detection; keep it for signature clarity if you want
    high0 = float(b0.high)
    low0  = float(b0.low)
    low2  = float(b2.low)
    high2 = float(b2.high)

    ts = b2.ts
    created_ts = ts if isinstance(ts, Timestamp) else Timestamp(ts) if ts is not None else Timestamp.utcnow()

    if low2 > high0:
        return FVG(dir="bull", gap_low=high0, gap_high=low2, created_ts=created_ts)

    if high2 < low0:
        return FVG(dir="bear", gap_low=high2, gap_high=low0, created_ts=created_ts)

    return None


def should_push(stack: List[FVG], new_dir: str, gap_low: float, gap_high: float) -> bool:
    if not stack:
        return abs(gap_high - gap_low) > 0.02

    new_size = gap_high - gap_low
    top = stack[-1]
    old_size = top.gap_high - top.gap_low

    if new_dir != top.dir:
        # opposite direction while structure still active -> ignore (wait for pops)
        return False

    # same direction continuation condition
    return new_size > old_size

def stack_pop_invalidated(stack: List[FVG], bar_low: float, bar_high: float) -> None:
    # Pop while the top is invalidated (filled)
    while stack:
        top = stack[-1]
        if top.dir == "bull":
            if bar_low <= top.gap_low:
                stack.pop()
                continue
        else:  # bear
            if bar_high >= top.gap_high:
                stack.pop()
                continue
        break

def frac_closed_norm_log(r: float, alpha: float, r_max: float = 2.2) -> float:
    if r <= 0:
        return 0.0
    r = min(r, r_max)
    return log(1.0 + alpha * r) / log(1.0 + alpha * r_max)

def frac_cut_norm_log(neg_r: float, beta: float = 1.5, r_stop: float = 1.0) -> float:
    """
    neg_r   : adverse excursion in R (>= 0)
    beta    : aggressiveness (suggest 1.0–2.5)
    r_stop  : R at which you want 100% of position cut (usually 1.0)

    returns : fraction of ORIGINAL position to cut (0..1)
    """
    if neg_r <= 0:
        return 0.0

    neg_r = min(neg_r, r_stop)

    return log(1.0 + beta * neg_r) / log(1.0 + beta * r_stop)