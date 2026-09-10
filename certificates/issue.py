"""Issue homelab TLS using a private local CA; outputs only encrypted keys."""
from datetime import datetime, timedelta, timezone
import base64
import json
import os
from pathlib import Path
import subprocess
import tempfile
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID, ExtendedKeyUsageOID

root = Path(__file__).resolve().parents[1]
private = Path.home()/'.config/sops/age'
now = datetime.now(timezone.utc)
ca_key_path = private/'homelab-ca.key'
ca_cert_path = root/'certificates/homelab-ca.crt'
pem = serialization.Encoding.PEM
if ca_key_path.exists():
    ca_key = serialization.load_pem_private_key(ca_key_path.read_bytes(),password=None)
    ca_cert = x509.load_pem_x509_certificate(ca_cert_path.read_bytes())
else:
    if ca_cert_path.exists():
        raise RuntimeError('CA certificate exists without key; recover key rather than replace CA')
    ca_key = rsa.generate_private_key(public_exponent=65537,key_size=3072)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME,'Homelab Local CA')])
    ca_cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
        .public_key(ca_key.public_key()).serial_number(x509.random_serial_number())
        .not_valid_before(now-timedelta(minutes=5)).not_valid_after(now+timedelta(days=3650))
        .add_extension(x509.BasicConstraints(ca=True,path_length=0),critical=True)
        .add_extension(x509.KeyUsage(False,False,False,False,False,True,True,False,False),critical=True)
        .add_extension(x509.NameConstraints([x509.DNSName('.home')],None),critical=True)
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key()),critical=False)
        .sign(ca_key,hashes.SHA256()))
    with ca_key_path.open('xb') as f:
        f.write(ca_key.private_bytes(pem,serialization.PrivateFormat.PKCS8,serialization.NoEncryption()))
    ca_cert_path.write_bytes(ca_cert.public_bytes(pem))
hosts=['api.home','ai.home','dashboard.home','grafana.home','argocd.home']
key=rsa.generate_private_key(public_exponent=65537,key_size=2048)
cert=(x509.CertificateBuilder().subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME,'Homelab services')]))
      .issuer_name(ca_cert.subject).public_key(key.public_key()).serial_number(x509.random_serial_number())
      .not_valid_before(now-timedelta(minutes=5)).not_valid_after(now+timedelta(days=365))
      .add_extension(x509.BasicConstraints(ca=False,path_length=None),critical=True)
      .add_extension(x509.SubjectAlternativeName([x509.DNSName(h) for h in hosts]),critical=False)
      .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]),critical=False)
      .add_extension(x509.KeyUsage(True,False,True,False,False,False,False,False,False),critical=True)
      .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()),critical=False)
      .sign(ca_key,hashes.SHA256()))
items=[]
for ns in ['backend','dashboard','monitoring','argocd']:
    items.append({'apiVersion':'v1','kind':'Secret','metadata':{'name':'homelab-tls','namespace':ns},'type':'kubernetes.io/tls','data':{'tls.crt':base64.b64encode(cert.public_bytes(pem)).decode(),'tls.key':base64.b64encode(key.private_bytes(pem,serialization.PrivateFormat.PKCS8,serialization.NoEncryption())).decode()}})
def encrypt(data,path,regex):
    with tempfile.NamedTemporaryFile(dir=private,suffix='.json',delete=False) as source:
        source.write(json.dumps(data).encode())
    try:
        result=subprocess.run(['sops','--config',os.devnull,'--encrypt','--age','age1tsns9fmhenrdl5ufs2vs28gut2m464xlcp23440uxp4x3aqvdgzsytyjwx','--encrypted-regex',regex,'--input-type','json','--output-type','yaml',source.name],stdout=subprocess.PIPE,stderr=subprocess.PIPE)
    finally:
        Path(source.name).unlink()
    if result.returncode:
        raise RuntimeError(result.stderr.decode())
    path.write_bytes(result.stdout)
encrypt({'apiVersion':'v1','kind':'List','items':items},root/'kubernetes/secrets/homelab-tls.enc.yaml','^(data|stringData)$')
encrypt({'private_key':ca_key_path.read_text(),'certificate':ca_cert.public_bytes(pem).decode()},root/'kubernetes/secrets/reference/homelab-ca.enc.yaml','^private_key$')
print('TLS certificate issued; expires '+cert.not_valid_after_utc.isoformat())
