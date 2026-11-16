# Stage 8 S8-C validation report

- **Release** `PRIVATE_BETA_DEPLOYMENT_READY`
- **Decision** Path B
- **Validation environment** Production-equivalent HTTPS staging on loopback
- **Operational research state** `LIVE_UNSUPPORTED`

## Authenticated research workflows

Chrome signed in through Keycloak using the OIDC Authorization Code flow, rejected a valid identity outside the invitation list, and denied expired and revoked sessions. The application stores workspace documents and result receipts in SQLite under the authenticated owner's identity. A separate signed-in mobile browser reopened the shared-box investigation and reproduced its exact result checksum. A different account could neither retrieve nor change that workspace. The earlier browser documents survived both the explicit migration and the rejection of incompatible model and source hashes.

Identical analysis requests under two owners produced distinct job handles. The owner of one handle could cancel it while the other completed. Polling, traces, cancellation, workspace exports and result exports enforce ownership. The HTTPS staging probe verified the certificate chain, security headers, direct upstream rejection and loopback bindings. A scan of 20 compiled browser bundles found none of the staged service secrets.

## Recovery and reproducibility

The service was stopped with one analysis running and another queued. Both recovered as `FAILED / SERVICE_RESTART` and exposed no result. The pending workspace draft and earlier completed workspace survived. The SQLite backup passed integrity checks and was restored to a separate database with sessions cleared. Chrome reopened and verified the same saved checksum after restart, restore and rollback. Rollback switched between two independently built production frontends from the S8-C source and schema.

| Research output | Cross-environment result |
| --- | --- |
| Archived Singapore and replacement forecasts | Frozen canonical checksums matched |
| Manual and conditional strategy | Result checksums, decision support and comparison markers matched |
| Robust GREEN, VSC and SC analysis | Frozen analysis and recommendation support matched |
| Safety Car timing | 46.754185 seconds of frozen regret |
| Paired service-box comparison | Exact frozen checksum, with 2.903848 seconds versus 0 seconds of follower wait |
| Workspace replay | Frozen exported result reproduced exactly |

Two clean authenticated production rehearsals reproduced the eight inherited result hashes and the selected two-policy shared-box result. The replay and historical withholding controls matched. All 56 S8-B archive checksums and the three pinned S8-B release file hashes verified. The simulator, model and frozen source files were unchanged. The six existing S6/S7 regression output directories were rechecked against their frozen manifests.

## Engineering verification and capacity

- 466 Python tests passed, including the existing 458-test suite. TypeScript, ESLint, two frontend unit tests and the Next.js production builds passed. Separate process tests verified worker exit on supervisor loss and at the wall-time deadline.
- Five authenticated Chrome cases covered H1–H20. Eight original local Chrome workflows passed. The evidence archive includes 17 staged screenshots.
- The two cold rehearsals measured roughly 3.1–3.3 seconds for manual comparison, 4.0–4.5 seconds for robust comparison and 8.8–10.9 seconds for the frozen timing case. Observed peak worker RSS was about 354 MiB. Timings describe this staging machine under concurrent validation activity.
- The beta retains one worker, four outstanding jobs globally and two per user. Start with two invited users on a host allocation of two CPU cores and 2 GiB RAM for the application, with an independently operated identity provider. Monitor actual queue delay, worker resource usage, encrypted backup size and egress before expanding the beta.

## External deployment

The rehearsal ran on a Windows checkout with Caddy, a local CA and Keycloak's staging database. The Linux service definition, read-only release mount, external DNS and certificate, external identity configuration, and remote access still require an authorized host and an environment-specific smoke run. No remote instance or public access was provisioned. An operator needs approved hosting and ingress, a trusted certificate, a confidential OIDC client with the exact callback, enrolled subjects, encrypted persistent storage and backups, and permissions appropriate to the initial audience and historical source material.

D8R1 remains `MEAN_ONLY_VALIDATED` for 2023–2025. G3 calibration remains failed. The app withheld historical physical execution without the required state and returned `MODEL_ERA_UNSUPPORTED` for 2026.

[Setup and operating procedures](../../docs/web/private-beta.md) · [Licensing review](../../docs/web/private-beta-licensing.md) · [Release evidence](stage8_s8_c_evidence.zip) · [Metrics](stage8_s8_c_metrics.json)
