<p align="center">
  <img src="web/icon-192.png" alt="Ulak icon" width="88">
</p>

<h1 align="center">Ulak</h1>

<p align="center">
  <strong>A courier for your coding agents, not a key to your machine.</strong><br>
  The yes goes to the question you read.
</p>

<p align="center"><a href="https://github.com/mtbagatur/ulak/actions/workflows/test.yml"><img src="https://github.com/mtbagatur/ulak/actions/workflows/test.yml/badge.svg" alt="Tests"></a></p>

<p align="center"><a href="https://ulak.tahabagatur.com/"><strong>ulak.tahabagatur.com</strong></a> · <a href="https://ulak.tahabagatur.com/demo/">Try the live demo in your browser</a> (nothing to install, no real machine behind it)</p>

![Ulak demo: an approval, a push that needs two seals, and an answer refused because the question changed](docs/demo.gif)

## The problem

An agent stops and asks: *run this?* You are not at your desk. The usual answer is to put a terminal on your phone, which also puts your whole machine on your phone, and a tap on a small screen can land on a question that changed a second ago.

Ulak carries one thing between you and your agents: the answer. It is a small web app (PWA) served from your own computer that shows your [Herdr](https://herdr.dev) panes (Claude Code, Codex, Antigravity) as cards and conversations. It has no terminal, and a pane with no agent in it can be read but not written to. It is reachable only through your own [Tailscale](https://tailscale.com) network: no account, no relay server, no app store, no build step, no telemetry.

## What it does differently

<p align="center">
  <img src="docs/screenshots/en/refused.png" alt="An approval refused because the question changed" width="195">
  <img src="docs/screenshots/en/second-seal.png" alt="A git push asking for a second confirmation with the full command" width="195">
  <img src="docs/screenshots/en/delivered.png" alt="The line above the composer: approved, on its way, agent received it" width="195">
  <img src="docs/screenshots/en/decisions.png" alt="The decision log: who answered what, and when the agent got it" width="195">
</p>

- **Commands that leave a mark need two seals.** `rm`, `git push`, `ssh`, `terraform apply`, `kubectl delete`, cloud CLIs and your own patterns show the full command and need a second confirmation that expires in two minutes. A Claude command whose beginning has left the screen can only be denied; for Codex and agy Ulak cannot tell, as [SECURITY.md](SECURITY.md) explains.
- **The record is on your phone.** Each decision, each refused answer and each timeout is logged on your machine with who, what and when, and you read it where you answered: the ledger icon.
- **Your yes is bound to what you read.** Every approval carries a fingerprint of the prompt on your screen. If the agent is asking something else by the time your tap arrives, the server answers 409, shows you the new question, and approves nothing.
- **What you cannot see is written out.** Zero-width and direction-reversing characters in a command are named where they stand (`⟨U+200B⟩`). Such a command has no approve button in the list and cannot be allowed for the whole session.
- **You see the answer arrive.** A line above the composer follows it: sent, on its way, agent received it. If the prompt never leaves the agent's screen, Ulak says so instead of assuming.

None of this is a claim to be the only one. Other tools guard approvals in their own ways; the table below repeats only what their READMEs say.

## When to pick something else

Ulak is deliberately narrow. These projects solve the same problem with different trade-offs, and each is the better choice for some people.

| | Ulak | [HerdrChat](https://github.com/cobanov/herdrchat) | [ColliePWA](https://github.com/AltanS/collie) | [Ovis](https://github.com/devswha/ovis) |
|---|---|---|---|---|
| Form | PWA served from your machine | native iPhone and iPad app; Android experimental | PWA served from your machine | desktop app, iPhone and Android apps, browser |
| How the phone reaches it | Tailscale Serve only | SSH, usually through Tailscale | Tailscale Serve by default; other front doors documented | Wi-Fi, Tailscale, SSH, or the Ovis end-to-end encrypted relay |
| Account | none | none | none; phones are paired | optional |
| Agents | Claude Code, Codex, Antigravity | Claude Code, Codex, Grok, Antigravity, Pi, OMP | Claude Code, Codex, OpenCode | Claude Code, Codex and other agents |
| Works beside | Herdr | Herdr | Herdr; tmux, zellij, tuios, Tern and Rex experimental | Herdr, or its own terminals |
| Terminal on the phone | no; a read-only text view of the agent's screen | yes, a full emulator | types keys into the live pane | yes |
| Approvals | bound to the prompt you read; two steps for destructive commands; decision log read on the phone | approval choices; binding and a log are not described | the agent's prompts as buttons; binding is not described; an audit log is named in its durable-agent security notes | cards; binding and a log are not described |
| Usage ping | none | not mentioned | on by default, can be turned off | on by default, can be turned off |

Read from each project's README on 27 November 2026, not from their code: "not described" means the README does not say, not that the feature is missing. They move fast; check theirs before deciding.

- **Pick HerdrChat** when you want a native app, a real terminal, subagent and workflow transcripts, an iPad layout, or several computers in one list.
- **Pick ColliePWA** when you use tmux or zellij instead of Herdr, or want built-in voice input, themes and its durable-agent tools.
- **Pick Ovis** when your phone cannot join a tailnet, or you want one app across desktop, phone and browser.
- **Pick Ulak** when you want the phone to be able to answer and little else, every yes tied to the question you read, and code short enough to read before you trust it: about 5,000 lines of Python, JavaScript and CSS and four runtime dependencies.

What Ulak does not do: no terminal, one computer, one person, Herdr only. Conversations are fullest for Claude Code, where the transcript is read directly; other agents are read from the screen.

## What else it does

<p align="center">
  <img src="docs/screenshots/en/home.png" alt="Home: sessions grouped by what needs you, with usage limits on top" width="195">
  <img src="docs/screenshots/en/approval.png" alt="A session with an approval waiting under the conversation" width="195">
  <img src="docs/screenshots/en/reading.png" alt="A conversation: finished work folded into one line, a failed command left open, context and cache in the header" width="195">
  <img src="docs/screenshots/en/changes.png" alt="Read-only view of the uncommitted changes in a session's folder" width="195">
  <img src="docs/screenshots/en/light.png" alt="The light theme" width="195">
  <br>
  <sub>Demo data, retaken with <code>python tools/shots.py</code>. <a href="docs/screenshots/tr">Turkish screenshots</a>.</sub>
</p>

- **See what needs you.** Sessions are grouped as Needs you, Working, Just finished (for five minutes) and Idle. Pins live on the server, so every device shows the same list.
- **Read it like a chat.** Claude Code transcripts become messages with their headings, lists and tables, and code blocks you can copy with one tap. Subagents show as cards that say what they were asked and whether they are done.
- **Work that went well folds away.** In a Claude Code conversation a turn's reads, edits and commands collapse into one line you can open, edits with their diff. Other agents keep no record Ulak can read this way, so theirs is shown as the text on their screen. A command that failed and a request you refused never fold; they stay in sight.
- **Know how much room is left.** The header of a Claude conversation shows the context in use and how long the prompt cache still lives, both read from the transcript, and a ring for the tightest usage limit. Context and cache are Claude only; the Claude limit needs `limit_files`, Codex limits need no setup.
- **See the pictures.** An image the conversation points at shows as a thumbnail and opens full screen. It is served under the same rules as any file in the session's folder; a picture from the web is never loaded.
- **Answer questions and menus.** Single and multi-select questions, trust dialogs, and a small key bar (arrows, Esc, Enter, digits) when a prompt needs it.
- **The agent's own slash commands.** Type `/` for the commands and skills that exist on that machine, with a built-in list as fallback.
- **Look without touching.** Agent screen shows the pane as text, Changes shows `git status` and the diff since the last commit. Both are read-only.
- **Attach files.** A photo, screenshot, PDF or text file (15 MB max, kept a week); the agent gets the path. A photo loses where and with what it was taken on the way: a JPEG is cleaned on the server without touching the picture, an iPhone HEIC is redrawn on the phone. If one could not be cleaned, Ulak says so.
- **Push notifications.** Standard Web Push with your own VAPID keys. Nothing buzzes while Ulak is on screen, or for turns shorter than two minutes: a question from another session shows as a line above the one you are reading, and if you lock the phone within a minute the notification follows. Once a minute Ulak also asks Herdr what is really waiting, so one lost event does not lose a question. Settings show when a notification last reached this device and say so when they stopped; a test button sends one on demand.
- **Usage limits.** A strip shows the tightest window per provider, read from files your own scripts write.
- **Start and close sessions** from the phone (closing takes two steps).
- **English and Turkish**, light and dark; both follow the phone.
- **Dictation** works through your keyboard's microphone. Ulak adds no voice button of its own, so no audio passes through it.

## Try it in one minute

No Herdr needed, no setup:

```sh
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
python tools/demo_server.py      # http://127.0.0.1:8766
```

The demo is the real app over a scripted, fake machine that lives in the browser tab (`tools/demo/demo.js`); nothing leaves the tab. Three scenes are worth trying in order:

1. **panel**: approve the test run. Ulak moves you to the next session that waits.
2. **tugra**: a `git push` needs two seals. After the second, the line above the composer follows your answer until the agent has it, and the agent resumes.
3. **sadrazam**: a little later a third agent stops. Approve it: the question changed while you were reading, so Ulak refuses your tap, shows the new question, and puts the refusal on the record (ledger icon, Decisions).

`?speed=4` hurries the agents. `python tools/demo_server.py --build DIR` writes the same demo as static files for any web host.

## Security

The server binds to `127.0.0.1` and trusts one thing: the `Tailscale-User-Login` header that Tailscale Serve sets, compared with `ULAK_ALLOWED_LOGIN` on every API request except the one that hands out the public push key. A request that changes something is also refused when it comes from another web page, and only Ulak's own host names are answered. What that protects, what the fingerprint, the two seals and the log add, and what none of it protects against is in [SECURITY.md](SECURITY.md). Read it before you run Ulak.

## Install

One command (venv, dependencies, push keys, systemd user service):

```sh
ULAK_ALLOWED_LOGIN=you@github ULAK_VAPID_SUBJECT=mailto:you@example.com ./install.sh
tailscale serve --bg 8765
```

The unit it installs is `deploy/ulak.service.example`. To update, run `git pull`, then the same command again: it rewrites the unit and restarts the service. A user service stops when you log out of the computer; `loginctl enable-linger` keeps it running.

Without systemd, run it by hand:

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
  "allowed_hosts": [],
  "min_work_seconds": 120,
  "approval_timeout_minutes": 0
}
```

- `rules_file`: extra destructive regexes, one per line. Blank lines and lines starting with `#` are ignored. Also accepted: markdown list ``- pattern: `regex` ``.
- `quick_targets` / `quick_label`: workspace or project names that get the quick-send button.
- `limit_files`: usage-limit files shown in the limits strip. Two formats are understood: JSON `{"h5": {"used_percentage": 4, "resets_at": 1795200600}, "d7": {...}, "ts": 1795183857}` (epoch seconds; a key such as `d7_fable` or `h5_<model>` adds a window kept for one model, shown on its own line), or tab-separated text `name<TAB>window<TAB>NN%<TAB>ISO-reset` where NN% is what is *remaining*.
- `project_roots`: folders whose subfolders appear in the new-session picker (default `["~"]`). New sessions can only start inside your home folder.
- `allowed_hosts`: extra host names Ulak answers to. `localhost`, `127.0.0.1`, names without a dot and `*.ts.net` already work; any other name gets 403.
- `min_work_seconds`: a "finished" push is skipped when the turn took less than this (default 120).
- `approval_timeout_minutes`: off by default (`0`), so an approval waits for you however long that takes. Set it and Ulak denies an approval nobody answered in that many minutes. It can only deny, only the exact prompt it has been timing, and each one is written to the decision log as a timeout. The clock restarts when Ulak restarts.

State (`.state/`: pins, notification history, the decision log) lives on the server, so every device sees the same view. The browser keeps only unsent drafts, a copy of your pins, the chosen language and theme, and whether the idle group is open.

## Architecture

- `app.py` serves a FastAPI backend on `127.0.0.1:8765` and a static PWA.
- `web/md.js` turns a message into headings, lists, tables and code blocks. Text is escaped first and tags come only from the renderer, so nothing in a transcript runs.
- `herdr_client.py` speaks newline-delimited JSON over `~/.config/herdr/herdr.sock` (`HERDR_SOCKET_PATH` points it elsewhere).
- Startup subscribes to the current pane set and adds/removes pane subscriptions as lifecycle events arrive.
- Herdr events flow through the backend to browser clients as Server-Sent Events; the PWA uses REST for reads and actions.
- Push notifications use local VAPID keys and go directly to the browser's Web Push endpoint. The [encrypted payload](https://www.rfc-editor.org/rfc/rfc8291) includes the project label, a short command or output preview, and the pane link. The device can display the preview on its lock screen.

## API

- `GET /api/sessions`: enriched cards, ordered blocked > working > done (five minutes) > unknown > idle.
- `GET /api/sessions/{pane}/chat`: cleaned screen split into blocks (Claude JSONL when available; `?from=` keeps older messages in view), with `meters` (context and cache, when the transcript has them) and the server's clock as `now`. `GET .../history?before=` loads older ones, and `GET .../conversations` lists the earlier Claude conversations of the session's own folder (read-only; the phone names an id, never a path).
- `GET /api/sessions/{pane}/search?q=`: where a word or phrase (2 to 80 characters) was said by you or the agent in the open conversation and the folder's earlier ones, newest first, at most 50. Tool output and subagent lines are not searched; records are read a piece at a time within a two-second budget (`partial` says older lines were not reached), and a new search stops the one before.
- Read-only views: `GET /api/sessions/{pane}/screen` (the pane as text), `.../changes` (`git status` and a size-capped diff of the session's own folder; the phone sends no path), `.../commands` (names and one-line descriptions of the slash commands on the machine).
- `POST /api/sessions/{pane}/prompt`: send text (409 for a pane with no agent in it, as for `.../keys` and `/api/quick`). `POST /api/sessions/{pane}/upload`: raw file body (image, PDF or text), returns its server path. `GET /api/sessions/{pane}/file`: open a supported document inside that workspace. `POST /api/quick`: quick send to a configured pane (see Configuration). `GET/PUT /api/pins`: server-side pins, shared across devices.
- `POST /api/sessions/{pane}/approve`: approve/always/deny, bound to the screen fingerprint (409 if it changed; destructive = two steps).
- `POST /api/sessions/{pane}/choose`: pick a menu option (single/multi, trust dialog). `POST .../keys`: raw keys (Enter/approve keys refused on destructive prompts).
- `POST /api/sessions/{pane}/close`: two-step close. `GET /api/projects`, `POST /api/sessions`: start a new session.
- `GET /api/audit`: the decision log. `GET /api/events`: SSE (`ready`, `session`, `removed`, `receipt`). `POST /api/presence`: report whether a browser tab is visible. `GET /api/limits`: `{providers: [{id, label, updated, items: [{label, used, resets_at}]}]}`. `/api/push/*`: public key, subscribe, known (with the last delivery and the last refusal), lang, test (one notification to the asking device). `GET /api/config`: the quick-send label and the approval timeout. `GET /api/notifications`: the history of push notifications.

## Tests

```sh
pip install -r requirements.txt -r requirements-dev.txt
python -m pytest -q
```

Browser cases use Chrome or Chromium with a loopback-only fake API. They skip when a browser is unavailable.

## License

MIT
