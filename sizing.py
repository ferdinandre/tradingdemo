from models import Side, ExecCfg, PositionState
from typing import Optional
import live_exec


def compute_live_qty(
    *,
    paper_trading,
    cfg: ExecCfg,
    entry: float,
    stop: float,
    side: Side,
) -> float:
    acct = paper_trading.get_account()
    equity = float(acct["equity"])
    bp = float(acct["buying_power"])

    rps = (entry - stop) if side == "long" else (stop - entry)
    if rps <= 0:
        return 0.0

    return compute_qty(
        equity=equity,
        risk_pct=cfg.risk_pct,
        risk_per_share=rps,
        entry=entry,
        side=side,
        max_pos_value_mult=cfg.max_pos_value_mult,
        buying_power=bp,
        bp_buffer=0.995,
        allow_fractional=True,
    )

def compute_qty(
    *,
    equity: float,
    risk_pct: float,
    risk_per_share: float,
    entry: float,
    side: Side,
    max_pos_value_mult: float,
    buying_power: float | None = None,   # pass from account
    bp_buffer: float = 0.995,            # leave ~0.5% headroom for spread/slip/fees
    allow_fractional: bool = True,
) -> float:
    if risk_per_share <= 0 or entry <= 0:
        print(f"Sizing: Invalid parameters: risk_per_share={risk_per_share}, entry={entry}. Returning qty=0.")
        return 0.0

    # 1) risk-based qty
    risk_dollars = equity * risk_pct
    qty = risk_dollars / risk_per_share

    # 2) notional cap (your existing rule)
    max_notional = equity * max_pos_value_mult
    qty = min(qty, max_notional / entry)

    # 3) hard cap by available buying power (MOST IMPORTANT here)
    if buying_power is not None:
        qty = min(qty, (buying_power * bp_buffer) / entry)
    if side == "short":
        # fractional shorting is not allowed in Alpaca; round down to whole shares
        allow_fractional = False
    # 4) rounding / minimums
    if allow_fractional:
        # don’t force >= 1; allow small sizes
        print(f"Sizing: Computed raw qty: {qty}, allowing fractional shares")
        return max(0.0, float(qty))
    else:
        # whole shares
        print(f"Sizing: Computed raw qty: {qty}, rounding down to whole shares")
        return float(int(qty))  # floor
