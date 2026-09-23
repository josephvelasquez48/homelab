"""Issue the phone bridge's TLS certificate (phone.home) from the homelab CA.

Separate from issue.py on purpose: the phone bridge isn't behind Traefik
(it runs on the Pi's host, not in K3s - see docs/phone.md), so it has
its own key and certificate instead of joining the shared five-name
certificate. Re-issuing this one never touches the cluster's TLS Secrets.

Writes phone-bridge.crt/.key into the restricted key directory next to
the CA key; copy them to the Pi and delete the local key afterwards:

    python certificates/issue-phone.py
    scp ~/.config/sops/age/phone-bridge.crt joe@192.168.1.253:.config/phone-bridge/tls.crt
    scp ~/.config/sops/age/phone-bridge.key joe@192.168.1.253:.config/phone-bridge/tls.key
"""
from datetime import datetime, timedelta, timezone
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

root = Path(__file__).resolve().parents[1]
private = Path.home() / '.config/sops/age'
pem = serialization.Encoding.PEM
now = datetime.now(timezone.utc)

ca_key = serialization.load_pem_private_key((private / 'homelab-ca.key').read_bytes(), password=None)
ca_cert = x509.load_pem_x509_certificate((root / 'certificates/homelab-ca.crt').read_bytes())

key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
cert = (x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'phone.home')]))
        .issuer_name(ca_cert.subject).public_key(key.public_key()).serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=5)).not_valid_after(now + timedelta(days=365))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(x509.SubjectAlternativeName([x509.DNSName('phone.home')]), critical=False)
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
        .add_extension(x509.KeyUsage(True, False, True, False, False, False, False, False, False), critical=True)
        .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()), critical=False)
        .sign(ca_key, hashes.SHA256()))

(private / 'phone-bridge.crt').write_bytes(cert.public_bytes(pem))
key_path = private / 'phone-bridge.key'
key_path.write_bytes(key.private_bytes(pem, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
print(f'wrote {private / "phone-bridge.crt"} and {key_path} (expires {cert.not_valid_after_utc:%Y-%m-%d})')
