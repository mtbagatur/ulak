// Ulak demo: the real app over a scripted, fake machine. Everything below runs in this tab; nothing is sent anywhere.
//   ?speed=4  hurries the fake agents (tests, recordings)      ?worst  loads worst-case data (long names, huge command)
(() => {
  const query = new URLSearchParams(location.search);
  const speed = Math.max(1, +query.get('speed') || 1), worst = query.has('worst');
  const now = () => Date.now() / 1000;
  const later = (seconds, fn) => setTimeout(fn, seconds * 1000 / speed);
  const CHANGED = 'The approval screen changed; refresh';
  const user = text => ({role: 'user', text}), bot = text => ({role: 'assistant', text});
  const tool = (name, text, result, agent) => ({role: 'tool', tool: name, text, ...(result ? {result} : {}), ...(agent ? {agent} : {})});
  const ask = (details, fingerprint, destructive, always) => ({kind: 'approval', question: 'Do you want to proceed?', destructive: !!destructive, cut: false,
    details, fingerprint, roles: always ? ['approve', 'always', 'deny'] : ['approve', 'deny']});
  const card = (pane, agent, status, project, ws, title, preview, ago, model, pending, hint) => ({pane_id: pane, agent, status, since: now() - ago, project, title,
    workspace_label: ws, workspace_id: ws, cwd: `~/projects/${project}`, name: '', model: model || '', preview, pending: pending || null, quick: false,
    transcript: agent === 'claude', hint: hint || ''});

  let sessions = [
    card('d1:p1', 'claude', 'blocked', 'billing-api', 'work', 'Fix flaky invoice test', 'I found the race in the retry helper. Run the billing tests to confirm?', 90, 'Sonnet',
      ask('pytest -q tests/test_billing.py', 'demo-a', false, true)),
    card('d2:p1', 'claude', 'blocked', 'web-app', 'work', 'Ship 2.4', 'Release notes are ready. Push the release branch?', 240, 'Opus', ask('git push origin release/2.4', 'demo-b', true)),
    card('d3:p1', 'codex', 'working', 'infra', 'ops', 'Terraform plan cleanup', 'Reading modules/network to find unused subnets…', 30, 'gpt', null, 'Reading modules/network…'),
    card('d4:p1', 'claude', 'working', 'docs', 'work', 'Rewrite the quickstart', 'Editing docs/quickstart.md', 15, 'Haiku', null, 'Editing docs/quickstart.md…'),
    card('d5:p1', 'agy', 'done', 'data-pipeline', 'ops', 'Backfill job', 'Backfill finished: 1.2M rows, 0 errors.', 120, ''),
    card('d6:p1', 'claude', 'idle', 'notes', 'personal', '', '', 7200, 'Sonnet'),
  ];
  const chats = {
    'd1:p1': [
      user('The invoice test fails about one run in ten. Find out why.'),
      tool('Grep', 'retry_with_backoff in src/billing', 'src/billing/retry.py:41: def retry_with_backoff(fn, tries=3):'),
      tool('Agent', 'Find every caller of retry_with_backoff', '4 callers: invoices.py, refunds.py, payouts.py, webhooks.py. Only invoices.py retries a write.', {kind: 'Explore', state: 'done'}),
      tool('Agent', 'Run the billing suite 20 times and count the failures', '', {kind: 'general-purpose', state: 'running'}),
      bot('Found it. `retry_with_backoff` sleeps with a **fixed** delay, so two workers retry in lockstep and collide on the same invoice row.'),
      tool('Edit', 'src/billing/retry.py', '+    delay = base * 2 ** attempt + random.uniform(0, base)'),
      bot('I added jitter to the backoff. Let me run the billing tests to confirm.'),
    ],
    'd2:p1': [
      user('Cut the 2.4 release branch and write the notes. Ask me before anything leaves this machine.'),
      tool('Bash', 'git log --oneline v2.3.0..HEAD', '31 commits: 4 features, 19 fixes, 8 chores'),
      tool('Write', 'docs/releases/2.4.md', '+ ## 2.4\n+ - Saved filters on the orders page\n+ - Faster first load (-38% JS)\n+ - 19 fixes'),
      bot('The notes are in `docs/releases/2.4.md` and the branch `release/2.4` exists locally. Pushing it starts the release pipeline.'),
    ],
    'd3:p1': [
      user('Find subnets nothing uses any more and plan their removal. Do not apply anything without asking.'),
      tool('Bash', 'rg -l aws_subnet modules/', 'modules/network/main.tf\nmodules/network/legacy.tf'),
      bot('Three subnets in `legacy.tf` have no instances, no load balancers and no route targets. I am checking the peering config before I plan.'),
    ],
    'd4:p1': [
      user('The quickstart is too long. Get a new user to a first request in five minutes.'),
      tool('Read', 'docs/quickstart.md', '412 lines'),
      bot('Most of the page is configuration reference. I am moving that to its own page and keeping three steps: install, key, first request.'),
      tool('Edit', 'docs/quickstart.md', '- ## Advanced configuration\n+ See [Configuration](configuration.md) for every option.'),
    ],
    'd5:p1': [
      user('Backfill the events table from the March export.'),
      tool('Bash', 'python jobs/backfill.py --from 2026-03-01 --to 2026-03-31', 'batches: 240/240\nrows: 1,204,118\nerrors: 0'),
      bot('Backfill finished: 1.2M rows, 0 errors. Row counts match the export manifest.'),
    ],
  };
  // What each agent does once its question is answered.
  const SCRIPT = {
    'demo-a': {hint: 'Running the billing tests…', seconds: 5, no: 'Okay, I did not run the tests. The change is still uncommitted.',
      yes: [tool('Bash', 'pytest -q tests/test_billing.py', '12 passed in 3.41s'), bot('All 12 billing tests pass, and 20 runs in a row came back clean. The fix is the jitter in `retry_with_backoff`.')]},
    'demo-b': {hint: 'Pushing release/2.4…', seconds: 4, no: 'Okay, nothing was pushed. The notes are in `docs/releases/2.4.md` if you want to read them first.',
      yes: [tool('Bash', 'git push origin release/2.4', ' * [new branch]      release/2.4 -> release/2.4'), bot('Pushed. CI is running on `release/2.4`; I will tag 2.4.0 when it is green.')]},
    'demo-d': {hint: 'Applying the plan…', seconds: 5, no: 'Okay, nothing was applied. The plan is saved as `tf.plan`.',
      yes: [tool('Bash', 'terraform apply tf.plan', 'Apply complete! Resources: 0 added, 2 changed, 3 destroyed.'), bot('Applied. The three unused subnets are gone and both route tables are updated.')]},
  };
  const audit = [];
  const notes = [{ts: now() - 240, pane: 'd2:p1', status: 'blocked', title: 'web-app', body: 'Approval needed: git push origin release/2.4'},
    {ts: now() - 90, pane: 'd1:p1', status: 'blocked', title: 'billing-api', body: 'Approval needed: pytest -q tests/test_billing.py'}];
  let pins = ['work|billing-api'], made = 0, infraAsked = false;

  if (worst) {  // break-ui: long names, unbreakable strings, a huge command, RTL/CJK/emoji
    const long = 'x'.repeat(200), big = Array.from({length: 60}, (_, i) => `find / -name '*.log' -exec rm -f {} \; # step ${i} ${'a'.repeat(60)}`).join('\n');
    sessions = [
      card('d1:p1', 'claude', 'blocked', 'a-very-long-project-name-that-keeps-going-and-going-and-going-forever', 'workspace-with-a-long-label', long,
        `Unbreakable: ${long} 日本語の長い文章がここに入ります 🙂🙂🙂 مرحبا بالعالم`, 90, 'Sonnet-4-5-with-an-absurdly-long-model-name',
        {...ask(big, 'demo-a', false, true), question: 'Do you want to proceed? '.repeat(12)}),
      card('w2:p1', 'codex', 'working', 'a', '', '', '', 5, ''),
      card('w3:p1', 'agy', 'done', '同じ名前', 'ws', 't'.repeat(90), 'y'.repeat(400), 99999999),
      card('w4:p1', 'claude', 'idle', 'same', 'w1', '', '', 7200, ''), card('w5:p1', 'claude', 'idle', 'same', 'w2', '', '', 7200, ''),
    ];
    const hard = [user('z'.repeat(300) + '\n\n\n\n\nend'), bot('```\n' + 'long_line_'.repeat(40) + '\n```\n' + long + '\nhttps://example.com/' + 'p'.repeat(200)), tool('Bash', big.slice(0, 400), big)];
    sessions.forEach(s => { chats[s.pane_id] = hard.slice(); });
  }

  // ---------- the fake machine ----------
  const streams = new Set();
  const emit = message => streams.forEach(s => s.ready && s.onmessage?.({data: JSON.stringify(message)}));
  const find = pane => sessions.find(s => s.pane_id === pane);
  const view = ({hint, ...s}) => ({...s, bucket: s.status === 'done' && now() - s.since > 300 ? 'idle' : s.status});  // done is news for five minutes
  function set(s, patch) {
    const was = s.status;
    Object.assign(s, patch);
    if (s.status !== was) {
      s.since = now();
      if (s.status === 'blocked') notes.unshift({ts: now(), pane: s.pane_id, status: 'blocked', title: s.project, body: `Approval needed: ${s.pending?.details || ''}`});
      if (s.status === 'done') notes.unshift({ts: now(), pane: s.pane_id, status: 'done', title: s.project, body: s.preview});
    }
    emit({type: 'session', session: view(s)});
  }
  function finish(s, messages) {
    if (!find(s.pane_id)) return;
    chats[s.pane_id].push(...messages);
    set(s, {status: 'done', hint: '', preview: messages.at(-1).text});
  }
  function infraAsks() {
    const s = find('d3:p1');
    if (infraAsked || !s) return;
    infraAsked = true;
    chats[s.pane_id].push(bot('Nothing peers with them. I would like to write the plan.'));
    set(s, {status: 'blocked', hint: '', pending: ask('terraform plan -out=tf.plan', 'demo-c'), preview: 'Three unused subnets. Write the plan?'});
  }
  function approve(pane, action) {
    const s = find(pane), p = s?.pending;
    if (!p) return [409, {detail: CHANGED}];
    const record = (outcome, reason) => audit.unshift({id: `demo-${audit.length}`, ts: now(), pane, project: s.project, decision: action.decision, outcome, reason: reason || '',
      command: p.details, destructive: p.destructive, login: 'you@example.test', delivered: null});
    if (p.fingerprint === 'demo-c') {
      // The scripted race: someone at the keyboard answered while you were reading, and the agent already asks something else.
      // Your tap was for the old question, so Ulak refuses it instead of approving the new one.
      record('refused', CHANGED);
      chats[pane].push(tool('Bash', 'terraform plan -out=tf.plan', 'Plan: 0 to add, 2 to change, 3 to destroy.'), bot('The plan removes three subnets and updates two route tables. Apply it?'));
      set(s, {pending: ask('terraform apply tf.plan', 'demo-d', true), preview: 'The plan removes three subnets. Apply it?'});
      return [409, {detail: CHANGED}];
    }
    if (action.fingerprint !== p.fingerprint) { record('refused', CHANGED); return [409, {detail: CHANGED}]; }
    if (p.destructive && action.decision === 'approve' && action.confirm_token !== 'demo-second-seal') return [200, {confirm_required: true, confirm_token: 'demo-second-seal', command: p.details}];
    record('sent');
    const receipt = audit[0], yes = action.decision !== 'deny', script = SCRIPT[p.fingerprint] || {hint: 'Working…', seconds: 4, yes: [bot('Done.')], no: 'Okay, I did not run it.'};
    set(s, {pending: null, status: 'working', hint: yes ? script.hint : ''});
    later(1.2, () => { receipt.delivered = now(); emit({type: 'receipt', pane, delivered: true}); });
    later(yes ? script.seconds : 2.5, () => {
      if (yes) chats[pane].forEach(m => { if (m.agent?.state === 'running') { m.agent.state = 'done'; m.result = '20 runs, 0 failures.'; } });
      finish(s, yes ? script.yes : [bot(script.no)]);
    });
    if (!sessions.some(x => x.status === 'blocked')) later(8, infraAsks);  // the queue is empty: time to watch the answer arrive, then a third agent stops
    return [200, {ok: true, next: sessions.find(x => x !== s && x.status === 'blocked')?.pane_id || null}];
  }
  function chat(pane) {
    const s = find(pane);
    if (!s) return [404, {detail: 'Session not found'}];
    const p = s.pending, labels = {approve: 'Yes', always: "Yes, and don't ask again", deny: 'No'};
    return [200, {messages: chats[pane] || [], source: 'transcript', status: s.status, hint: s.hint, session: view(s),
      prompt: p && {...p, options: p.roles.map((role, i) => ({n: i + 1, label: labels[role], role}))},
      receipt: audit.find(a => a.pane === pane && a.outcome === 'sent') || null}];
  }
  function screen(pane) {
    const s = find(pane), command = s?.pending?.details;
    const last = (chats[pane] || []).filter(m => m.role === 'assistant').at(-1)?.text || '';
    return {ts: now(), text: [`● ${last}`, '', ...(command ? ['Bash command', '', `  ${command}`, '', 'Do you want to proceed?', '❯ 1. Yes', '  2. No'] : s?.status === 'working' ? ['✻ Working… (esc to interrupt)'] : ['❯ '])].join('\n')};
  }
  const DIFF = `diff --git a/src/billing/retry.py b/src/billing/retry.py
--- a/src/billing/retry.py
+++ b/src/billing/retry.py
@@ -41,7 +41,8 @@ def retry_with_backoff(fn, tries=3):
     """Call fn again after a failure, waiting longer each time."""
-    delay = base * 2 ** attempt
+    delay = base * 2 ** attempt + random.uniform(0, base)
+    log.debug("retry %s in %.2fs", attempt, delay)
 
 
 def retry(call, attempts=5):
diff --git a/tests/test_retry.py b/tests/test_retry.py
--- a/tests/test_retry.py
+++ b/tests/test_retry.py
@@ -20,3 +20,8 @@ def test_backoff_is_capped():
     assert backoff(20) <= 30.0
+
+
+def test_backoff_has_jitter():
+    waits = {backoff(3) for _ in range(50)}
+    assert len(waits) > 1 and all(2.0 <= w <= 4.0 for w in waits)
`;
  const changes = pane => ({repo: true, more_files: 0, truncated: false, ts: now(), ...(pane === 'd1:p1'
    ? {branch: 'retry-backoff', files: [{code: 'M', path: 'src/billing/retry.py'}, {code: 'M', path: 'tests/test_retry.py'}, {code: '?', path: 'docs/backoff.md'}], diff: DIFF}
    : {branch: 'main', files: [], diff: ''})});
  const COMMANDS = [...['/clear', '/compact', '/help', '/model'].map(name => ({name, description: '', source: 'builtin'})),
    {name: '/review', description: 'Review the current diff before it ships', source: 'project'},
    {name: '/release-notes', description: 'Draft release notes from merged changes', source: 'project'},
    {name: '/git:sync', description: 'Rebase on main and rerun the tests', source: 'user'}];
  const limits = () => ({now: now(), providers: [
    {id: 'claude', label: 'Claude', updated: now() - 120, items: [{label: '5h', used: 38, resets_at: now() + 2.4 * 3600}, {label: '7d', used: 71, resets_at: now() + 3.2 * 86400}]},
    {id: 'codex', label: 'Codex', updated: now() - 300, items: [{label: '5h', used: 12, resets_at: now() + 4.1 * 3600}, {label: '7d', used: 91, resets_at: now() + 1.5 * 86400}]}]});
  const PROJECTS = {projects: ['billing-api', 'web-app', 'docs'].map(name => ({name, path: `/home/you/projects/${name}`, display: `~/projects/${name}`})),
    tools: {claude: ['sonnet', 'opus'], codex: [], agy: []}};

  function route(method, path, body) {
    const [, pane, verb] = path.match(/^\/api\/sessions\/([^/]+)\/(\w+)$/) || [];
    const s = pane && find(pane);
    if (method === 'GET') {
      if (path === '/api/sessions') return sessions.map(view);
      if (verb === 'chat') return chat(pane);
      if (verb === 'commands') return {commands: COMMANDS};
      if (verb === 'changes') return changes(pane);
      if (verb === 'screen') return screen(pane);
      return {'/api/config': {quick_label: '', approval_timeout_minutes: 0}, '/api/pins': {pins}, '/api/audit': {items: audit}, '/api/notifications': {items: notes},
        '/api/limits': limits(), '/api/push/public': {key: ''}, '/api/projects': PROJECTS}[path] || [404, {detail: 'Not found'}];
    }
    if (path === '/api/pins') { pins = body.pins || []; return {pins}; }
    if (path === '/api/sessions') {
      const project = (body.cwd || 'project').split('/').filter(Boolean).pop(), fresh = card(`n${++made}:p1`, body.tool || 'claude', 'idle', project, 'work', '', '', 0, body.model);
      sessions.push(fresh); chats[fresh.pane_id] = [];
      later(0.3, () => emit({type: 'session', session: view(fresh)}));
      return {ok: true, pane: fresh.pane_id};
    }
    if (verb && !s) return [404, {detail: 'Session not found'}];
    if (verb === 'approve') return approve(pane, body);
    if (verb === 'prompt') {
      (chats[pane] ||= []).push(user(body.text || ''));
      set(s, {status: 'working', hint: 'Thinking…'});
      later(2.5, () => finish(s, [bot('This is a demo, so no agent is behind this session. On your own machine these words are typed into the agent\'s terminal, exactly as you wrote them.')]));
      return {ok: true};
    }
    if (verb === 'close') {
      if (body.confirm_token !== 'demo-close') return {confirm_required: true, confirm_token: 'demo-close', working: s.status === 'working', project: s.project};
      sessions = sessions.filter(x => x !== s);
      emit({type: 'removed', pane});
      return {ok: true};
    }
    if (verb === 'upload') return {ok: true, path: '~/.state/uploads/demo.png', bytes: 1};
    return {ok: true};
  }

  // ---------- what the app sees instead of a server ----------
  const realFetch = window.fetch.bind(window);
  window.fetch = async (input, options = {}) => {
    const url = new URL(typeof input === 'string' ? input : input.url, location.href);
    if (!url.pathname.startsWith('/api/')) return realFetch(input, options);
    let body = {};
    try { if (typeof options.body === 'string') body = JSON.parse(options.body); } catch {}
    await new Promise(resolve => setTimeout(resolve, 40));
    const result = route((options.method || 'GET').toUpperCase(), decodeURIComponent(url.pathname), body);
    const [status, data] = Array.isArray(result) && typeof result[0] === 'number' ? result : [200, result];
    return new Response(JSON.stringify(data), {status, headers: {'Content-Type': 'application/json'}});
  };
  window.EventSource = class {
    static CONNECTING = 0; static OPEN = 1; static CLOSED = 2;
    constructor() {
      this.readyState = 1; this.listeners = {}; streams.add(this);
      setTimeout(() => { this.ready = true; (this.listeners.ready || []).forEach(fn => fn({data: '{}'})); }, 30);
    }
    addEventListener(name, fn) { (this.listeners[name] ||= []).push(fn); }
    close() { this.readyState = 2; streams.delete(this); }
  };
  // No service worker and no push: a demo must not install itself or ask for notification permission.
  try { Object.defineProperty(navigator, 'serviceWorker', {value: {register: () => Promise.reject(new Error('demo')), addEventListener() {}}, configurable: true}); } catch {}
  addEventListener('DOMContentLoaded', () => {
    const tag = Object.assign(document.createElement('small'), {textContent: 'demo', title: 'Fake sessions. Nothing leaves this tab.', className: 'demo-tag'});
    tag.style.cssText = 'margin-left:10px;padding:2px 7px;border:1px solid currentColor;border-radius:999px;font:600 11px/1.4 system-ui,sans-serif;letter-spacing:.08em;text-transform:uppercase;opacity:.55;vertical-align:middle';
    document.querySelector('.top h1')?.append(tag);
  });
})();
