"""Coordinate automatic recovery with logical continuation per pane."""
import asyncio
import time


class PaneActions:
    def __init__(self, logic_store=None, clock=time.time):
        self.store = logic_store
        self.clock = clock
        self._locks = {}
        self.protected = set()

    def lock(self, pane_id):
        return self._locks.setdefault(pane_id, asyncio.Lock())

    def invalidate(self, pane_id):
        if self.store is not None:
            self.store.invalidate(pane_id, self.clock(), 'automatic recovery superseded judgment', 120)
