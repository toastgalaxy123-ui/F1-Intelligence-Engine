# Private research hosting

S8-C packages the existing race research workspace for a single private host. Caddy terminates HTTPS and verifies each frontend request through FastAPI. Next.js serves the current interface using same-origin API calls. A single FastAPI process owns a local SQLite database and starts one bounded Python child per analysis. The OpenID Connect provider handles account authentication and can be operated independently. The unmodified simulator loads the pinned D8R1 model and checked evidence from the release checkout.

| Boundary | Production configuration |
| --- | --- |
| Ingress | Caddy on the configured HTTPS origin. Internal Next.js port 3100 and FastAPI port 8100 bind to loopback. The launcher defaults to loopback. The operator explicitly enables ingress when hosting is authorized. |
| Identity | OIDC Authorization Code with PKCE S256. The operator enables an exact issuer and registers individual subjects. Email and display names have no role in access decisions. |
| Config | The deployment reads an absolute, private JSON configuration from disk. Settings cover the HTTPS origin, IdP/CA, credentials, queue, sessions, storage and API log level (`INFO` or `WARNING`). The browser calls the same-origin `/api/v1`. |
| Storage | A dedicated, access-restricted disk path for SQLite, WAL, the queue lock, gateway state and worker scratch. Mount the release checkout read-only. The worker cannot load paths supplied by a research request. |
| Health | `GET /healthz` returns readiness after the model, evidence and database are opened. The authenticated `/api/v1/health` also returns research status and worker ceilings. |

## Loopback staging

Install the repository's web, private and modeling Python extras in a virtual environment and install the locked frontend packages. The staged reference used Caddy 2.11.9 and Keycloak 26.8.2 with Java 25. The same release layout is suitable for a maintained Linux host. [Keycloak release](https://github.com/keycloak/keycloak/releases/tag/26.8.2) · [Keycloak supported configurations](https://www.keycloak.org/server/supported-configurations) · [Caddy release](https://github.com/caddyserver/caddy/releases/tag/v2.11.9)

```powershell
python -m pip install -e '.[web,private,modeling]'
npm --prefix apps/web ci
$stage = Join-Path $PWD 'artifacts/runs/stage8_s8_c_staging'
python scripts/prepare_private_staging.py --directory $stage
$env:F1RI_STAGE_DIR = $stage
```

The staging generator creates a seven-day fixture certificate and two invited identities plus one uninvited identity. Restrict this directory to your account and keep its files outside the release archive. Copy `realm.private.json` into a fresh Keycloak distribution's `data/import/f1ri-stage-realm.json`. From the repository directory, run Keycloak in a dedicated terminal with the following arguments. Set `$env:JAVA_HOME` to the supported local Java installation and `$keycloak` to the Keycloak directory.

```powershell
& "$keycloak/bin/kc.bat" start --import-realm --db=dev-file --cache=local `
  --http-host=127.0.0.1 --http-enabled=false --https-port=9443 `
  --hostname=https://127.0.0.1:9443 --https-certificate-file="$stage/edge.pem" `
  --https-certificate-key-file="$stage/edge.key"
```

Build and run the private application in a second terminal. Use the full executable path with `--caddy` if Caddy is outside PATH.

```powershell
$stage = Join-Path $PWD 'artifacts/runs/stage8_s8_c_staging'
python scripts/private_stack.py build
python scripts/private_stack.py serve --config "$stage/staging.private.json" `
  --cert "$stage/edge.pem" --key "$stage/edge.key"
```

Open `https://localhost:8443` with a client that trusts the staging CA. Chrome acceptance uses the fixture's SPKI pin. Verify TLS independently with a client that trusts only the fixture CA for this request.

```powershell
$stage = Join-Path $PWD 'artifacts/runs/stage8_s8_c_staging'
$env:F1RI_STAGE_DIR = $stage
$env:F1RI_PRIVATE_BROWSER_REPORT = Join-Path $PWD 'artifacts/runs/stage8_s8_c_browser.json'
curl.exe --cacert "$stage/ca.pem" https://localhost:8443/healthz
$env:F1RI_PRIVATE_SCREENSHOTS = Join-Path $PWD 'artifacts/runs/stage8_s8_c_private_hosting/screenshots'
npm --prefix apps/web run test:private -- --grep-invert 'H15'
python scripts/verify_private_deployment.py --stage $stage `
  --output artifacts/runs/stage8_s8_c_private_hosting/deployment_smoke_tests.json
