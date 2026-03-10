from contextlib import contextmanager
import threading
from models import PositionState
from typing import Optional, Iterator

class SharedPosState:
    def __init__(self, initial: Optional[PositionState] = None) -> None:
        self._pos: Optional[PositionState] = initial
        self._lock = threading.RLock()

    @contextmanager
    def locked(self) -> Iterator[Optional[PositionState]]:
        self._lock.acquire()
        try:
            yield self._pos
        finally:
            self._lock.release()

    def get_copy(self) -> Optional[PositionState]:
        with self._lock:
            return None if self._pos is None else PositionState(**self._pos.__dict__)

    def set(self, pos: Optional[PositionState]) -> None:
        with self._lock:
            self._pos = pos

    def clear(self) -> None:
        with self._lock:
            self._pos = None

    def is_open(self) -> bool:
        with self._lock:
            return self._pos is not None