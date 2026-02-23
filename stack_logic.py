from typing import List
from models import FVG

def pop_invalidated(stack: List[FVG], bar_low: float, bar_high: float) -> None:
    while stack:
        top = stack[-1]
        if top.dir == "bull" and bar_low <= top.gap_low:
            stack.pop(); continue
        if top.dir == "bear" and bar_high >= top.gap_high:
            stack.pop(); continue
        break

def should_push(stack: List[FVG], new_dir: str, gap_low: float, gap_high: float) -> bool:
    if not stack:
        return True
    top = stack[-1]
    if new_dir != top.dir:
        return False
    if new_dir == "bull":
        return gap_low > top.gap_low
    return gap_high < top.gap_high