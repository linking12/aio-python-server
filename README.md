# aio-python-server

A standalone extraction of the **`python-server`** component from
[AIO Sandbox](https://github.com/agent-infra/sandbox) — the unified control-plane
API that sits at the heart of that container (the 158-route FastAPI service exposing
shell / file / browser / code / jupyter / MCP over one port).

This repo pulls that one service out of the all-in-one Docker image so it can be read,
run, and hacked on by itself, without the browser/VNC/VSCode/nginx stack around it.

---

## ⚠️ Provenance & license — read first

- **Source:** copied verbatim from the Python site-packages of the published image
  `ghcr.io/agent-infra/sandbox:latest` (image version **1.11.0**, arm64). The wheel is
  named `python-server 0.1.0`.
- **It is not part of the public `agent-infra/sandbox` git repo.** That repo is
  Apache-2.0 and ships only SDKs/examples/docs; this server is distributed **only** as a
  prebuilt binary layer inside the image. Its own license is **unstated / proprietary
  (ByteDance)**.
- Treat this as **vendored third-party code for local study and experimentation only.**
  Do not publish or redistribute it. The image also carries ByteDance-internal artifacts
  (e.g. `bytedpypi.byted.org` references, `vendors/bytedlogger`); none of that is yours
  to ship.

---

## What's here

```
aio-python-server/
├── src/
│   ├── app/            # the server itself (FastAPI app, 19 services, MCP hub)
│   ├── vendors/        # vendored deps the server imports as top-level `vendors.*`
│   │   ├── openhands/      #   OpenHands bash runtime (tmux-backed stateful shell)
│   │   ├── openhands_aci/  #   OpenHands ACI (file editor etc.)
│   │   └── bytedlogger/    #   ByteDance logger shim
│   └── browser_sdk/    # first-party CDP browser driver (not on PyPI → vendored)
├── config/
│   └── mcp-hub.json    # external MCP servers to aggregate (empty by default)
├── requirements.txt    # = the wheel's Requires-Dist (all public PyPI, bar browser-sdk)
├── pyproject.toml
├── setup.sh            # create .venv (Python 3.12) + pip install
└── run.sh              # launch the server
```

The server is plain, un-obfuscated Python. Nothing was decompiled; `.py` sources were
copied as-is and `__pycache__` stripped.

## Requirements

- **Python 3.12** (hard requirement — the code uses 3.12-only syntax such as
  backslashes inside f-strings). `brew install python@3.12`.
- For the **stateful shell** (`/v1/shell`): **tmux** on `PATH` (`brew install tmux`) —
  it drives OpenHands' `BashSession` through tmux.

## Run

```bash
./setup.sh      # one-time: venv + deps  (set $PYTHON if python3.12 isn't on PATH)
./run.sh        # starts on http://127.0.0.1:8091
```

Then:

```bash
curl http://127.0.0.1:8091/health
open  http://127.0.0.1:8091/v1/docs          # Swagger UI
curl -X POST http://127.0.0.1:8091/v1/bash/exec \
     -H 'content-type: application/json' \
     -d '{"command":"echo hello from aio"}'
```

`run.sh` honours `HOST`, `PORT`, `WORKSPACE`, `VENV` env vars.

## What works standalone vs. what needs the container's backends

This service is, by design, a **facade** over backends that the full container also
ships. Pulled out on its own, the picture splits:

| Endpoint group | Standalone (this repo) | Needs |
|---|---|---|
| `/health`, `/v1/docs`, `/auth` | ✅ | — |
| `/v1/bash/*` | ✅ | `/bin/bash` (present on macOS) |
| `/v1/file/*` (read/write/grep/glob/str_replace_editor) | ✅ | local filesystem |
| `/v1/util/convert_to_markdown` | ✅ | MarkItDown (in deps) |
| `/v1/shell/*`, `/v1/shell/ws` | ✅ | **tmux** (stateful) + terminado (PTY, in deps) |
| `/v1/jupyter/*`, `/v1/code` (python) | ✅ | ipykernel/jupyter-client (in deps) |
| `/mcp`, `/v1/mcp/*` | ✅ | first-party tools; external servers via `config/mcp-hub.json` |
| `/v1/sandbox/observe/*` | ✅ | in-process |
| `/v1/nodejs/*`, `/v1/code` (javascript) | ⚠️ partial | the Node REPL servers the image runs at `/opt/repl-servers` (not extracted) |
| `/v1/proxy/*` | ⚠️ degraded | these just write nginx port-proxy config; no nginx here to enforce it |
| `/v1/browser/page/*` (navigate/click/screenshot/html/markdown/…) | ✅ | `pip install -r requirements-browser.txt` + any local Chrome on CDP `:9222` (see below). No X needed on macOS. |
| `/v1/browser/restart`, `/v1/display/*`, `/screenshot`, VNC | ❌ | depend on the container's native `agent-browser` daemon + Xvnc display — not extracted |

### Browser endpoints on macOS (verified)

`browser_sdk` is only a CDP *client* (`playwright.chromium.connect_over_cdp`); it never
launches a browser. So you supply your own Chrome — no X server required on macOS:

```bash
pip install -r requirements-browser.txt          # playwright python pkg only (no bundled browsers)

"/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" \
  --headless=new --remote-debugging-port=9222 '--remote-allow-origins=*' \
  --user-data-dir=/tmp/aio-chrome &

BROWSER_REMOTE_DEBUGGING_PORT=9222 ./run.sh       # server connects to 127.0.0.1:9222
# curl -X POST :8091/v1/browser/page/navigate -d '{"url":"https://example.com"}'
# curl :8091/v1/browser/page/markdown
```

The host is hardcoded `127.0.0.1`; the port comes from `BROWSER_REMOTE_DEBUGGING_PORT`
(default 9222). `--remote-allow-origins=*` is required or Chrome rejects the CDP websocket.

In short: **shell, file, code (python), jupyter, markdown, MCP, and observe run on their
own.** Browser automation and the desktop/display features assume the rest of the
container and won't function without a CDP Chrome + X server.

## How it fits the original architecture

Inside AIO Sandbox this process is `SANDBOX_SRV_PORT=8091`, reached through nginx on
`:8080`. nginx handled TLS/auth/routing and fanned `/v1/*`, `/mcp`, `/v1/shell/ws` to it.
Here you talk to it directly on `:8091`; there is no nginx, no auth gateway, and no
per-port preview proxy — those lived in the container's `/opt/gem` bash+nginx layer,
which is **not** part of this extraction.
