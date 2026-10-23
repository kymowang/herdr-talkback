"""Local TLS provisioning; private material is never printed."""
from datetime import datetime, timedelta, timezone
import ipaddress
import os
from pathlib import Path
import secrets


def provision(directory, hosts, force=False):
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    directory = Path(directory)
    if not hosts:
        raise ValueError('at least one certificate hostname or IP is required')
    paths = [directory / name for name in ('node.crt', 'node.key', 'pair.token')]
    if not force and any(path.exists() for path in paths):
        raise FileExistsError('credentials already exist; use --force to replace them')
    sans = []
    for host in hosts:
        try:
            sans.append(x509.IPAddress(ipaddress.ip_address(host)))
        except ValueError:
            sans.append(x509.DNSName(host))
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, hosts[0])])
    now = datetime.now(timezone.utc)
    certificate = (x509.CertificateBuilder().subject_name(subject).issuer_name(subject)
                   .public_key(key.public_key()).serial_number(x509.random_serial_number())
                   .not_valid_before(now - timedelta(minutes=5)).not_valid_after(now + timedelta(days=365))
                   .add_extension(x509.SubjectAlternativeName(sans), critical=False)
                   .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
                   .add_extension(x509.KeyUsage(digital_signature=True, content_commitment=False,
                       key_encipherment=True, data_encipherment=False, key_agreement=False,
                       key_cert_sign=True, crl_sign=True, encipher_only=False, decipher_only=False), critical=True)
                   .sign(key, hashes.SHA256()))
    directory.mkdir(parents=True, exist_ok=True)
    data = [certificate.public_bytes(serialization.Encoding.PEM),
            key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                              serialization.NoEncryption()), (secrets.token_hex(32) + '\n').encode('ascii')]
    for path, content in zip(paths, data):
        flags = os.O_WRONLY | os.O_CREAT | (os.O_TRUNC if force else os.O_EXCL)
        fd = os.open(str(path), flags, 0o600)
        with os.fdopen(fd, 'wb') as handle:
            handle.write(content)
    return paths
