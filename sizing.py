from models import Side, ExecCfg
import mylogger


def compute_live_qty(
    *,
    paper_trading,
    cfg: ExecCfg,
    entry: float,
    stop: float,
    side: Side,
    _logger: mylogger.Logger
) -> float:
    acct = paper_trading.get_account()

    cash = float(acct["cash"])
    equity = float(acct["equity"])
    bp = float(acct["buying_power"])

    # Avoid using margin/leverage. This should stay around your real account size.
    effective_capital = min(cash, equity, bp) * 0.95

    rps = (entry - stop) if side == "long" else (stop - entry)
    if rps <= 0:
        _logger.log(f"Sizing: Invalid entry/stop: entry={entry}, stop={stop}, side={side}. Not entering.")
        return 0.0

    return compute_qty(
        capital=effective_capital,
        risk_pct=cfg.risk_pct,
        risk_per_share=rps,
        entry=entry,
        side=side,
        max_pos_value_mult=cfg.max_pos_value_mult,
        bp_buffer=0.90 if side == "long" else 0.85,
        allow_fractional=False,
        _logger=_logger
    )


def compute_qty(
    *,
    capital: float,
    risk_pct: float,
    risk_per_share: float,
    entry: float,
    side: Side,
    max_pos_value_mult: float,
    bp_buffer: float = 0.90,
    allow_fractional: bool = False,
    _logger: mylogger.Logger
) -> float:
    if capital <= 0:
        _logger.log(f"Sizing: Invalid capital={capital}. Returning qty=0.")
        return 0.0

    if risk_per_share <= 0 or entry <= 0:
        _logger.log(
            f"Sizing: Invalid parameters: risk_per_share={risk_per_share}, entry={entry}. Returning qty=0."
        )
        return 0.0

    # 1) Risk-based quantity
    risk_dollars = capital * risk_pct
    qty_by_risk = risk_dollars / risk_per_share

    # 2) Max position value cap
    max_notional = capital * max_pos_value_mult
    qty_by_notional = max_notional / entry

    # 3) Extra buffer cap
    qty_by_buffer = (capital * bp_buffer) / entry

    qty = min(qty_by_risk, qty_by_notional, qty_by_buffer)

    if not allow_fractional:
        qty = float(int(qty))

    _logger.log(
        f"Sizing: cash/effective capital={capital}, side={side}, entry={entry}, "
        f"risk_per_share={risk_per_share}, risk_dollars={risk_dollars}, "
        f"qty_by_risk={qty_by_risk}, qty_by_notional={qty_by_notional}, "
        f"qty_by_buffer={qty_by_buffer}, final_qty={qty}"
    )

    return qty