# Security

Ulak lets a phone answer the coding agents on your computer. This page says what it checks, what those checks are worth, and where they stop.

## The model in one paragraph

Ulak listens on `127.0.0.1:8765` and nowhere else. The phone reaches it through [Tailscale Serve](https://tailscale.com/kb/1312/serve), which terminates TLS inside your tailnet and adds a `Tailscale-User-Login` header that a client cannot set through the proxy. Every API request must carry that header with exactly the login in `ULAK_ALLOWED_LOGIN`; anything else gets 403. The one exception is `/api/push/public`, which returns the public half of the push key, like the app shell itself. There is no account, no relay, no telemetry, and the only outbound traffic is Web Push to the push service your browser chose.

## What each check does

**Only Ulak's own page may act.** The identity header says whose device sent a request, not which page asked the browser to send it. So a request that changes anything is refused when the browser says it comes from another site (`Sec-Fetch-Site`, or `Origin` in a browser without it). And Ulak answers only to its own names (`localhost`, `127.0.0.1`, `*.ts.net`, names without a dot, and `allowed_hosts` from your config), so a web page cannot point a name of its own at `127.0.0.1` and write the header itself.

**The fingerprint: your yes goes to the question you read.** When Ulak shows an approval it also sends a hash of that prompt. The phone returns the hash with the decision. Before any key is sent, the server reads the agent's screen again and compares. If the prompt changed (the agent moved on, someone answered at the keyboard, a second question replaced the first) the request is refused with 409 and nothing is typed. One answer at a time is allowed per pane, so two taps cannot race each other.

**Two seals: destructive commands take two requests.** A command is treated as destructive when it matches the built-in list (among them `rm`, `git push`, `gh`, `ssh`, `scp`, remote `rsync`, `curl -X POST/PUT/PATCH/DELETE`, `terraform apply/destroy`, `kubectl delete/apply`, `aws`) or a pattern in your own `rules_file`. The first request returns the full command and a one-time token; only a second request with that token, for the same prompt, within two minutes, sends the key. For these commands:

- "always allow" is refused;
- the key bar lets through only keys that cannot say yes: Esc, the arrows, Tab, `n` and the number of the prompt's "No".

**A command that is not all there can only be denied.** Ulak reads a Claude request from the rule drawn above it down to the question. When that rule has left the screen, the beginning of the command cannot be read, so approving is refused, on the card and on the key bar, whether or not the visible part looks destructive. For Codex and agy, which draw no such rule, Ulak reads the 24 lines above the question and cannot tell whether a longer command began earlier.

**The receipt: sent is not the same as arrived.** After a decision Ulak watches the agent's screen for a few seconds. When the answered prompt is gone it records the time and shows "agent received it". If the prompt is still there it says "not seen on the agent's screen" rather than reporting success.

**The decision log: what was answered, by whom, when.** Every decision is appended to `.state/audit.json` (mode 600, last 500 entries): the pane, the command, the decision, the login, the outcome and the delivery time. Refusals are logged with their reason, and so are timeouts. A choice from an agent's menu and keys typed at a waiting agent are decisions too and are logged the same way; text you write to an agent is not. The log is on your machine and is shown in the app under the ledger icon.

**The timeout: off unless you turn it on.** With `approval_timeout_minutes` set, an approval nobody answered in that time is denied. The timeout can only deny, only the exact prompt it has been timing, and it writes a log entry. It never approves.

**Read-only views stay read-only.** The agent screen, the changes view and the slash-command list are separate endpoints that send no keys. The changes view runs `git status` and `git diff` in the session's own folder with external diff tools, text conversion and filesystem monitors disabled; the phone cannot name a path. The file endpoint serves a short list of document types, only from the session's folder or from Ulak's own upload folder.

## What Ulak does not protect against

- **Someone who is you.** A person holding your unlocked phone, or logged in to your tailnet as you, has everything Ulak offers. There is no second factor inside Ulak.
- **Programs on the computer itself.** The header check assumes requests arrive through Tailscale Serve. A process already running on the same machine can call `127.0.0.1:8765` and write the header itself. Ulak adds no barrier there; anything running as you could type into your panes without it.
- **Any exposure other than Tailscale Serve.** Do not use `tailscale funnel`, a reverse proxy, or a public bind. Behind anything that forwards a client's own `Tailscale-User-Login` header, the check is worth nothing.
- **What you write.** A message is typed into the pane it is addressed to. To an agent that is an instruction, and what the agent then does is governed by the agent's own permission mode, not by Ulak. An agent started with permission prompts switched off never asks, so there is nothing for Ulak to guard. A pane with no agent in it is a plain shell: Ulak shows its screen and refuses to send it a message or a key. That check rests on Herdr's own detection of what runs in the pane.
- **Prompts it does not recognise.** The two seals apply to approval prompts Ulak can parse. A prompt it does not recognise as an approval gets no approve button. If it has numbered options they are shown as a menu, where one tap picks one, and the key bar (arrows, Esc, Enter, Tab, Space, `y`, `n`, digits) works on that pane. Neither is checked against the destructive list.
- **Commands that hide what they do.** The destructive list is pattern matching on the text of the command. `./deploy.sh` or `make release` will not match unless you add a pattern. It is a speed bump for the obvious cases, not a sandbox.
- **A command that lies.** The fingerprint proves the prompt did not change between your reading and your answer. It says nothing about whether the command does what it appears to.
- **A tampered log.** The decision log is a plain file owned by your user. It is a record for you, not evidence against someone with access to your account.
- **Eyes on your phone.** Conversations, the screen view and diffs can contain source code and secrets, and they are shown on the phone. Push notifications carry the project name and a short preview of the command or reply, which the phone may show on its lock screen.

## What is stored, and where

Everything stays on the computer that runs Ulak, in two untracked folders next to the code:

- `.secrets/`: the Web Push private key, push subscriptions, `config.json`.
- `.state/`: pins, notification history, the decision log, uploads (deleted after a week).

The browser keeps unsent drafts, the chosen language and whether the idle group is open, nothing else.

## Reporting a problem

Please report vulnerabilities privately through the repository's **Security → Report a vulnerability** page rather than in a public issue. Say what you did, what you expected and what happened; a failing test is the most useful form.
