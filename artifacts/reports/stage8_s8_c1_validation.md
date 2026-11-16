# Stage 8 S8-C.1 private-beta launch preflight

- **Release** `PRIVATE_BETA_DEPLOYMENT_READY` (Path B)
- **Evidence scope** Fresh Windows loopback HTTPS rehearsal
- **Remote deployment** Pending an approved host, ingress and identity configuration
- **Research status** `LIVE_UNSUPPORTED`

## Frozen release and local results

The evidence ZIP, metrics and report from S8-C matched the three uploaded hashes. All 49 archive members matched the S8-C checksum manifest. The D8R1 model, research sources, simulator, backend and deployment configuration matched the pinned S8-C release.

The full Python baseline passed with **466 tests** using the repository's supported pandas range. The first run picked up pandas 3.1.1 from an older dependency directory and failed one historical audit. Using pandas 2.3.3 restored the baseline without changing research code. TypeScript, ESLint, two frontend unit tests and the production builds passed. The eight original local Chrome cases also passed.

The fresh browser run surfaced a sign-out race. Session polling and research API requests could redirect the page while logout was still finishing. Both now defer the sign-in redirect during sign-out. A Chrome regression holds the logout response until a session poll and workspace request receive 401 responses, then verifies the signed-out page and confirms the old session is rejected.

Five Chrome cases verified the invited and uninvited paths, workspace replay in separate desktop and mobile-sized browser contexts, cross-owner denial, stale sessions, cancellation, quotas and recovery. The strict TLS probe verified the loopback CA chain and expected protection of the app, API and raw research paths. A restart with one active and one queued job returned `FAILED / SERVICE_RESTART` for both. The saved checksum survived an isolated SQLite restore and a rollback across two independently built compatible frontends. The evidence includes 17 local screenshots.

## Scientific results

Two cold authenticated runs matched every frozen canonical result tested, including the archived Singapore forecast, declared strategy, both conditional-search outcomes, robust GREEN/VSC/SC, Safety Car timing, paired shared-box execution and workspace replay. The declared comparison retained its exact tie, `NO_UNIQUE_RECOMMENDATION`, and `CONSTRAINT_DOMINATED_IDENTIFIABILITY`. The original Safety Car regret was **46.754185 seconds**. Shared-box waits were **2.903848 seconds** and **0 seconds**. D8R1 remains `MEAN_ONLY_VALIDATED` for 2023–2025. The application continued to withhold unsupported historical physical strategy and 2026 model execution.

## External activation

The approved Linux host, service access, resource and spending limits, exact app origin and TLS trust, production OIDC client, approved users and research audience, encrypted storage and backup policy are still required. Production DNS/TLS, identity enrollment, remote device access, target capacity, systemd and target-host recovery have zero external test evidence. The operator checklist defines those gates and includes a Chrome-based cold parity command for the approved origin.

The local Python runtime loaded SQLite 3.50.4. Require a SQLite build with the WAL-reset fix before opening the beta, such as upstream 3.51.3 or later or a documented vendor backport. Recheck backup, integrity and recovery on the actual target filesystem. [SQLite WAL reference](https://sqlite.org/wal.html)

[Operator checklist](../../docs/web/remote-private-beta.md) · [Checksummed evidence](stage8_s8_c1_evidence.zip) · [Metrics](stage8_s8_c1_metrics.json)