```

For a fresh acceptance run, start with a fresh staging fixture so previous jobs and quotas cannot affect the cache and cancellation cases. For the restart and rollback rehearsal, stop the application services, leave Keycloak running, and build a second frontend with `python scripts/private_stack.py build --build-slot candidate`. Set `F1RI_REHEARSAL_CADDY` to the local Caddy executable and set `F1RI_PRIVATE_BROWSER_REPORT` to a separate recovery report such as `artifacts/runs/stage8_s8_c_browser_recovery.json` using its full path. Run `npm --prefix apps/web run test:private -- --grep 'H15 H20'`. The test creates new databases under the disposable staging directory. The original local workflow uses [local web setup](local.md).

## Authorized private deployment

Use a dedicated machine or VM, a supported Python and Node runtime, Caddy, and encrypted persistent storage. Use a controlled ingress and a DNS name with a valid TLS certificate for the exact origin. Start with two invited researchers and one worker. Review source and branding permissions before enabling access beyond approved research users.

1. Install one versioned release checkout with its locked Node dependencies and Python extras. Verify the release/model checksums and build the private frontend before locking the release directory read-only.
2. Register a confidential OIDC web client with an approved provider. Set its exact redirect URI to `<APP_ORIGIN>/auth/callback`. Enable Authorization Code and PKCE S256. Disable public registration for the beta, require provider-side MFA where available and enroll specific provider subjects.
3. Copy [the environment example](../../deploy/private.example.json) to `/etc/f1ri/production.private.json`. Replace all example values and store config and certificate keys with access limited to the service account. Generate independent flow and gateway secrets with `python -c "import secrets; print(secrets.token_urlsafe(48))"`. Use the public CA trust store by default or supply a readable pinned provider CA as `issuer_ca`.
4. Create the database and enroll the users. Use the service account or assign it ownership of the state files before launching the service.

```bash
python scripts/private_admin.py --config /etc/f1ri/production.private.json migrate
python scripts/private_admin.py --config /etc/f1ri/production.private.json enroll --subject APPROVED_SUBJECT --label "Researcher"
python scripts/private_stack.py build
python scripts/private_stack.py validate --config /etc/f1ri/production.private.json \
  --cert /etc/f1ri/edge.pem --key /etc/f1ri/edge.key
python scripts/private_stack.py serve --config /etc/f1ri/production.private.json \
  --cert /etc/f1ri/edge.pem --key /etc/f1ri/edge.key --bind-interface 0.0.0.0
```

The [systemd example](../../deploy/f1ri-private.service.example) configures a dedicated service user, read-only release paths, a writable state directory and service-wide shutdown. Adjust the interpreter, release path and network interface to the authorized host before installation. Expose only the configured HTTPS port. For low ports use an approved listener, port mapping or service capability. Record the checkout commit, Python dependency resolution, package lock, build ID, model digest and SQLite schema before enabling client access. Confirm HTTPS and authentication with an invited account from the permitted network.

## Workspaces, quotas and recovery

Authenticated Workspaces saves, renames, duplicates, deletes, exports and reopens investigations. The saved receipt contains original analysis inputs and the canonical checksum. When moving between the original localhost origin and private hosting, export from the original app and import from the private app. The private workspace shelf also offers an explicit copy for S8-B documents accessible on its browser origin. Both flows verify the archived source and model hashes and leave the earlier copies in place.

Each user can have two queued or active jobs and 50 saved workspaces by default. The server accepts up to four outstanding jobs total and runs one at a time. It enforces 90 seconds of queue wait, 35 seconds of worker wall time, a sampled 720 MiB RSS ceiling, 16 KiB request documents and a 3 MB worker result and trace envelope. The owner can cancel their own job. Identical submissions for the same owner can reuse that owner's run. A second user receives an independent handle. A process restart fails any unfinished entries with `SERVICE_RESTART`. Saved inputs can be submitted again.

Keep encrypted backups at a restricted location. Use powershell equivalents where applicable.

```bash
python scripts/private_admin.py --config /etc/f1ri/production.private.json status
python scripts/private_admin.py --config /etc/f1ri/production.private.json backup --destination /secure-backups/f1ri-YYYYMMDD.sqlite3
python scripts/private_admin.py --config /etc/f1ri/production.private.json restore \
  --source /secure-backups/f1ri-YYYYMMDD.sqlite3 --destination /var/lib/f1ri/isolated-restore.sqlite3
python scripts/private_admin.py --config /etc/f1ri/isolated-restore.private.json integrity
```

For a restore exercise point an isolated config at the new database, verify the pinned assets, sign in again, and reproduce a known workspace receipt. Restore explicitly removes saved session hashes. Keep the current and previous build plus pinned release directories when updating. Confirm both support the database schema, take a backup, stop services, switch to the earlier release and its corresponding build, restart, check health, authenticate and verify a saved receipt. For an incompatible schema, validate an isolated restored copy first and decide how to handle data written after the backup before cutting over.

## Monitoring and access revocation

Monitor HTTPS readiness, response failures, route-denial counters, terminal job events, CPU and sampled RSS, backlog, database usage and the deployment version. Use `private_admin.py status` for aggregate retained job counts, the schema and the model manifest ID. Restrict access to logs and state. Review access when protected keys or devices are lost.

To remove a member, disable access at the provider and run the following command with the corresponding OIDC subject. This revokes all of that user's app sessions immediately and keeps their saved workspaces under their owner for operator-controlled recovery.

```bash
python scripts/private_admin.py --config /etc/f1ri/production.private.json revoke --subject REVOKED_SUBJECT
```

For a secret or storage incident, block ingress, revoke affected accounts, rotate the client and gateway/flow secrets, remove app sessions or restore into an isolated database with fresh sessions, and inspect the limited event logs for the incident window. Preserve the encrypted original and restore service only after HTTPS, invitation and isolation checks pass.

The app remains `LIVE_UNSUPPORTED`. Forecasts display the frozen `MEAN_ONLY_VALIDATED` classification. The model supports 2023–2025 and returns `MODEL_ERA_UNSUPPORTED` for 2026. The [S8-C validation report](../../artifacts/reports/stage8_s8_c_validation.md) records the staging observations.
