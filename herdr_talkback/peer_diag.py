"""Peer connectivity diagnostics that work before credentials are correct.

The first steps never authenticate: TCP reachability, a TLS handshake, the
fixed ``/v1/ping`` probe and the peer certificate. Later steps use the
configured CA and token so a failure points at the exact layer instead of a
blank "connection unavailable".
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import socket
import ssl
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

from . import __version__, __build__


def certificate_fingerprint(der: bytes) -> str:
    digest = hashlib.sha256(der).hexdigest()
    return ':'.join(digest[index:index + 2] for index in range(0, len(digest), 2)).upper()


def _decode_der(der: bytes) -> dict:
    """Decode a peer certificate without trusting it."""
    info = {'subject': None, 'names': [], 'expires': None, 'days_left': None}
    if not der:
        return info
    try:
        from cryptography import x509
        from cryptography.x509.oid import NameOID
    except ImportError:  # pragma: no cover - optional dependency
        return info
    certificate = x509.load_der_x509_certificate(der)
    common = certificate.subject.get_attributes_for_oid(NameOID.COMMON_NAME)
    info['subject'] = common[0].value if common else None
    try:
        san = certificate.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
        info['names'] = [value for value in san.get_values_for_type(x509.DNSName)]
        info['names'] += [str(value) for value in san.get_values_for_type(x509.IPAddress)]
    except Exception:  # noqa: BLE001
        pass
    expires = certificate.not_valid_after_utc if hasattr(certificate, 'not_valid_after_utc') else certificate.not_valid_after.replace(tzinfo=timezone.utc)
    info['expires'] = expires.isoformat()
    info['days_left'] = (expires - datetime.now(timezone.utc)).days
    return info


def _names(cert) -> list:
    return list(cert.get('names') or [])


def _host_matches(host: str, cert) -> bool:
    host = (host or '').lower()
    return any(str(value).lower() == host for value in _names(cert))


def _expiry(cert) -> dict:
    return {'expires': cert.get('expires'), 'days_left': cert.get('days_left')}


def inspect_token(path) -> dict:
    """Report token file health without ever exposing the secret."""
    path = Path(path)
    if not path.is_file():
        return {'ok': False, 'reason': 'token file missing', 'path': str(path)}
    text = path.read_text(encoding='ascii', errors='replace').strip()
    valid = len(text) == 64 and all(character in '0123456789abcdef' for character in text)
    mode = path.stat().st_mode & 0o777
    problems = []
    if not valid:
        problems.append('token must be 256-bit lowercase hexadecimal')
    # Windows has no POSIX mode bits; its ACL is not visible through st_mode.
    if os.name != 'nt' and mode & 0o077:
        problems.append('token file is readable by group/others')
    return {'ok': not problems, 'reason': '; '.join(problems), 'path': str(path),
            'length': len(text), 'mode': oct(mode)}


def inspect_cert(path, host: str) -> dict:
    path = Path(path)
    if not path.is_file():
        return {'ok': False, 'reason': 'certificate file missing', 'path': str(path)}
    try:
        der = ssl.PEM_cert_to_DER_cert(path.read_text(encoding='ascii'))
        cert = ssl._ssl._test_decode_cert(str(path))
    except Exception as exc:  # noqa: BLE001 - report decoding failures verbatim
        return {'ok': False, 'reason': f'{type(exc).__name__}: {exc}', 'path': str(path)}
    names = [value for kind, value in cert.get('subjectAltName', ())
             if kind in ('IP', 'IP Address', 'DNS')]
    expiry = {'expires': cert.get('notAfter'), 'days_left': None}
    problems = []
    if not _host_matches(host, {'names': names}):
        problems.append(f'certificate has no SAN for {host}')
    if expiry['days_left'] is not None and expiry['days_left'] < 0:
        problems.append('certificate expired')
    return {'ok': not problems, 'reason': '; '.join(problems), 'path': str(path),
            'subject': cert.get('subject'), 'names': names,
            'fingerprint_sha256': certificate_fingerprint(der), **expiry}


async def _tcp(host: str, port: int, timeout: float) -> dict:
    started = time.monotonic()
    try:
        reader, writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout)
    except Exception as exc:  # noqa: BLE001 - always report the real error
        return {'ok': False, 'detail': f'{host}:{port} unreachable: {type(exc).__name__}: {exc}'}
    writer.close()
    try:
        await writer.wait_closed()
    except Exception:  # noqa: BLE001
        pass
    return {'ok': True, 'detail': f'{host}:{port} connected in {int((time.monotonic() - started) * 1000)} ms'}


async def _tls_probe(host: str, port: int, context: ssl.SSLContext, timeout: float) -> dict:
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port, ssl=context, server_hostname=None if context.check_hostname is False else host),
            timeout)
    except Exception as exc:  # noqa: BLE001
        return {'ok': False, 'detail': f'{type(exc).__name__}: {exc}'}
    try:
        ssl_object = writer.get_extra_info('ssl_object')
        der = ssl_object.getpeercert(binary_form=True)
        cert = _decode_der(der)
        detail = {'tls_version': ssl_object.version(), 'cipher': (ssl_object.cipher() or [None])[0],
                  'names': cert['names'], 'subject': cert['subject'],
                  'fingerprint_sha256': certificate_fingerprint(der) if der else None,
                  **_expiry(cert)}
        return {'ok': True, 'detail': detail}
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:  # noqa: BLE001
            pass


async def _http_probe(peer, host: str, timeout: float, local_node: str) -> dict:
    try:
        import aiohttp
    except ImportError:  # pragma: no cover - optional dependency
        return {'ok': False, 'detail': 'aiohttp is not installed (pip install herdr-talkback[network])'}
    results = {}
    unverified = ssl.create_default_context()
    unverified.check_hostname = False
    unverified.verify_mode = ssl.CERT_NONE
    try:
        verified = ssl.create_default_context(cafile=str(peer.ca_file)) if peer.ca_file else None
    except Exception as exc:  # noqa: BLE001
        verified = None
        results['ca_load'] = {'ok': False, 'detail': f'{type(exc).__name__}: {exc}'}
    if verified is not None:
        verified.check_hostname = True

    async def fetch(context, path, authenticated):
        headers = {}
        if authenticated:
            headers = {'X-Talkback-Node': local_node,
                       'Authorization': 'Bearer ' + peer.token()}
        connector = aiohttp.TCPConnector(ssl=context, force_close=True)
        async with aiohttp.ClientSession(connector=connector, timeout=aiohttp.ClientTimeout(total=timeout),
                                         trust_env=False) as session:
            async with session.get(peer.url + path, headers=headers, allow_redirects=False) as response:
                body = await response.text()
                try:
                    payload = json.loads(body)
                except ValueError:
                    payload = body[:200]
                return response.status, payload

    # Credential-free liveness probe, first with the configured CA then unverified.
    for label, context in (('verified', verified), ('unverified', unverified)):
        if context is None:
            continue
        try:
            status, payload = await fetch(context, '/v1/ping', False)
        except Exception as exc:  # noqa: BLE001
            results['ping_' + label] = {'ok': False, 'detail': f'{type(exc).__name__}: {exc}'}
            continue
        ok = status == 200 and isinstance(payload, dict) and payload.get('ping') is True
        hint = ''
        if not ok and status in (401, 404):
            # Older builds answer the unknown path with 401; that is not a
            # connectivity failure, just a missing capability.
            hint = 'peer build does not expose /v1/ping yet (upgrade to enable it)'
            ok = True
            payload = {'probe': 'unsupported by peer build'}
        results['ping_' + label] = {'ok': ok, 'status': status, 'detail': payload, 'hint': hint}
        if ok:
            results['ping'] = results['ping_' + label]
            break

    # Authenticated call: separates token problems (401) from permission (403).
    context = verified if verified is not None and results.get('ping_verified', {}).get('ok') else unverified
    try:
        status, payload = await fetch(context, '/v1/info', True)
        detail = payload if isinstance(payload, dict) else {'body': payload}
        detail['status'] = status
        detail.pop('instance_id', None)
        results['auth'] = {'ok': status == 200, 'status': status, 'detail': detail,
                           'hint': {401: 'token mismatch: exchange the pair token',
                                    403: 'authenticated but the peer denied remote control'}.get(status, '')}
    except Exception as exc:  # noqa: BLE001
        results['auth'] = {'ok': False, 'detail': f'{type(exc).__name__}: {exc}'}
    return results


async def diagnose(peer, local_node: str = '', *, timeout: float = 5.0) -> dict:
    """Run the ordered diagnostic steps for one configured peer."""
    parts = urlsplit(peer.url)
    host, port = parts.hostname, parts.port or 443
    report = {'peer': peer.node_id, 'url': peer.url, 'local_version': __version__,
              'local_build': __build__, 'steps': []}

    def add(name, ok, detail, **extra):
        report['steps'].append(dict(step=name, ok=ok, detail=detail, **extra))

    tcp = await _tcp(host, port, timeout)
    add('tcp', tcp['ok'], tcp['detail'])
    if not tcp['ok']:
        return report

    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    probe = await _tls_probe(host, port, context, timeout)
    add('tls_handshake', probe['ok'], probe['detail'])
    if not probe['ok']:
        return report

    cert = probe['detail']
    add('certificate_matches_url', _host_matches(host, cert),
        {'host': host, 'names': cert.get('names'), 'fingerprint_sha256': cert.get('fingerprint_sha256'),
         'days_left': cert.get('days_left')})

    http = await _http_probe(peer, host, timeout, local_node)
    for name in ('ca_load', 'ping', 'auth'):
        if name in http:
            item = http[name]
            add(name, item['ok'], item.get('detail'), status=item.get('status'), hint=item.get('hint', ''))
    if 'ping' not in http and 'ping_unverified' in http:
        item = http['ping_unverified']
        add('ping_unverified', item['ok'], item.get('detail'), status=item.get('status'),
            hint=item.get('hint', ''))

    report['local'] = {'cert': inspect_cert(peer.ca_file, host), 'token': inspect_token(peer.token_file)}
    add('local_credentials', report['local']['cert']['ok'] and report['local']['token']['ok'],
        '; '.join(filter(None, [report['local']['cert'].get('reason'), report['local']['token'].get('reason')])) or 'certificate and token look usable')
    return report


def summary(report: dict) -> str:
    lines = [f"peer {report['peer']} -> {report['url']}  (local {report['local_version']} {report['local_build']})"]
    for step in report['steps']:
        mark = 'OK  ' if step['ok'] else 'FAIL'
        detail = step['detail']
        if isinstance(detail, dict):
            detail = ', '.join(f'{key}={value}' for key, value in detail.items() if value is not None)
        lines.append(f"  [{mark}] {step['step']}: {detail}")
        if step.get('hint'):
            lines.append(f"         hint: {step['hint']}")
    cert = report.get('local', {}).get('cert', {})
    token = report.get('local', {}).get('token', {})
    if cert or token:
        lines.append('  exchange (manual, never paste secrets in chat):')
        if cert.get('path'):
            lines.append(f"    send {cert['path']} (sha256 {cert.get('fingerprint_sha256')}) to the peer as its ca_file")
        if token.get('path'):
            lines.append(f"    exchange {token['path']} with the peer and write it as this peer's token_file")
    return '\n'.join(lines)
