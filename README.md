# Ulak

Ulak is a local PWA for viewing and controlling Herdr agent panes. Python with FastAPI and Uvicorn keeps the async Unix-socket client, SSE stream, and JSON API in one small backend; the frontend has no build step.

## Architecture

- `app.py` serves a FastAPI backend on `127.0.0.1:8765` and a static PWA.
- `herdr_client.py` speaks newline-delimited JSON over `~/.config/herdr/herdr.sock`.
- Startup subscribes to the current pane set and adds/removes pane subscriptions as lifecycle events arrive.
- Herdr events flow through the backend to browser clients as Server-Sent Events; the PWA uses REST for reads and actions.
- Push notifications use local VAPID keys and the browser Push API. No relay service is used.

## API

- `GET /api/sessions`: enriched cards, ordered blocked > working > done (<1h) > idle.
- `GET /api/sessions/{pane}/chat`: cleaned screen split into blocks (Claude JSONL when available).
- `POST /api/sessions/{pane}/prompt`: send text. `POST /api/quick`: quick send to a configured pane (see Configuration). `GET/PUT /api/pins`: server-side pins, shared across devices.
- `POST /api/sessions/{pane}/approve`: approve/always/deny, bound to the screen fingerprint (409 if it changed; destructive = two steps).
- `POST /api/sessions/{pane}/choose`: pick a menu option (single/multi, trust dialog). `POST .../keys`: raw keys (Enter/approve keys refused on destructive prompts).
- `POST /api/sessions/{pane}/close`: two-step close. `GET /api/projects`, `POST /api/sessions`: start a new session.
- `GET /api/events`: SSE (`session`, `removed`). `GET /api/limits`. `/api/push/*`: public key, subscribe, known.

## Security

The server binds only to `127.0.0.1`; Tailscale Serve is an external deployment step. Requests require the `Tailscale-User-Login` header to match `ULAK_ALLOWED_LOGIN`. Destructive pending commands are checked against a built-in list plus an optional rules file (see Configuration). They require a confirmation token on a second request; normal approvals take one request. This header check assumes the app is reachable only through trusted Tailscale Serve, which strips client-supplied identity headers and supplies its own.

## Run

```sh
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
python app.py
```

Set `ULAK_ALLOWED_LOGIN` to the exact Tailscale login before starting. Generate local push keys once with `python tools/generate_vapid.py`; the app serves the public key to the PWA and reads the private key from `.secrets/vapid_private.pem`. Set `ULAK_VAPID_SUBJECT` to a `mailto:` contact for push delivery. Keep `.secrets/` private and untracked. Enable notifications with the button in the app; the browser requires HTTPS (Tailscale Serve) and user permission.

## Configuration

Optional, in `.secrets/config.json` (untracked):

```json
{
  "rules_file": "~/rules.md",
  "quick_targets": ["main-agent"],
  "quick_label": "Main",
  "limit_files": {"claude": "~/limit.json"}
}
```

- `rules_file`: extra destructive patterns, one per line as ``- desen: `regex` ``.
- `quick_targets` / `quick_label`: workspace or project names that get the quick-send button.
- `limit_files`: files exposed by `GET /api/limits`.

State (`.state/`: pins, notification history) lives on the server, so every device sees the same view. Nothing user-specific is stored in the browser except drafts.

## Tests

```sh
pip install pytest && pytest
```

## License

MIT
