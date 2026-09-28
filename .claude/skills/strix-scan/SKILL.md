---
name: strix-scan
description: Run a Strix AI penetration test against a local instance of AI Test Generator (web app, API and source tree). Use when asked to security-test, pentest or run Strix on this project.
---

# Strix security scan

[Strix](https://github.com/usestrix/strix) runs autonomous security-testing agents inside a Docker
sandbox. Only scan systems you own — here, a local copy of this app.

## Prerequisites

1. Docker is running (`docker info` succeeds).
2. Strix is installed: `uv tool install strix-agent --python 3.12` (or `pipx install strix-agent`).
   Strix needs Python 3.12+.
3. An LLM key in the environment. Reuse the project's Gemini key without writing it anywhere:
   ```bash
   export STRIX_LLM="gemini/gemini-2.5-pro"
   export LLM_API_KEY="$GEMINI_API_KEY"
   ```
   The key must come from a Google AI project that still has access to this model (Google no
   longer offers it to new users), with billing enabled: a scan makes many requests.

## Run

1. Start the app locally from `backend/`:
   ```bash
   python -m uvicorn main:app --host 0.0.0.0 --port 8000
   ```
2. Scan the running app plus the source tree (white-box). Strix runs in Docker, so reach the host
   through `host.docker.internal`:
   ```bash
   strix --target http://host.docker.internal:8000 --target ./ \
         --scan-mode quick --max-budget 5 \
         --instruction "Focus on authentication (GitHub OAuth + session cookie), the /api/runs/* upload endpoints, SSRF via spec_url/base_url, and the code-execution sandbox in backend/sandbox.py."
   ```
3. Results are written to `./strix_runs/<run-name>/`. Summarise confirmed findings with file and line
   references, and propose fixes. Do not commit `strix_runs/`.

## Notes

- Never paste API keys into commands that are logged or into files in the repository.
- `-m quick` keeps cost low; use `standard` or `deep` before a release.
