"""A certificate for the headset page, so the browser will let it into VR.

WebXR - the only way a web page can reach the headset's displays - exists only on a secure page.
On a plain http:// address the Quest browser does not even define it, so the page has no VR mode
at all. No certificate authority will vouch for a bare address like 192.168.1.169, so BlueShelf signs
its own. The headset warns about it the first time, which has to be accepted once, and from then
on the page is secure enough for VR.

The certificate lives in BlueShelf's own folder, covers every address this PC answers on, and is made
again whenever those addresses change.
"""
from __future__ import annotations

import datetime as dt
import ipaddress
import socket

from blueshelf import paths

try:  # the app still runs, minus VR, if the certificate library is missing
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID
except ImportError:  # pragma: no cover - only on an incomplete install
    x509 = None

YEARS = 10


def available() -> bool:
    return x509 is not None


def _local_ips() -> list[str]:
    ips = ["127.0.0.1"]
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(("192.0.2.1", 9))  # a reserved address, so nothing is sent
        ips.append(probe.getsockname()[0])
    except OSError:
        pass
    finally:
        probe.close()
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ips.append(info[4][0])
    except OSError:
        pass
    return list(dict.fromkeys(ips))


def _names() -> tuple[list[str], list[str]]:
    """The addresses the certificate must cover: every local IP, plus this PC's name."""
    hosts = ["localhost"]
    try:
        name = socket.gethostname()
        hosts += [name, f"{name}.local"]
    except OSError:
        pass
    return _local_ips(), list(dict.fromkeys(hosts))


def _covers(path, ips: list[str], hosts: list[str]) -> bool:
    try:
        cert = x509.load_pem_x509_certificate(path.read_bytes())
    except Exception:
        return False
    if cert.not_valid_after_utc < dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=30):
        return False
    try:
        san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    except x509.ExtensionNotFound:
        return False
    listed = {str(value) for value in san.get_values_for_type(x509.IPAddress)}
    listed |= set(san.get_values_for_type(x509.DNSName))
    return set(ips) <= listed and set(hosts) <= listed


def ensure() -> tuple[str, str] | None:
    """The certificate and key files, made if they are missing or no longer fit this PC.

    Returns None when a certificate cannot be made, so the caller can fall back to plain http.
    """
    if x509 is None:
        return None
    folder = paths.data_dir()
    cert_path, key_path = folder / "headset-cert.pem", folder / "headset-key.pem"
    ips, hosts = _names()
    if cert_path.is_file() and key_path.is_file() and _covers(cert_path, ips, hosts):
        return str(cert_path), str(key_path)
    try:
        _write(cert_path, key_path, ips, hosts)
    except Exception:  # pragma: no cover - a read-only folder or a broken library
        return None
    return str(cert_path), str(key_path)


def _write(cert_path, key_path, ips: list[str], hosts: list[str]) -> None:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = x509.Name([
        x509.NameAttribute(NameOID.COMMON_NAME, "BlueShelf on this PC"),
        x509.NameAttribute(NameOID.ORGANIZATION_NAME, "BlueShelf"),
    ])
    alt: list[x509.GeneralName] = [x509.DNSName(host) for host in hosts]
    for ip in ips:
        try:
            alt.append(x509.IPAddress(ipaddress.ip_address(ip)))
        except ValueError:
            continue
    now = dt.datetime.now(dt.timezone.utc)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(days=1))
        .not_valid_after(now + dt.timedelta(days=365 * YEARS))
        .add_extension(x509.SubjectAlternativeName(alt), critical=False)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    key_path.write_bytes(key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.TraditionalOpenSSL,
        encryption_algorithm=serialization.NoEncryption(),
    ))
    cert_path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    try:
        key_path.chmod(0o600)
    except OSError:
        pass
