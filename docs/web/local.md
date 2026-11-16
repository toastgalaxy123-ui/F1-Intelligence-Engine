# Run F1 Race Intelligence locally

Use Python 3.11 or later and Node.js 20.9 or later. Run these commands from the repository root after checking out the published artifacts.

```powershell
python -m pip install -e ".[web,modeling,dashboard,dev]"
npm --prefix apps/web ci
npm --prefix apps/web run build
python scripts/run_local_web.py
```

Open [the workspace](http://127.0.0.1:3000). The [API contract](http://127.0.0.1:8000/docs) is available separately. Use `python scripts/run_local_web.py --dev` for development. Use Ctrl+C to stop the processes. To run them separately:

```powershell
python -m uvicorn f1ri.api.app:app --host 127.0.0.1 --port 8000 --workers 1
npm --prefix apps/web run dev
# After the production build, replace "dev" with "start".
```

The backend defaults to the editable repository checkout. Set the operator-side `F1RI_REPO_ROOT` to an absolute checkout path when needed. The pinned model and evidence paths are relative to that checkout. Copy `apps/web/.env.example` to `apps/web/.env.local` to override the browser API URL on loopback. Next.js reads that public address at build time. Keep credentials out of `NEXT_PUBLIC_*` values.

## Validate the release

```powershell
python -m pytest -q tests/api
python -m pytest -q
python -m ruff check src tests scripts
npm --prefix apps/web run lint
npm --prefix apps/web run typecheck
npm --prefix apps/web run build
# With both local servers running and Chrome installed
npm --prefix apps/web test
python scripts/verify_s8a.py --output artifacts/runs/stage8_s8_a_web_reproduced
```

Playwright uses installed Chrome by default. Set `F1RI_BROWSER_CHANNEL` to another installed Chromium channel if needed. Set `F1RI_SCREENSHOTS` and `F1RI_UI_REPORT` to absolute destinations for the 26 browser captures and raw test report. Supply `--ui-report` to the release verification script to attach those captures. The underlying Python engine and previous reproducibility commands are available independently of this frontend.

## Private hosting boundary

The validated release is for a trusted user on localhost. Remote deployment is gated on access control. Before offering private remote access, place both services behind an authenticated, TLS-terminating same-origin gateway, keep the API and worker bound privately, define allowed hosts and browser origins explicitly, and add authenticated per-user isolation, throttling and audit logging. Validate that gate separately. Public hosting and live strategy are outside this release.
