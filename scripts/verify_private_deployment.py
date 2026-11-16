"""Probe staging TLS, ingress protection and frontend bundles without reporting secrets."""

from argparse import ArgumentParser
from json import loads
from pathlib import Path
from ssl import create_default_context

import httpx
import psutil

from f1ri.validation.historical_field_study import write_json

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = ArgumentParser(description=__doc__)
    parser.add_argument("--stage", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    config = loads((args.stage/"staging.private.json").read_bytes())
    origin = "https://localhost:8443"
    issuer = "https://127.0.0.1:9443/realms/f1ri-stage"
    assert config["environment"] == "staging" and config["origin"] == origin and config["oidc_issuer"] == issuer
    context = create_default_context(cafile=str(args.stage/"ca.pem"))
    tests = {}

    def check(client, name, expected, method, url, **options):
        response = client.request(method, url, **options)
        assert response.status_code == expected, name
        tests[name] = response.status_code
        return response

    with httpx.Client(verify=context, trust_env=False, timeout=5) as client:
        secure = check(client, "https_health", 200, "GET", origin+"/healthz")
        assert secure.json() == {"status": "ready"} and secure.http_version in ("HTTP/1.1", "HTTP/2")
        for header, expected in {"strict-transport-security": "max-age=31536000",
                                 "x-robots-tag": "noindex, nofollow", "x-content-type-options": "nosniff",
                                 "x-frame-options": "DENY", "referrer-policy": "no-referrer"}.items():
            assert secure.headers[header] == expected, header
        assert "frame-ancestors 'none'" in secure.headers["content-security-policy"]
        assert "no-store" in secure.headers["cache-control"] and "server" not in secure.headers
        check(client, "issuer_tls", 200, "GET", issuer+"/.well-known/openid-configuration")
        check(client, "anonymous_page", 303, "GET", origin+"/")
        check(client, "raw_archive_blocked", 303, "GET", origin+"/artifacts/reports/stage8_s8_b_evidence.zip")
        check(client, "encoded_file_path_blocked", 303, "GET", origin+"/%2e%2e/artifacts/models/pace_v2_d8r1.pkl")
        check(client, "anonymous_api", 401, "GET", origin+"/api/v1/presets")
        check(client, "unauthenticated_fake_proxy_headers", 401, "GET", origin+"/api/v1/presets",
              headers={"X-F1RI-Edge": "forged", "X-Forwarded-Host": "localhost", "X-Forwarded-User": "researcher"})
        check(client, "origin_boundary", 403, "POST", origin+"/api/v1/analyses",
              headers={"Origin": "https://unauthorized.example.invalid"}, json={})
        check(client, "direct_api", 403, "GET", "http://127.0.0.1:8100/api/v1/presets")
        check(client, "direct_ui", 403, "GET", "http://127.0.0.1:3100/")
        check(client, "invalid_oidc_callback", 400, "GET", origin+"/auth/callback?state=invalid&code=invalid")

    ports = {3100: "next", 8100: "api", 8443: "gateway", 9443: "issuer"}
    input_secrets = [config[key].encode() for key in ("oidc_client_secret", "flow_secret", "edge_secret")]
    bundles = list((ROOT/"apps/web").glob(".next-private*/static/**/*.js"))
    assert bundles
    for bundle in bundles:
        data = bundle.read_bytes()
        assert not any(key in data for key in input_secrets)
        assert b"BEGIN PRIVATE KEY" not in data
    network = {}
    for conn in psutil.net_connections(kind="tcp"):
        if conn.status == psutil.CONN_LISTEN and conn.laddr.port in ports:
            assert conn.laddr.ip == "127.0.0.1"
            network[ports[conn.laddr.port]] = {"address": conn.laddr.ip, "port": conn.laddr.port,
                                              "rss_mib": round(psutil.Process(conn.pid).memory_info().rss/1024**2, 1)}
    assert set(network) == set(ports.values())
    write_json(args.output, {"checks": tests, "strict_certificate_validation": True,
                             "expected_security_headers": True, "loopback_services": network,
                             "compiled_bundles_scanned": len(bundles), "staging_secrets_in_bundles": False,
                             "diagnostics_and_caddy_admin_public": False})


if __name__ == "__main__":
    main()
