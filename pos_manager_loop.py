import time
from typing import Callable
from shared_pos_state import SharedPosState
import threading
from live_exec import LiveExecutor
from time_mgmt import TimeMgr
from dataapi import AlpacaMarketData, AlpacaPaperTrading

def position_manager_loop(
    *,
    live_executor: LiveExecutor, 
    shared_pos: SharedPosState,
    stop_event: threading.Event,
    market_data: AlpacaMarketData,
    paper_trading: AlpacaPaperTrading,
    cfg,
    poll_seconds: float = 5.0,
    timemgr: TimeMgr = None
) -> None:
    """
    Runs until stop_event is set.
    Every poll_seconds:
      - lock shared position
      - if no position, do nothing
      - otherwise fetch latest price info / quote
      - run cut_loss and take_profit while still holding the lock
    """
    time.sleep(5) # initial sleep to stagger with main thread's market data fetch
    while not stop_event.is_set():
        
        try:
            should_clear = False
            print(f"position_manager_loop tick at {time.strftime('%Y-%m-%d %H:%M:%S')}")
            # 1) read symbol under lock
            with shared_pos.locked() as pos:
                symbol = None if pos is None else pos.symbol

            if symbol is not None:
                resp = market_data._get_latest_quote(symbol)
                q = resp["quotes"][symbol]

                bid = float(q["bp"])
                ask = float(q["ap"])

                with shared_pos.locked() as pos:
                    if pos is not None:
                        # Use executable-side price for exit logic
                        px = bid if pos.side == "long" else ask

                        # 1) stop loss first
                        did_stop = live_executor.cut_loss(
                            paper=paper_trading,
                            pos=pos,
                            px=px,
                            cfg=cfg,
                            extended_hours=False,
                        )
                        print(f"cut_loss check: did_stop={did_stop}, pos.remaining_qty={pos.remaining_qty}")

                        # 2) take profit second
                        if not did_stop and pos.remaining_qty > 0:
                            did_tp = live_executor.take_profit(
                                paper=paper_trading,
                                pos=pos,
                                px=px,
                                cfg=cfg,
                            )
                        else:
                            did_tp = False
                        print(f"take_profit check: did_tp={did_tp}, pos.remaining_qty={pos.remaining_qty}")

                        # 3) hard exit last
                        if not did_stop and not did_tp and pos.remaining_qty > 0:
                            reason = live_executor.hard_exit(
                                paper=paper_trading,
                                pos=pos,
                                px=px,
                                extended_hours=False,
                                timemgr=timemgr,
                            )
                        else:
                            reason = None
                        print(f"hard_exit check: reason={reason}, pos.remaining_qty={pos.remaining_qty}")
                        if pos.remaining_qty <= 0:
                            should_clear = True

            if should_clear:
                print("Position fully closed, clearing shared state.")
                shared_pos.set(None)

        except Exception as e:
            print(f"position_manager_loop error: {e}")

        stop_event.wait(poll_seconds)