"""Shared admission gate for short service maintenance windows."""
import asyncio


class AdmissionGate:
    def __init__(self, paused=False):
        self.paused = paused
        self.lock = asyncio.Lock()
