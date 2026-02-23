from math import floor
from models import Side

def compute_qty(
    *,
    equity: float,
    risk_pct: float,
    risk_per_share: float,
    entry: float,
    side: Side,
    max_pos_value_mult: float,
) -> float:
    """
    Risk-based size with optional notional cap for longs.
    Returns qty as float (works for fractional if your broker supports it; otherwise cast to int).
    """
    if risk_per_share <= 0:
        return 0.0

    risk_dollars = equity * risk_pct
    qty = risk_dollars / risk_per_share

    # optional notional cap for longs (avoid insane sizing with tight stops)
    if side == "long":
        max_notional = equity * max_pos_value_mult
        qty_cap = max_notional / max(entry, 1e-9)
        qty = min(qty, qty_cap)

    # keep at least 1 share (or 1 unit) if possible
    if qty < 1.0:
        qty = 1.0

    return float(qty)
