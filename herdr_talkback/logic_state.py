"""Durable pane observations and at-most-once automatic actions."""
import hashlib
import uuid
import sqlite3
from contextlib import closing


RESULTS = frozenset(('blocked', 'continue', 'waiting', 'done', 'unknown'))


def read_status(path):
    """Read existing observations without initializing or migrating the database."""
    if not path.exists():
        return []
    with closing(sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True)) as db:
        db.row_factory = sqlite3.Row
        if not db.execute("select 1 from sqlite_master where type='table' and name='logic_panes'").fetchone():
            return []
        return [dict(row) for row in db.execute('''select pane_id,herdr_status,logic_status,
            content_hash,stable_since,updated_at,next_check_at,request_id,detail
            from logic_panes order by pane_id''')]


class LogicStore:
    def __init__(self, state):
        self.state = state
        with state._connect() as db:
            db.executescript('''
                create table if not exists logic_panes (
                    pane_id text primary key, herdr_status text not null,
                    logic_status text not null, content text not null,
                    content_hash text not null, stable_since real not null,
                    updated_at real not null, next_check_at real not null default 0,
                    request_id text, detail text not null default ''
                );
                create table if not exists logic_requests (
                    request_id text primary key, pane_id text not null,
                    monitor_pane_id text not null, content_hash text not null,
                    baseline text not null, state text not null,
                    created_at real not null, deadline real not null,
                    result text, finished_at real
                );
                create table if not exists logic_attempts (
                    pane_id text not null, content_hash text not null,
                    created_at real not null, outcome text not null,
                    updated_at real not null,
                    primary key(pane_id, content_hash)
                );
            ''')

    def get(self, pane_id):
        with self.state._connect() as db:
            row = db.execute('select * from logic_panes where pane_id=?', (pane_id,)).fetchone()
            return dict(row) if row else None

    def list(self):
        with self.state._connect() as db:
            return [dict(r) for r in db.execute('select * from logic_panes order by pane_id')]

    def observe(self, pane_id, status, content, now):
        digest = hashlib.sha256(content.encode('utf-8')).hexdigest()
        with self.state._connect() as db:
            db.execute('begin immediate')
            old = db.execute('select * from logic_panes where pane_id=?', (pane_id,)).fetchone()
            reset = old is None or old['content_hash'] != digest or (
                old['herdr_status'] in ('working', 'blocked', 'offline') and status not in ('working', 'blocked', 'offline'))
            if status in ('working', 'blocked'):
                logic, stable = status, now
                reset = True
            elif reset:
                logic = 'init' if old and old['content_hash'] != digest else 'todo'
                stable = now
            else:
                logic = 'todo' if old['logic_status'] == 'init' else old['logic_status']
                stable = old['stable_since']
            if reset:
                self._invalidate(db, pane_id, now)
            db.execute('''insert into logic_panes
                (pane_id,herdr_status,logic_status,content,content_hash,stable_since,updated_at,next_check_at,request_id,detail)
                values (?,?,?,?,?,?,?,?,?,?) on conflict(pane_id) do update set
                herdr_status=excluded.herdr_status, logic_status=excluded.logic_status,
                content=excluded.content, content_hash=excluded.content_hash,
                stable_since=excluded.stable_since, updated_at=excluded.updated_at,
                next_check_at=excluded.next_check_at,request_id=excluded.request_id,detail=excluded.detail''',
                (pane_id, status, logic, content, digest, stable, now,
                 0 if reset else old['next_check_at'], None if reset else old['request_id'],
                 '' if reset else old['detail']))
        if not old or old['logic_status'] != logic or old['content_hash'] != digest:
            self.state.record_event('logic_observation', {'pane_id': pane_id, 'herdr_status': status,
                                                        'logic_status': logic, 'content_hash': digest})
        return self.get(pane_id)

    def ready(self, pane_id, now, stable_seconds):
        row = self.get(pane_id)
        return bool(row and row['herdr_status'] not in ('working', 'blocked', 'offline')
                    and row['logic_status'] in ('todo', 'init', 'waiting', 'unknown', 'timeout')
                    and now - row['stable_since'] >= stable_seconds
                    and now >= row['next_check_at'] and not self.attempted(pane_id, row['content_hash'])
                    and not row['request_id'])

    @staticmethod
    def _invalidate(db, pane_id, now):
        db.execute("update logic_requests set state='invalidated',finished_at=? where pane_id=? and state in ('sending','awaiting')",
                   (now, pane_id))

    def invalidate(self, pane_id, now, detail='observation invalidated', cooldown=0):
        with self.state._connect() as db:
            db.execute('begin immediate')
            self._invalidate(db, pane_id, now)
            db.execute("update logic_panes set request_id=null,logic_status='unknown',next_check_at=?,detail=? where pane_id=?",
                       (now + cooldown, detail, pane_id))

    def start_request(self, pane_id, monitor_pane_id, baseline, now, timeout):
        request_id = uuid.uuid4().hex
        with self.state._connect() as db:
            db.execute('begin immediate')
            row = db.execute('select * from logic_panes where pane_id=?', (pane_id,)).fetchone()
            if row is None or row['request_id']:
                raise ValueError('pane already has a request or is missing')
            db.execute('insert into logic_requests values (?,?,?,?,?,?,?,?,null,null)',
                       (request_id, pane_id, monitor_pane_id, row['content_hash'], baseline,
                        'sending', now, now + timeout))
            db.execute("update logic_panes set request_id=?,logic_status='judging' where pane_id=?", (request_id, pane_id))
        return self.request(request_id)

    def submitted(self, request_id):
        with self.state._connect() as db:
            db.execute("update logic_requests set state='awaiting' where request_id=? and state='sending'", (request_id,))

    def request(self, request_id):
        with self.state._connect() as db:
            row = db.execute('select * from logic_requests where request_id=?', (request_id,)).fetchone()
            return dict(row) if row else None

    def active(self):
        with self.state._connect() as db:
            return [dict(r) for r in db.execute("select * from logic_requests where state in ('sending','awaiting') order by created_at")]

    def finish(self, request_id, result, now, cooldown, state='complete'):
        if result not in RESULTS | {'timeout'}:
            raise ValueError('invalid logical result')
        with self.state._connect() as db:
            db.execute('begin immediate')
            req = db.execute('select * from logic_requests where request_id=?', (request_id,)).fetchone()
            if not req or req['state'] not in ('sending', 'awaiting'):
                return False
            db.execute('update logic_requests set state=?,result=?,finished_at=? where request_id=?',
                       (state, result, now, request_id))
            db.execute('''update logic_panes set request_id=null,logic_status=?,next_check_at=?,detail=?
                where pane_id=? and request_id=? and content_hash=?''',
                       (result, now + cooldown, 'monitor result: ' + result, req['pane_id'], request_id, req['content_hash']))
        self.state.record_event('logic_result', {'request_id': request_id, 'pane_id': req['pane_id'], 'result': result})
        return True

    def recover(self, now, cooldown):
        for req in self.active():
            self.finish(req['request_id'], 'unknown', now, cooldown, state='uncertain')

    def attempted(self, pane_id, digest):
        with self.state._connect() as db:
            return db.execute('select 1 from logic_attempts where pane_id=? and content_hash=?', (pane_id, digest)).fetchone() is not None

    def reserve_attempt(self, pane_id, digest, now):
        with self.state._connect() as db:
            cursor = db.execute("insert or ignore into logic_attempts values (?,?,?,'uncertain',?)", (pane_id, digest, now, now))
            if cursor.rowcount == 1:
                db.execute("update logic_panes set detail='continue: uncertain (reserved before submission)' where pane_id=? and content_hash=?",
                           (pane_id, digest))
            return cursor.rowcount == 1

    def attempt_outcome(self, pane_id, digest, outcome, now):
        with self.state._connect() as db:
            db.execute('update logic_attempts set outcome=?,updated_at=? where pane_id=? and content_hash=?',
                       (outcome, now, pane_id, digest))
            db.execute('update logic_panes set detail=? where pane_id=?', ('continue: ' + outcome, pane_id))
        self.state.record_event('logic_continue', {'pane_id': pane_id, 'content_hash': digest, 'outcome': outcome})

    def attempts(self):
        with self.state._connect() as db:
            return [dict(r) for r in db.execute('select * from logic_attempts order by created_at')]
