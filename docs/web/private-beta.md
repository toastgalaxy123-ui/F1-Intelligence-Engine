# Authenticated private-beta plan

S8-B validation applies to a trusted local browser and loopback services. Remote access is a separate S8-C release gate.

| Area | Proposed S8-C design and verification |
| --- | --- |
| Identity and sessions | Begin with one explicitly enrolled user. Use a same-origin gateway with an established identity provider and short-lived secure session cookies. Enforce session validity, origin checks and CSRF protection at every write endpoint. Verify unauthenticated requests, expired sessions and sign-out. |
| Network boundary | Terminate TLS at the authenticated gateway. Run Next.js and the FastAPI worker on a private interface reachable only by that gateway. Restrict ingress, trusted host headers and forwarded origins. Exercise failures at both web and API routes before access. |
| Worker and job ownership | Keep one worker during the first hosted capacity study. Bind jobs, exports and replay permissions to the authenticated user. Enforce request and rolling job quotas with audited cancellation and strict limits. Keep tool-free worker processes isolated and recycle them after failures. |
| Workspace persistence | Keep browser storage for local use. If hosted persistence is enabled, store versioned bounded JSON with a user owner, digest and immutable result receipts. Apply retention limits. Encrypt storage and backups, and test restoration from a known manifest. |
| Evidence and licensing | Review redistribution terms separately for code, frozen models, all research inputs, fonts and the generated generic car artwork. Keep raw archives private and serve only approved summaries and exports. Review branding before publication. |
| Secrets and telemetry | Use server-side secret storage for client credentials. Keep job and health logs free of research payloads and tokens. Monitor queue depth, elapsed time, memory limit breaches, cancellation latency, cache reuse and authentication failures. |
| Rollback and recovery | Pin and record the deployed app commit, lockfile, schema, model and evidence digests. Retain the previous build. Rehearse rolling back the service and restoring workspace data without invalidating existing result receipts. |

The hosting decision requires observed access-control tests, restore and rollback exercises, a licensing review, and a capacity trial with the configured quotas. Live and historical strategy restrictions remain independent research gates.

Reference guidance for those deployment checks. [Next.js self-hosting](https://nextjs.org/docs/app/guides/self-hosting) · [OWASP session management](https://cheatsheetseries.owasp.org/cheatsheets/Session_Management_Cheat_Sheet.html)
