import time
from typing import Callable
from shared_pos_state import SharedPosState
import threading
from live_exec import LiveExecutor
from time_mgmt import TimeMgr
from dataapi import AlpacaMarketData, AlpacaPaperTrading
from mylogger import Logger

def position_manager_loop(
    *,
    live_executor: LiveExecutor, 
    shared_pos: SharedPosState,
    stop_event: threading.Event,
    market_data: AlpacaMarketData,
    paper_trading: AlpacaPaperTrading,
    cfg,
    poll_seconds: float = 5.0,
    timemgr: TimeMgr,
    logger: Logger
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
            logger.log(f"THREAD: position_manager_loop tick at {time.strftime('%Y-%m-%d %H:%M:%S')}")
            # 1) read symbol under lock
            with shared_pos.locked() as pos:
                symbol = None if pos is None else pos.symbol

            if symbol is not None:
                resp = market_data._get_latest_quote(symbol)
                q = resp["quotes"][symbol]

                bid = float(q["bp"])
                ask = float(q["ap"])


                pos = shared_pos.get_copy() # get a copy of the current position state for decision making outside the lock
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
                    position_closed_by_stop = pos.remaining_qty <= 0
                    logger.log(f"THREAD: cut_loss check: did_stop={did_stop}, pos.remaining_qty={pos.remaining_qty}")

                    # 2) take profit second
                    if not position_closed_by_stop:
                        did_tp = live_executor.take_profit(
                            paper=paper_trading,
                            pos=pos,
                            px=px,
                            cfg=cfg,
                        )
                        position_closed_by_tp = pos.remaining_qty <= 0
                        logger.log(f"THREAD: take_profit check: did_tp={did_tp}, pos.remaining_qty={pos.remaining_qty}")
                    else:
                        position_closed_by_tp = False

                    

                    # 3) hard exit last
                    if not position_closed_by_stop and not position_closed_by_tp:
                        reason = live_executor.hard_exit(
                            paper=paper_trading,
                            pos=pos,
                            px=px,
                            extended_hours=False,
                            timemgr=timemgr,
                        )
                        logger.log(f"THREAD: hard_exit check: reason={reason}, pos.remaining_qty={pos.remaining_qty}")
                    else:
                        reason = None

                    if pos.remaining_qty <= 0:
                        logger.log("THREAD: Position fully closed, clearing shared state.")
                        shared_pos.clear()
                    else:
                        shared_pos.set(pos)

        except Exception as e:
            logger.log(f"THREAD: position_manager_loop error: {e}")

        stop_event.wait(poll_seconds)
    
    pos = shared_pos.get_copy()

    if pos is not None:
        try:
            live_executor.hard_exit(
                paper=paper_trading,
                pos=pos,
                px=None,
                extended_hours=False,
                timemgr=timemgr,
            )
            if pos.remaining_qty <= 0:
                shared_pos.clear()
                logger.log("THREAD: Shutdown flatten complete, shared position cleared")
            else: 
                shared_pos.set(pos)
                logger.log(f"THREAD: Shutdown hard_exit sent but qty still remains: {pos.remaining_qty}")


        except Exception as e:
            logger.log(f"THREAD: Shutdown hard_exit failed: {e}")