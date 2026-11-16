# S8-C.1 private-beta activation checklist

S8-C.1 completed the local predeployment checks in the [validation report](../../artifacts/reports/stage8_s8_c1_validation.md). Use this checklist to approve, install and verify the first remote release. The [private hosting guide](private-beta.md) covers the existing app, worker, database and staging tools.

## Record approval before activation

Record a responsible operator, the approving party and a reference to the approval in a restricted deployment record. Keep addresses, account identifiers and personal data out of shareable evidence.

| Approval needed | What to provide |
| --- | --- |
| Host and access | Approved Linux VM or physical host, authorized deployment account, access method, network boundary and permitted ingress. Give the operator access through the host's normal account system. |
| Resource and spending limit | Approved CPU, RAM, persistent storage, backup storage/retention and spending ceiling. Start the assessment with two CPU cores, 2 GiB app RAM and two invited users. Size the IdP separately. |
| Application origin and trust | Exact DNS origin or approved private-network address, permission to configure ingress, certificate issuer/trust model and responsibility for renewal. Supply the key via the host's secure provisioning path. |
| Identity provider | Approved issuer with a path, confidential OIDC client, Authorization Code and PKCE S256, exact `<APP_ORIGIN>/auth/callback`, approved MFA policy and secret delivery. The deployment config belongs on the host or in an approved secret manager. |
| Audience and data | Approve two independent invited identities and a controlled uninvited account for denial testing. Confirm rights for the intended audience and the exact derived charts and exports the app will provide. Describe the app as independent research. [S8-C licensing review](private-beta-licensing.md) |
| Persistence and operations | Approved encrypted local storage, separate encrypted backup location, backup schedule and retention, monitoring destination and incident contacts. |

Use restricted configuration or provider-side secret management for keys, client secrets and credentials. Operator records and screenshots should omit account names, private hosts, session cookies, authorization redirects and workspace contents.

## Verify the target Linux host

