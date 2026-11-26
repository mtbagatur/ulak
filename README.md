<p align="center">
  <img src="web/icon-192.png" alt="Ulak icon" width="88">
</p>

<h1 align="center">Ulak</h1>

<p align="center"><a href="https://github.com/mtbagatur/ulak/actions/workflows/test.yml"><img src="https://github.com/mtbagatur/ulak/actions/workflows/test.yml/badge.svg" alt="Tests"></a></p>

![Ulak demo: an approval, a push that needs two seals, and an answer refused because the question changed](docs/demo.gif)

<p align="center">
  <strong>Your coding agents, in your pocket.</strong><br>
  Approve a command, answer a question, send a nudge. Then get back to your coffee.
</p>

Ulak is a small web app (PWA) that turns your [Herdr](https://herdr.dev) agent panes (Claude Code, Codex, Antigravity and plain terminals) into cards and conversations on your phone. Agents keep running on your computer. Ulak is reachable only through your own [Tailscale](https://tailscale.com) network: no account, no relay server, no app store, no build step.

<p align="center">
  <img src="docs/screenshots/en/home.png" alt="Home: sessions grouped by what needs you, with usage limits on top" width="195">
  <img src="docs/screenshots/en/approval.png" alt="A session with an approval waiting under the conversation" width="195">
  <img src="docs/screenshots/en/second-seal.png" alt="A git push asking for a second confirmation with the full command" width="195">
  <img src="docs/screenshots/en/delivered.png" alt="The line above the composer: approved, on its way, agent received it" width="195">
  <br>
  <img src="docs/screenshots/en/refused.png" alt="An approval refused because the question changed" width="195">
  <img src="docs/screenshots/en/decisions.png" alt="The decision log: who answered what, and when the agent got it" width="195">
  <img src="docs/screenshots/en/changes.png" alt="Read-only view of the uncommitted changes in a session's folder" width="195">
  <img src="docs/screenshots/en/light.png" alt="The light theme" width="195">
  <br>
  <sub>Demo data, retaken with <code>python tools/shots.py</code>. English and Turkish are built in; the app follows your phone's language and has a TR/EN switch. <a href="docs/screenshots/tr">Turkish screenshots</a>.</sub>
</p>

## What it does

- **See what needs you.** Sessions are grouped as Needs you, Working, Just finished (for five minutes) and Idle. Pin the ones you care about; pins live on the server, so every device shows the same list.
- **Approve from the card.** A normal approval takes one tap. The decision is bound to the exact screen you saw: if the prompt changed in the meantime, the server refuses (409) instead of approving something else.
- **Destructive commands need two steps.** `rm`, `git push`, `ssh`, `kubectl delete`, cloud CLIs and your own patterns show the full command and ask for a confirmation token on a second request. Anything too long to fit on screen can only be denied.
- **Read it like a chat.** Claude Code transcripts become messages and tool cards; other agents are parsed from the screen.
- **Stay up to date.** Open chats refresh automatically; slash commands appear as you type. Web links and supported workspace documents open from the conversation.
- **Answer questions and menus.** Single and multi-select questions, trust dialogs, raw keys when you need them.
- **Attach files.** The paperclip uploads a photo, screenshot, PDF or text file (15 MB max, kept a week); agents read the path and supported documents open from chat.
- **Push notifications.** Standard Web Push with your own VAPID keys. Nothing goes through a third-party service; a fresh visible-page heartbeat suppresses push while Ulak is on screen, and quick turns (under two minutes by default) do not buzz your phone when they finish.
- **See your limits.** A strip on the home screen shows the tightest usage window per provider (5-hour and 7-day), turns orange and red as it fills, and opens to resets and staleness. Point it at the files your own scripts already write.
- **English and Turkish.** Follows the phone's language; switch with the TR/EN button. Push text follows the language of each device.
- **Start and close sessions** from the phone (closing is two steps).

## Try it in one minute

No Herdr needed, no setup:

```sh
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
python tools/demo_server.py      # http://127.0.0.1:8766
```

The demo is the real app over a scripted, fake machine that lives in the browser tab (`tools/demo/demo.js`); nothing leaves the tab. Three scenes are worth trying in order:

1. **billing-api**: approve the test run. Ulak moves you to the next session that waits.
2. **web-app**: a `git push` needs two seals. After the second, the line above the composer follows your answer until the agent has it, and the agent resumes.
3. **infra**: a little later a third agent stops. Approve it: the question changed while you were reading, so Ulak refuses your tap, shows the new question, and puts the refusal on the record (ledger icon, Decisions).

`?speed=4` hurries the agents. `python tools/demo_server.py --build DIR` writes the same demo as static files for any web host.

Run the test suite with:

```sh
pip install -r requirements.txt -r requirements-dev.txt
python -m pytest -q
```

Browser cases use Chrome or Chromium with a loopback-only fake API. They skip when a browser is unavailable.

## How it compares

| | Ulak | [HerdrChat](https://github.com/cobanov/herdrchat) |
|---|---|---|
| Form | PWA served from your machine | native iOS / Android app |
| Connection | Tailscale Serve, identity checked per request | SSH |
| Install | add to home screen | App Store |
| Approvals | screen-bound, two-step for destructive commands | approval choices |

They solve the same problem from different sides. HerdrChat is the polished native route; Ulak is the one you can read in an afternoon and run without installing anything on the phone.

## Architecture

- `app.py` serves a FastAPI backend on `127.0.0.1:8765` and a static PWA.
- `herdr_client.py` speaks newline-delimited JSON over `~/.config/herdr/herdr.sock`.
- Startup subscribes to the current pane set and adds/removes pane subscriptions as lifecycle events arrive.
- Herdr events flow through the backend to browser clients as Server-Sent Events; the PWA uses REST for reads and actions.
- Push notifications use local VAPID keys and go directly to the browser's Web Push endpoint. The [encrypted payload](https://www.rfc-editor.org/rfc/rfc8291) includes the project label, a short command or output preview, and the pane link. The device can display the preview on its lock screen.

## API

- `GET /api/sessions`: enriched cards, ordered blocked > working > done (five minutes) > unknown > idle.
- `GET /api/sessions/{pane}/chat`: cleaned screen split into blocks (Claude JSONL when available).
- `POST /api/sessions/{pane}/prompt`: send text. `POST /api/sessions/{pane}/upload`: raw file body (image, PDF or text), returns its server path. `GET /api/sessions/{pane}/file`: open a supported document inside that workspace. `POST /api/quick`: quick send to a configured pane (see Configuration). `GET/PUT /api/pins`: server-side pins, shared across devices.
- `POST /api/sessions/{pane}/approve`: approve/always/deny, bound to the screen fingerprint (409 if it changed; destructive = two steps).
- `POST /api/sessions/{pane}/choose`: pick a menu option (single/multi, trust dialog). `POST .../keys`: raw keys (Enter/approve keys refused on destructive prompts).
- `POST /api/sessions/{pane}/close`: two-step close. `GET /api/projects`, `POST /api/sessions`: start a new session.
- `GET /api/events`: SSE (`session`, `removed`). `POST /api/presence`: report whether a browser tab is visible. `GET /api/limits`: `{providers: [{id, label, updated, items: [{label, used, resets_at}]}]}`. `/api/push/*`: public key, subscribe, known, lang.

## Security

The server binds only to `127.0.0.1`; Tailscale Serve is an external deployment step. Requests require the `Tailscale-User-Login` header to match `ULAK_ALLOWED_LOGIN`. Destructive pending commands are checked against a built-in list plus an optional rules file (see Configuration). They require a confirmation token on a second request; normal approvals take one request. This header check assumes the app is reachable only through trusted Tailscale Serve, which strips client-supplied identity headers and supplies its own.

## Run

One command (venv, dependencies, push keys, systemd user service):

```sh
ULAK_ALLOWED_LOGIN=you@github ULAK_VAPID_SUBJECT=mailto:you@example.com ./install.sh
tailscale serve --bg 8765
```

The unit it installs is `deploy/ulak.service.example`. Without systemd, run it by hand:

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
  "limit_files": {"claude": "~/limit.json", "agy": "~/agy-usage.txt"},
  "project_roots": ["~", "~/code"],
  "min_work_seconds": 120,
  "approval_timeout_minutes": 0
}
```

- `rules_file`: extra destructive regexes, one per line. Blank lines and lines starting with `#` are ignored. Also accepted: markdown list ``- pattern: `regex` ``.
- `quick_targets` / `quick_label`: workspace or project names that get the quick-send button.
- `limit_files`: usage-limit files shown in the limits strip. Two formats are understood: JSON `{"h5": {"used_percentage": 4, "resets_at": 1795200600}, "d7": {...}, "ts": 1795183857}` (epoch seconds), or tab-separated text `name<TAB>window<TAB>NN%<TAB>ISO-reset` where NN% is what is *remaining*.
- `project_roots`: folders whose subfolders appear in the new-session picker (default `["~"]`). New sessions can only start inside your home folder.
- `min_work_seconds`: a "finished" push is skipped when the turn took less than this (default 120).
- `approval_timeout_minutes`: off by default (`0`), so an approval waits for you however long that takes. Set it and Ulak denies an approval nobody answered in that many minutes. It can only deny, only the exact prompt it has been timing, and each one is written to the decision log as a timeout. The clock restarts when Ulak restarts.

State (`.state/`: pins, notification history) lives on the server, so every device sees the same view. Nothing user-specific is stored in the browser except drafts.

## Tests

```sh
pip install -r requirements.txt -r requirements-dev.txt
python -m pytest -q
```

## License

MIT
