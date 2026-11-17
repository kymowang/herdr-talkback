"""Transactional peer admission, durable receipts, and outbound progress."""
import hashlib
import json
import re
import time
import uuid

from .addressing import EndpointAddress, parse_endpoint_address


TERMINAL = frozenset(('delivered', 'failed', 'delivery_unknown'))
PROTOCOL_VERSION = 1
BODY_LIMIT = 64 * 1024
REQUEST_LIMIT = 128 * 1024
ID_RE = re.compile(r'^[A-Za-z0-9_-]{1,128}$')


class PeerConflict(ValueError):
    pass


class PeerCapacity(ValueError):
    pass


class PeerInstanceChanged(ValueError):
    pass


def message_envelope(message):
    return dict(protocol_version=1, message_id=message.message_id,
                source_endpoint=message.source_endpoint, target_endpoint=message.target_endpoint,
                body=message.body, conversation_id=message.conversation_id,
                reply_to=message.reply_to, metadata=dict(message.metadata))


def validate_envelope(peer_id, local_id, envelope):
    if not isinstance(envelope, dict) or type(envelope.get('protocol_version')) is not int or envelope['protocol_version'] != 1:
        raise ValueError('unsupported protocol version')
    expected = {'protocol_version', 'message_id', 'source_endpoint', 'target_endpoint',
                'body', 'conversation_id', 'reply_to', 'metadata'}
    if set(envelope) != expected:
        raise ValueError('invalid envelope fields')
    for key in ('message_id', 'conversation_id'):
        if not isinstance(envelope.get(key), str) or not ID_RE.fullmatch(envelope[key]):
            raise ValueError('invalid message identity')
    reply = envelope.get('reply_to')
    if reply is not None and (not isinstance(reply, str) or not ID_RE.fullmatch(reply)):
        raise ValueError('invalid reply identity')
    source = parse_endpoint_address(envelope.get('source_endpoint'))
    target = parse_endpoint_address(envelope.get('target_endpoint'))
    if (not isinstance(source, EndpointAddress) or source.node_id != peer_id
            or not isinstance(target, EndpointAddress) or target.node_id != local_id):
        raise ValueError('authenticated source or local target mismatch; relaying is prohibited')
    if max(len(source.qualified), len(target.qualified)) > 256:
        raise ValueError('address too long')
    body = envelope.get('body')
    if not isinstance(body, str) or not body.strip() or len(body.encode('utf-8')) > BODY_LIMIT:
        raise ValueError('invalid or oversized message body')
    metadata = envelope.get('metadata')
    if not isinstance(metadata, dict) or len(json.dumps(metadata).encode('utf-8')) > 16 * 1024:
        raise ValueError('invalid or oversized metadata')
    return source, target