Record actual OS and kernel, Python and its SQLite library, Node, Caddy, clock synchronization, CPU and cgroup limits, RAM, filesystem type, space, service account permissions, firewall policy and HTTPS egress to the IdP. Keep the database and its WAL on a local filesystem. Verify that the SQLite library loaded by Python contains the WAL-reset fix, upstream in 3.51.3 and later or through a documented vendor backport. Run the compatibility and capacity checks on that target host. [SQLite WAL reference](https://sqlite.org/wal.html)

```bash
cat /etc/os-release
uname -r
nproc
free -h
findmnt -T /var/lib/f1ri
df -h /var/lib/f1ri
timedatectl show -p NTPSynchronized
/opt/f1ri/runtime/bin/python -c 'import sqlite3,sys; print(sys.version); print(sqlite3.sqlite_version)'
node --version
caddy version
```

Inspect the approved firewall and verify reachability from one permitted external client. Keep ports 3100 and 8100 on loopback. The example Caddy config serves HTTPS with an operator-provided certificate and disables HTTP redirects at Caddy. If the authorized topology permits an HTTP listener, configure any required HTTP-to-HTTPS redirect at the approved edge. Otherwise direct clients to the HTTPS origin. Confirm whether TCP-only ingress or HTTP/3 is approved. [Caddy TLS configuration](https://caddyserver.com/docs/caddyfile/directives/tls)

## Build and activate the pinned release

1. Use the reviewed S8-C.1 release commit. The archive pins the S8-C base `8080c1fc8d9163230283cfed4e9739b77db948ef`, frozen research inputs and locally built frontend. Stage the release under a versioned path such as `/opt/f1ri/releases/<commit>`. Record the exact deployed commit, Python dependency resolution, Node lock hash and build ID. Retain the previous compatible release and its dependencies when upgrading.
2. In an isolated build environment, install `python -m pip install '.[web,private,modeling]'` and run `python -m pip check`, `npm --prefix apps/web ci` and `python scripts/private_stack.py build`. Use the approved exact dependency versions for the host and keep the resolution with the release. Keep build credentials and production credentials out of the frontend build environment.
3. Check the model and evidence digests in the release manifest. Verify immutable release ownership and write protection for the running service. Keep `/var/lib/f1ri` writable by the dedicated service account with directory mode 0700. Place the production JSON and TLS key under restricted `/etc/f1ri` permissions. For port 443, arrange approved port mapping or grant only the required binding capability to the service. [systemd execution environment](https://www.freedesktop.org/software/systemd/man/latest/systemd.exec.html) · [Next.js self-hosting](https://nextjs.org/docs/app/guides/self-hosting)
4. From the pinned release as the service user, validate the Caddy config, run the existing `migrate` and `integrity` commands, and enroll approved OIDC subjects. Confirm the certificate and discovery issuer with the host's configured trust store. Do not promote loopback fixture accounts or credentials.
5. Install the [systemd service example](../../deploy/f1ri-private.service.example) under the intended unit name. Set the correct working directory, provision `StateDirectoryMode=0700` in the installed unit or drop-in, then run `systemd-analyze verify` against the installed unit. Point `/opt/f1ri/current` to the selected immutable release. Start the unit during the authorized window and check `systemctl is-active f1ri-private` and `/healthz`. Keep ingress restricted to authorized testers until the gates below pass.

## Verify the actual remote service

Use a desktop browser and a separate phone on the permitted external network. Use trusted hostname verification and the production IdP. Keep external tests separate from the loopback Playwright fixture. Download only a test workspace you created with approved inputs and record its canonical checksum instead of publishing the export.

- Verify DNS, the certificate chain and exact hostname, HTTPS readiness, protected API and frontend routes, security headers, and denied access to internal ports and raw archives. Record the network and trust scope.
- Try anonymous access, invitee sign-in, and denial of a provider-authenticated identity outside the invitation list. Confirm secure cookies, logout, expiration, revocation, and origin restrictions. Use two approved identities to check cross-owner workspace, export, job polling and cancellation denial.
- Execute fresh archived forecast, declared, conditional, robust, timing and shared-box cases. Compare canonical hashes to `api_parity.json` in the frozen S8-C evidence. Check the incomplete and complete search classifications, 46.754185-second timing regret, and 2.903848-second and zero-second waits. Verify historical physical withholding, unsupported 2026 and unsupported live strategy. Record wall time, CPU and resource use separately.
- Save the desktop investigation. Authenticate on the phone, reopen it, and reproduce the desktop receipt. Capture redacted screenshots and label the two verification environments.
- Use two invitees for distinct and identical submissions and observe one active worker, a full four-job queue, per-owner and global rejections, same-owner cache reuse, owner-scoped cancellation and failure. Exercise bounded timeout and worker interruption during an approved test window. Verify the service remains available and publishes no partial result. Record actual queue delay, CPU, RSS, disk usage and error counts.
- With a completed test workspace saved, exercise `sudo systemctl restart f1ri-private`. Verify completed workspace persistence, interrupted job terminal states and a fresh sign-in. On restart confirm that only the new service processes own the intended listeners.

Capture H1–H20 against the remote origin before approving Path A. The fixture uses a local CA and synthetic test identities. Those checks are recorded separately in the preflight archive.

From an unauthenticated external client, expect `200` on readiness, `303` at the frontend boundary and `401` on the protected API. Check the TLS trust result on every request and use the approved CA bundle where applicable.

```bash
curl --silent --show-error --dump-header - "$APP_ORIGIN/healthz"
curl --silent --show-error --output /dev/null --write-out '%{http_code}\n' "$APP_ORIGIN/"
curl --silent --show-error --output /dev/null --write-out '%{http_code}\n' "$APP_ORIGIN/api/v1/presets"
```

For the cold API parity sweep, run the following from the checked-out repository on an approved external client with Chrome, Python and the locked Node dependencies. Set `APP_ORIGIN` to the approved HTTPS origin. Use a newly enrolled test identity, before running any of the same analyses under that identity. Sign in through the provider in Chrome. The harness passes the short-lived app session in memory and stores only the scientific comparison and timing ledger. With approved private network trust, install the approved CA in the client trust store and pass its PEM bundle as the optional third argument for Python. Revoke or sign out the test session at the end of the exercise.

```bash
node apps/web/scripts/check-private-parity.mjs "$APP_ORIGIN" /path/to/approved/evidence/api_parity.json
```

## Backup, rollback and routine operations

- Start and stop with `sudo systemctl start f1ri-private` and `sudo systemctl stop f1ri-private`. Use `journalctl -u f1ri-private --since today` on the host for restricted logs and `scripts/private_admin.py --config /etc/f1ri/production.private.json status` for aggregate state. Monitor readiness externally, job counts and failures, timeouts, cancellation, CPU/RSS, disk usage, backup completion and the active model/release hash.
- Run the existing `backup` command on the approved schedule. Verify integrity, log status without workspace or credential content, and apply the approved retention policy only after successful backups. Keep backups on the approved encrypted destination outside the release.
- Restore to a new file and a compatible isolated instance. The launcher uses fixed local upstream ports, so run the restore instance in a separate approved host/network namespace, or during a controlled outage on the same host. Verify SQLite integrity, fresh login, and a known analysis checksum. Preserve the running database.
- For upgrade or rollback, preserve a checked backup, stop the unit, change the versioned release symlink, then start and recheck the unit, schema and workspace checksum. Confirm the earlier build supports the current schema. Return to the intended release after rehearsal. If only one compatible version exists, use two independent production builds and record that scope.
- Revoke a member at the provider and with `private_admin.py revoke`. Rotate client, flow and edge secrets through the approved host configuration. If sessions may be compromised, invalidate them before reopening ingress. See [monitoring and access revocation](private-beta.md#monitoring-and-access-revocation).
