from dataclasses import dataclass
from typing import Literal
import pandas as pd
import typing as t
from datetime import datetime

Side = Literal["long", "short"]

@dataclass
class FVG:
    dir: Literal["bull", "bear"]
    gap_low: float
    gap_high: float
    created_ts: pd.Timestamp  # ET

@dataclass
class PendingSetup:
    fvg: FVG
    stage: str  # "WAIT_RETEST" | "WAIT_ENGULF"
    retest_high: float = 0.0
    retest_low: float = 0.0

@dataclass
class PendingEntry:
    symbol: str
    side: Side                  # "long" / "short"
    stop: float                 # fixed from signal candle / FVG
    tp_r: float                 # e.g. 2
    armed_at_ts: datetime       # when we decided to enter (close of b2)
    valid_for_minute_ts: datetime  # the minute we intend to enter (b2.ts + 1min)

@dataclass
class PositionState:
    symbol: str
    side: Side
    entry: float
    stop: float
    tp: float
    risk_per_share: float

    init_qty: float
    remaining_qty: float

    max_r_seen: float = 0.0        # MFE in R
    max_neg_r_seen: float = 0.0    # MAE in R

@dataclass
class ExecCfg:
    # position sizing
    risk_pct: float = 0.01               # fraction of equity risked per trade (at stop)
    max_pos_value_mult: float = 1.0      # cap notional = equity * mult (for long)

    # profit ladder (normalized log)
    alpha: float = 2.0
    r_max: float = 2.0                  # fraction_closed reaches 100% by this R

    # loss ladder (normalized log)
    beta: float = 1.5
    r_stop: float = 1.0                 # fraction_cut reaches 100% by this adverse R

    # if you want to disable loss ladder dynamically:
    enable_loss_ladder: bool = True

@dataclass
class Trade:
    entry_ts: pd.Timestamp
    exit_ts: pd.Timestamp
    direction: Literal["long", "short"]
    entry: float
    stop: float
    tp: float
    shares: int
    exit_price: float
    exit_reason: str
    pnl: float
    equity_after: float

@dataclass
class Candle:
    symbol: str
    ts: datetime
    open: float
    high: float
    low: float
    close: float
    volume: int
    vwap: t.Optional[float] = None
    trade_count: t.Optional[int] = None