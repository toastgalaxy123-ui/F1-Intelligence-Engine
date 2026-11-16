"""Create a disposable loopback HTTPS and Keycloak invitation fixture."""

from argparse import ArgumentParser
from base64 import b64encode
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from ipaddress import ip_address
from json import dumps
from pathlib import Path
from secrets import token_urlsafe
from uuid import uuid4

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from f1ri.private.config import PrivateConfig
from f1ri.private.storage import Store


def json_file(path, value):
    with path.open("x", encoding="utf-8") as handle:
        handle.write(dumps(value, indent=2))
    path.chmod(0o600)


def tls_fixture(out):
    now = datetime.now(UTC)
    ca_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    leaf_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "F1RI staging CA")])
    leaf_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])

    def certificate(subject, issuer, public):
        return (x509.CertificateBuilder().subject_name(subject).issuer_name(issuer).public_key(public)
                .serial_number(x509.random_serial_number()).not_valid_before(now-timedelta(minutes=10))
                .not_valid_after(now+timedelta(days=7)))

    ca = certificate(ca_name, ca_name, ca_key.public_key()).add_extension(
        x509.BasicConstraints(ca=True, path_length=0), critical=True).add_extension(
        x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key()), critical=False).add_extension(
        x509.KeyUsage(digital_signature=False, content_commitment=False, key_encipherment=False,
                      data_encipherment=False, key_agreement=False, key_cert_sign=True, crl_sign=True,
                      encipher_only=False, decipher_only=False), critical=True).sign(ca_key, hashes.SHA256())
    leaf = (certificate(leaf_name, ca_name, leaf_key.public_key()).add_extension(
        x509.BasicConstraints(ca=False, path_length=None), critical=True).add_extension(
        x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()), critical=False).add_extension(
        x509.SubjectKeyIdentifier.from_public_key(leaf_key.public_key()), critical=False).add_extension(
        x509.KeyUsage(digital_signature=True, content_commitment=False, key_encipherment=True,
                      data_encipherment=False, key_agreement=False, key_cert_sign=False, crl_sign=False,
                      encipher_only=False, decipher_only=False), critical=True).add_extension(
        x509.SubjectAlternativeName([x509.DNSName("localhost"), x509.IPAddress(ip_address("127.0.0.1"))]),
        critical=False).add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]),
                                      critical=False).sign(ca_key, hashes.SHA256()))
    (out/"ca.pem").write_bytes(ca.public_bytes(serialization.Encoding.PEM))
    (out/"edge.pem").write_bytes(leaf.public_bytes(serialization.Encoding.PEM)+ca.public_bytes(serialization.Encoding.PEM))
    (out/"edge.key").write_bytes(leaf_key.private_bytes(serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    (out/"edge.key").chmod(0o600)
    spki = leaf.public_key().public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
    (out/"edge-spki.txt").write_text(b64encode(sha256(spki).digest()).decode("ascii"), encoding="ascii")


def main():
    parser = ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--app-port", type=int, default=8443)
    parser.add_argument("--oidc-port", type=int, default=9443)
    args = parser.parse_args()
    out = args.directory.resolve()
    out.mkdir(mode=0o700, exist_ok=False, parents=True)
    tls_fixture(out)
    origin = f"https://localhost:{args.app_port}"
    issuer = f"https://127.0.0.1:{args.oidc_port}/realms/f1ri-stage"
    secret = token_urlsafe(40)
    cfg = {
        "environment": "staging", "origin": origin, "oidc_issuer": issuer, "oidc_client_id": "f1ri-private",
        "oidc_client_secret": secret, "flow_secret": token_urlsafe(40), "edge_secret": token_urlsafe(40),
        "database": str(out/"research.sqlite3"), "issuer_ca": str(out/"ca.pem"), "session_seconds": 120,
        "rate_submissions": 20,
    }
    PrivateConfig.model_validate(cfg)
    json_file(out/"staging.private.json", cfg)
    members = [{"label": label, "username": username, "password": token_urlsafe(22), "subject": str(uuid4())}
               for label, username in (("Researcher A", "researcher-a"), ("Researcher B", "researcher-b"),
                                       ("Uninvited", "uninvited"))]
    realm = {
        "realm": "f1ri-stage", "enabled": True, "registrationAllowed": False, "resetPasswordAllowed": False,
        "sslRequired": "all", "bruteForceProtected": True,
        "clients": [{"clientId": "f1ri-private", "enabled": True, "clientAuthenticatorType": "client-secret",
                     "secret": secret, "protocol": "openid-connect", "publicClient": False,
                     "standardFlowEnabled": True, "directAccessGrantsEnabled": False, "implicitFlowEnabled": False,
                     "redirectUris": [origin+"/auth/callback"], "webOrigins": [],
                     "attributes": {"pkce.code.challenge.method": "S256"}}],
        "users": [{"id": row["subject"], "username": row["username"], "enabled": True, "emailVerified": True,
                   "firstName": row["label"], "lastName": "Staging", "email": row["username"]+"@example.invalid",
                   "credentials": [{"type": "password", "value": row["password"], "temporary": False}]}
                  for row in members],
    }
    json_file(out/"realm.private.json", realm)
    json_file(out/"accounts.private.json", members)
    store = Store(out/"research.sqlite3")
    store.migrate()
    for user in members[:2]:
        store.enroll(issuer, user["subject"], user["label"])
    print("Prepared HTTPS staging fixture. Credentials were written only to the staging directory.")


if __name__ == "__main__":
    main()
