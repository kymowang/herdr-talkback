"""Session-scoped operator preferences and named pane directory."""
import asyncio
import sqlite3
from contextlib import closing

from .addressing import EndpointAddress, parse_endpoint_address


def read_monitor_enabled(path, default=False):
    if not path.exists():
        return default
    with closing(sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True)) as db:
        if not db.execute("select 1 from sqlite_master where type='table' and name='operator_settings'").fetchone():
            return default
        row = db.execute("select value from operator_settings where name='logic_monitor_enabled'").fetchone()
        return default if row is None else row[0] == 'true'


def read_registered_pane(path, name):
    if not path.exists():
        return None
    with closing(sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True)) as db:
        if not db.execute("select 1 from sqlite_master where type='table' and name='registered_panes'").fetchone():
            return None
        row = db.execute('select address from registered_panes where name=?', (name,)).fetchone()
        return None if row is None else row[0]


class OperatorStore:
    def __init__(self, state):
        self.state = state
        with state._connect() as db:
            db.executescript('''
                create table if not exists operator_settings (
                    name text primary key, value text not null
                );
                create table if not exists registered_panes (
                    name text primary key, address text not null
                );
            ''')

    def monitor_enabled(self, default=False):
        with self.state._connect() as db:
            row = db.execute("select value from operator_settings where name='logic_monitor_enabled'").fetchone()
        return default if row is None else row['value'] == 'true'

    def set_monitor_enabled(self, enabled):
        with self.state._connect() as db:
            db.execute("insert into operator_settings values ('logic_monitor_enabled',?) "
                       "on conflict(name) do update set value=excluded.value",
                       ('true' if enabled else 'false',))

    def register_pane(self, name, address, config):
        if not isinstance(name, str) or '/' in name:
            raise ValueError('pane registration name must be a single address segment')
        parse_endpoint_address(name)
        endpoint = parse_endpoint_address(address)
        if not isinstance(endpoint, EndpointAddress):
            raise ValueError('address must be node/pane')
        if endpoint.node_id != config.core_node_id and endpoint.node_id not in config.peers:
            raise ValueError('remote node must be a configured peer')
        try:
            with self.state._connect() as db:
                db.execute('insert into registered_panes values (?,?)', (name, endpoint.qualified))
        except sqlite3.IntegrityError as exc:
            raise ValueError('pane name is already registered') from exc

    def pane(self, name):
        with self.state._connect() as db:
            row = db.execute('select address from registered_panes where name=?', (name,)).fetchone()
        return None if row is None else row['address']

    def panes(self):
        with self.state._connect() as db:
            return [dict(row) for row in db.execute('select name,address from registered_panes order by name')]

    def remove_pane(self, name):
        with self.state._connect() as db:
            return db.execute('delete from registered_panes where name=?', (name,)).rowcount > 0


class MonitorController:
    def __init__(self, store, factory):
        self.store, self.factory = store, factory
        self.task = None
        self._lock = asyncio.Lock()
        self.monitor = None

    async def set_enabled(self, enabled, *, persist=True):
        async with self._lock:
            if enabled:
                if self.task is None or self.task.done():
                    monitor = self.factory()  # validate before saving
                    if persist:
                        self.store.set_monitor_enabled(True)
                    self.monitor = monitor
                    self.task = asyncio.create_task(monitor.run_forever())
                else:
                    if persist:
                        self.store.set_monitor_enabled(True)
            else:
                if persist:
                    self.store.set_monitor_enabled(False)
                await self._stop()

    async def _stop(self):
        if self.task is not None:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
            self.task = None
            if self.monitor is not None and hasattr(self.monitor, 'store'):
                self.monitor.store.recover(self.monitor.clock(), self.monitor.config.cooldown_seconds)
                self.monitor.actions.protected.clear()
            self.monitor = None

    async def close(self):
        async with self._lock:
            await self._stop()
