# Security

Ulak lets a phone answer the coding agents on your computer. This page says what it checks, what those checks are worth, and where they stop.

## The model in one paragraph

Ulak listens on `127.0.0.1:8765` and nowhere else. The phone reaches it through [Tailscale Serve](https://tailscale.com/kb/1312/serve), which terminates TLS inside your tailnet and adds a `Tailscale-User-Login` header that a client cannot set through the proxy. Every API request must carry that header with exactly the login in `ULAK_ALLOWED_LOGIN`; anything else gets 403. The one exception is `/api/push/public`, which returns the public half of the push key, like the app shell itself. There is no account, no relay and no telemetry. The only thing Ulak itself sends out is Web Push, to the push service your browser chose. The programs it starts are another matter: the agents, and `agy models`, which it runs to list agy's models, talk to their own services.

## What each check does

**Only Ulak's own page may act.** The identity header says whose device sent a request, not which page asked the browser to send it. So a request that changes anything is refused when the browser says it comes from another site (`Sec-Fetch-Site`, or `Origin` in a browser without it). And Ulak answers only to its own names (`localhost`, `127.0.0.1`, `*.ts.net`, names without a dot, and `allowed_hosts` from your config), so a web page cannot point a name of its own at `127.0.0.1` and write the header itself.

**The fingerprint: your yes goes to the question you read.** When Ulak shows an approval it also sends a hash of that prompt. The phone returns the hash with the decision. Before any key is sent, the server reads the agent's screen again and compares. If the prompt changed (the agent moved on, someone answered at the keyboard, a second question replaced the first) the request is refused with 409 and nothing is typed. One answer at a time is allowed per pane, so two taps cannot race each other.

**Two seals: destructive commands take two requests.** A command is treated as destructive when it matches the built-in list (among them `rm`, `git push`, `gh`, `ssh`, `scp`, remote `rsync`, `curl -X POST/PUT/PATCH/DELETE`, `terraform apply/destroy`, `kubectl delete/apply`, `aws`) or a pattern in your own `rules_file`. The first request returns the full command and a one-time token; only a second request with that token, for the same prompt, within two minutes, sends the key. For these commands:

- "always allow" is refused;
- the key bar lets through only keys that cannot say yes: Esc, the arrows, Tab, `n` and the number of the prompt's "No".

**A command that is not all there can only be denied.** Ulak reads a Claude request from the rule drawn above it down to the question. When that rule has left the screen, the beginning of the command cannot be read, so approving is refused, on the card and on the key bar, whether or not the visible part looks destructive. For Codex and agy, which draw no such rule, Ulak reads the 24 lines above the question and cannot tell whether a longer command began earlier.

**Characters you cannot see are written out.** A command can carry characters that take no room on a screen: zero-width ones, ones that reverse the direction of the text, control codes. With them a command can read as one thing and run as another. Ulak names every such character where it stands (`⟨U+200B⟩`) and says so on the card. For a command that has any, the list shows no approve button, so it is answered only after it has been opened and read, and "always allow" is refused by the server. The destructive list is matched against the command without them, so `rm` split by a zero-width space is still `rm`. Letters from another alphabet that look like Latin ones are ordinary letters and are not marked.

**The receipt: sent is not the same as arrived.** After a decision Ulak watches the agent's screen for a few seconds. When the answered prompt is gone it records the time and shows "agent received it". If the prompt is still there it says "not seen on the agent's screen" rather than reporting success.

**The decision log: what was answered, by whom, when.** Every decision is appended to `.state/audit.json` (mode 600, last 500 entries): the pane, the command, the decision, the login, the outcome and the delivery time. A refused answer is logged with its reason (a changed screen, an agent that is no longer waiting, a second tap while the first is still going), and so are timeouts. Requests turned away before they reach an agent (wrong identity, another web page, a closed session) are not. A choice from an agent's menu and keys typed at a waiting agent are decisions too and are logged the same way; text you write to an agent is not. The log is on your machine and is shown in the app under the ledger icon.

**The timeout: off unless you turn it on.** With `approval_timeout_minutes` set, an approval nobody answered in that time is denied. The timeout can only deny, only the exact prompt it has been timing, and it writes a log entry. It never approves.

**Read-only views stay read-only.** The agent screen, the changes view and the slash-command list are separate endpoints that send no keys. The changes view runs `git status` and `git diff` in the session's own folder with external diff tools, text conversion, filesystem monitors and the filters in the repository's own configuration disabled, and without looking inside submodules; filters from your global git configuration still run. The phone cannot name a path. The file endpoint serves a short list of document types, only from the session's folder or from Ulak's own upload folder. Hidden files and folders inside the session's folder (names starting with a dot, where tools keep their keys) are not served, also when a link points at them. A session in your home folder can still serve any visible document of those types under it.

**The page runs only its own code.** The app is sent with a Content Security Policy: scripts, styles, connections and workers from Ulak's own address only, no plugins, no forms posting elsewhere, no framing by another page. What an agent writes is escaped before it is drawn; the policy is the second wall behind that, so text that did slip through as markup could not load a script or call another server. Inline styles are allowed, because the app sets a few itself.

**A photo leaves its location behind.** A JPEG you attach is rewritten on the computer before the agent sees the file: where and when it was taken, the camera, comments and thumbnails are dropped, and so is anything stored after the end of the picture. The picture data itself is not touched and the rotation is kept. An iPhone HEIC photo is redrawn as a JPEG in the browser, which leaves those details behind as well. When neither could be done (a JPEG Ulak cannot walk through, a HEIC the browser cannot draw) the file is still attached and the app says its details may still be inside. Other file types are passed on as they are.

**Push goes only to a push service.** A subscription is the address the computer will post to for every notification, so it is checked when the phone registers it: `https` only, a public host name or a public IP address, no `.local`, `.internal` or `.localhost`, and at most 20 subscriptions are kept. A name that is public but points at a private address is not caught; only your own signed-in device can register one. Apple rejects topics that are not plain short text, so the topic sent with a notification is a hash of the pane's tag, not the tag.

## What Ulak does not protect against

- **Someone who is you.** A person holding your unlocked phone, or logged in to your tailnet as you, has everything Ulak offers. There is no second factor inside Ulak.
- **Programs on the computer itself.** The header check assumes requests arrive through Tailscale Serve. A process already running on the same machine can call `127.0.0.1:8765` and write the header itself. Ulak adds no barrier there; anything running as you could type into your panes without it.
- **Any exposure other than Tailscale Serve.** Do not use `tailscale funnel`, a reverse proxy, or a public bind. Behind anything that forwards a client's own `Tailscale-User-Login` header, the check is worth nothing.
- **What you write.** A message is typed into the pane it is addressed to. To an agent that is an instruction, and what the agent then does is governed by the agent's own permission mode, not by Ulak. An agent started with permission prompts switched off never asks, so there is nothing for Ulak to guard. A pane with no agent in it is a plain shell: Ulak shows its screen and refuses to send it a message or a key. That check rests on Herdr's own detection of what runs in the pane.
- **Prompts it does not recognise.** The two seals apply to approval prompts Ulak can parse. A prompt it does not recognise as an approval gets no approve button. If it has numbered options they are shown as a menu, where one tap picks one, and the key bar (arrows, Esc, Enter, Tab, Space, `y`, `n`, digits) works on that pane. Neither is checked against the destructive list.
- **Commands that hide what they do.** The destructive list is pattern matching on the text of the command. `./deploy.sh` or `make release` will not match unless you add a pattern. It is a speed bump for the obvious cases, not a sandbox.
- **A command that lies.** The fingerprint proves the prompt did not change between your reading and your answer. It says nothing about whether the command does what it appears to.
- **A tampered log.** The decision log is a plain file owned by your user. It is a record for you, not evidence against someone with access to your account.
- **Eyes on your phone.** Conversations, the screen view and diffs can contain source code and secrets, and they are shown on the phone. So are the diff of each edit an agent made and the pictures a conversation points at. A picture is served under the same rules as the file endpoint above (the session's folder or Ulak's upload folder, no hidden files); one that lives on the web is not loaded. Push notifications carry the project name and a short preview of the command or reply, which the phone may show on its lock screen. They are encrypted for the phone, so the push service in between sees when and how often, not what.

## What is stored, and where

Everything stays on the computer that runs Ulak, in two untracked folders next to the code:

- `.secrets/`: the Web Push private key, push subscriptions (each with the time a notification last reached it or last failed), `config.json`.
- `.state/`: pins, notification history, the decision log, uploads (removed once older than a week, at the next upload or restart).

The browser keeps unsent drafts, the messages you sent until the conversation shows them, your pins, the chosen language and theme, and whether the idle group is open, nothing else.

## Reporting a problem

Please report vulnerabilities privately through the repository's **Security → Report a vulnerability** page rather than in a public issue. Say what you did, what you expected and what happened; a failing test is the most useful form.