class PeerStore:
    def __init__(self, state):
        self.state = state
        with state._connect() as db:
            db.executescript('''
                create table if not exists peer_meta (key text primary key, value text not null);
                create table if not exists peer_instances (peer text primary key, instance_id text not null);
                create table if not exists peer_receipts (
                    peer text not null, message_id text not null unique, envelope_hash text not null,
                    state text not null, reason text not null, updated_at real not null,
                    primary key(peer, message_id));
                create table if not exists peer_progress (
                    message_id text primary key, peer text not null, remote_state text,
                    remote_received_at real, attempts integer not null default 0,
                    next_attempt_at real not null default 0, last_error text not null default '');
                create table if not exists peer_control_requests (
                    peer text not null, request_id text not null, pane_id text not null,
                    screen_sha256 text not null, state text not null,
                    primary key(peer, request_id));
                create table if not exists peer_update_jobs (
                    peer text not null, request_id text not null, state text not null,
                    detail text not null default '', updated_at real not null, service_pane_id text not null default '',
                    primary key(peer, request_id));
                create trigger if not exists peer_message_receipt_update after update of state, reason on messages
                begin
                    update peer_receipts set state=new.state, reason=new.reason, updated_at=new.updated_at
                    where message_id=new.message_id;
                end;
            ''')
            columns = {row['name'] for row in db.execute('pragma table_info(peer_update_jobs)')}
            if 'service_pane_id' not in columns:
                db.execute("alter table peer_update_jobs add column service_pane_id text not null default ''")
            db.execute("insert or ignore into peer_meta values ('instance_id', ?)", (uuid.uuid4().hex,))
            self.instance_id = db.execute("select value from peer_meta where key='instance_id'").fetchone()[0]

    def control_state(self, peer, request_id):
        with self.state._connect() as db:
            row = db.execute('select pane_id,screen_sha256,state from peer_control_requests where peer=? and request_id=?',
                             (peer, request_id)).fetchone()
        return dict(row) if row else None

    def start_control(self, peer, request_id, pane_id, fingerprint):
        with self.state._connect() as db:
            db.execute('insert into peer_control_requests values (?,?,?,?,?)',
                       (peer, request_id, pane_id, fingerprint, 'unknown'))

    def finish_control(self, peer, request_id):
        with self.state._connect() as db:
            db.execute("update peer_control_requests set state='submitted' where peer=? and request_id=?",
                       (peer, request_id))

    def update_job(self, peer, request_id):
        with self.state._connect() as db:
            row = db.execute('select request_id,state,detail,updated_at from peer_update_jobs where peer=? and request_id=?',
                             (peer, request_id)).fetchone()
        return dict(row) if row else None

    def start_update(self, peer, request_id, service_pane_id=''):
        with self.state._connect() as db:
            db.execute('begin immediate')
            current = db.execute("select 1 from peer_update_jobs where state in ('queued','preparing','activating','restarting') limit 1").fetchone()
            if current:
                existing = db.execute('select 1 from peer_update_jobs where peer=? and request_id=?', (peer, request_id)).fetchone()
                if not existing:
                    raise PeerConflict('another installation update is already running')
            return db.execute("insert or ignore into peer_update_jobs (peer,request_id,state,detail,updated_at,service_pane_id) values (?,?,'queued','',?,?)",
                              (peer, request_id, time.time(), service_pane_id)).rowcount == 1

    def claim_update(self):
        with self.state._connect() as db:
            db.execute('begin immediate')
            row = db.execute("select peer,request_id,service_pane_id from peer_update_jobs where state='queued' order by updated_at limit 1").fetchone()
            if row is None:
                return None
            db.execute("update peer_update_jobs set state='preparing',updated_at=? where peer=? and request_id=?",
                       (time.time(), row['peer'], row['request_id']))
            return dict(row)

    def set_update(self, peer, request_id, state, detail=''):
        with self.state._connect() as db:
            db.execute('update peer_update_jobs set state=?,detail=?,updated_at=? where peer=? and request_id=?',
                       (state, detail[:512], time.time(), peer, request_id))

    def fail_queued_update(self, peer, request_id, detail):
        with self.state._connect() as db:
            db.execute("update peer_update_jobs set state='failed',detail=?,updated_at=? where peer=? and request_id=? and state='queued'",
                       (detail[:512], time.time(), peer, request_id))

    def receive(self, peer, envelope, capacity=10000, *, registration_config=None, local_names=()):
        source, target = validate_envelope(peer, self.state.local_node_id, envelope)
        digest = hashlib.sha256(json.dumps(envelope, sort_keys=True, ensure_ascii=True,
                                          separators=(',', ':')).encode('utf-8')).hexdigest()
        message_id = envelope['message_id']
        with self.state._connect() as db:
            db.execute('begin immediate')
            old = db.execute('select * from peer_receipts where message_id=?', (message_id,)).fetchone()
            if old:
                if old['peer'] != peer or old['envelope_hash'] != digest:
                    raise PeerConflict('message identity conflicts with stored content')
                return False, self._receipt(dict(old))
            if (db.execute('select 1 from messages where message_id=?', (message_id,)).fetchone()
                    or db.execute('select 1 from message_archive where message_id=?', (message_id,)).fetchone()):
                raise PeerConflict('message identity already belongs to another delivery')
            if envelope['metadata'].get('talkback_whoami') == 1:
                if registration_config is None or peer not in registration_config.peers:
                    raise ValueError('whoami registration is unavailable')
                name = source.endpoint_id
                if name in local_names or name in registration_config.aliases:
                    raise ValueError('whoami name conflicts with a local pane or alias')
                existing = db.execute('select address from registered_panes where name=?', (name,)).fetchone()
                if existing and existing[0] != source.qualified:
                    raise ValueError('whoami name already registered to another pane')
                db.execute('insert or ignore into registered_panes values (?,?)', (name, source.qualified))
            count = db.execute("select count(*) from peer_receipts where peer=? and state not in ('delivered','failed','delivery_unknown')", (peer,)).fetchone()[0]
            if count >= capacity:
                raise PeerCapacity('inbound peer queue is full')
            now = time.time()
            db.execute('''insert into messages(
                message_id, source, target, target_pane_id, raw_text, state, reason, attempts,
                created_at, updated_at, source_endpoint, target_endpoint, body, metadata_json,
                conversation_id, reply_to) values(?,?,?,'',?,'queued','',0,?,?,?,?,?,?,?,?)''',
                (message_id, source.endpoint_id, target.endpoint_id,
                 f'[{source.qualified}->{target.qualified}] {envelope["body"]}', now, now,
                 source.qualified, target.qualified, envelope['body'], json.dumps(envelope['metadata']),
                 envelope['conversation_id'], envelope['reply_to']))
            db.execute('insert into peer_receipts values (?,?,?,\'queued\',\'\',?)',
                       (peer, message_id, digest, now))
            row = db.execute('select * from peer_receipts where message_id=?', (message_id,)).fetchone()
            return True, self._receipt(dict(row))

    def _receipt(self, row):
        return dict(node_id=self.state.local_node_id, instance_id=self.instance_id,
                    protocol_version=1, message_id=row['message_id'], state=row['state'],
                    reason=row['reason'], updated_at=row['updated_at'])

    def receipt(self, peer, message_id):
        with self.state._connect() as db:
            row = db.execute('select * from peer_receipts where peer=? and message_id=?',
                             (peer, message_id)).fetchone()
        if row is None:
            raise KeyError(message_id)
        return self._receipt(dict(row))

    def pin_instance(self, peer, instance_id):
        if not isinstance(instance_id, str) or not ID_RE.fullmatch(instance_id):
            raise ValueError('invalid receiver instance identity')
        with self.state._connect() as db:
            db.execute('begin immediate')
            old = db.execute('select instance_id from peer_instances where peer=?', (peer,)).fetchone()
            if old and old[0] != instance_id:
                raise PeerInstanceChanged('receiver storage instance changed; automatic replay stopped')
            db.execute('insert or ignore into peer_instances values (?,?)', (peer, instance_id))

    def progress(self, message_id, peer=None, **updates):
        columns = {'remote_state', 'remote_received_at', 'attempts', 'next_attempt_at', 'last_error'}
        if not set(updates) <= columns:
            raise ValueError('invalid progress fields')
        with self.state._connect() as db:
            if peer is not None:
                db.execute('insert or ignore into peer_progress(message_id,peer) values (?,?)', (message_id, peer))
            if updates:
                db.execute('update peer_progress set ' + ','.join(key + '=?' for key in updates)
                           + ' where message_id=?', (*updates.values(), message_id))
            row = db.execute('select * from peer_progress where message_id=?', (message_id,)).fetchone()
        return dict(row) if row else None
