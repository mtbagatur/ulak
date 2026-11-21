'use strict';
// Ulak v2 front end. No build step. Re-render only what changed: cards are patched by key,
// chat messages are appended, the composer and its draft are never touched by updates.
const $ = s => document.querySelector(s);
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const statusText = s => t('st_' + (['blocked', 'working', 'done', 'idle'].includes(s) ? s : 'unknown'));
const TOOLNAME = {claude: 'Claude', codex: 'Codex', agy: 'agy', terminal: 'Terminal'};
const store = {get(k){try{return localStorage.getItem(k)}catch{return null}}, set(k,v){try{localStorage.setItem(k,v)}catch{}}};

const state = {sessions: new Map(), current: null, chatHash: '', rendered: 0, prompt: null, idleOpen: store.get('idleOpen') === '1', busy: new Set(), live: false};

// ---------- network ----------
async function api(url, body, method) {
  const opt = body === undefined ? {} : {method: method || 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)};
  let r;
  try { r = await fetch(url, opt); } catch { throw new Error(t('offline')); }
  if (!r.ok) {
    let msg = `${r.status}`;
    try { const j = await r.json(); msg = j.detail || msg; } catch {}
    throw new Error(errText(msg));
  }
  return r.json();
}
function toast(text, kind = '') {
  const el = document.createElement('div');
  el.className = `toast ${kind}`; el.textContent = text;
  $('#toasts').appendChild(el);
  if (kind === 'ok' && navigator.vibrate) navigator.vibrate(12);
  setTimeout(() => el.remove(), kind === 'err' ? 4200 : 2200);
}
async function act(btn, fn, okText) {
  // Button loading state + toast, errors never become unhandled rejections.
  if (btn) btn.classList.add('loading');
  try { const r = await fn(); if (okText) toast(okText, 'ok'); return r; }
  catch (e) { toast(e.message, 'err'); return null; }
  finally { if (btn) btn.classList.remove('loading'); }
}

// ---------- time ----------
function ago(ts) {
  if (!ts) return '';
  const s = Math.max(0, Date.now() / 1000 - ts);
  if (s < 45) return t('justNow');
  if (s < 3600) return `${Math.round(s / 60)} ${t('min')}`;
  if (s < 86400) return `${Math.floor(s / 3600)} ${t('hour')}`;
  return `${Math.floor(s / 86400)} ${t('day')}`;
}

// ---------- home ----------
const GROUPS = ['blocked', 'working', 'done', 'idle'];
const rank = s => ({blocked: 0, working: 1, done: 2, idle: 3}[s.bucket] ?? 3);
function sorted() {
  return [...state.sessions.values()].sort((a, b) => rank(a) - rank(b) || (b.since - a.since));
}
// Pins live on the server (.state/pins.json) so every device sees the same list.
const pins = new Set();
let pinsReady = false;
async function loadPins() {
  try {
    const {pins: remote} = await api('/api/pins');
    let local = []; try { local = JSON.parse(store.get('pins') || '[]'); } catch {}
    remote.forEach(k => pins.add(k));
    pinsReady = true;
    if (local.length && !remote.length) { local.forEach(k => pins.add(k)); await savePins(); }  // one-time migration from this phone
    store.set('pins', '[]');
    renderHome();
  } catch {}
}
async function savePins() {
  try { await api('/api/pins', {pins: [...pins]}, 'PUT'); } catch { toast(t('pinFail'), 'err'); }
}
const pinKey = s => `${s.workspace_label}|${s.project}`;
const isPinned = s => pins.has(pinKey(s));
function cardHtml(s) {
  const dup = [...state.sessions.values()].some(o => o !== s && o.project === s.project);
  const p = s.pending;
  // one-tap approve only when the whole command is visible in the card (the preview box is ~3 lines); long ones open the session to read first
  const shown = p ? (p.details || p.question || '') : '';
  const inline = p && p.kind === 'approval' && !p.destructive && !p.cut && p.roles.includes('approve') && shown.length <= 160 && shown.split('\n').length <= 3;
  const stateText = s.bucket === 'idle' && s.status === 'done' ? t('doneAgo', {t: ago(s.since)}) : `${statusText(s.status)}${s.status === 'unknown' ? '' : ' · ' + ago(s.since)}`;
  const body = p ? `<pre class="c-cmd">${esc((p.destructive ? '⚠ ' : '') + (p.details || p.question))}</pre>` : (s.preview ? `<div class="c-prev">${esc(s.preview)}</div>` : '');
  return `<div class="c-top"><span class="proj">${esc(s.project)}</span>${dup && s.workspace_label ? `<span class="ws">${esc(s.workspace_label)}</span>` : ''}<span class="chip ${esc(s.agent)}">${esc(TOOLNAME[s.agent] || s.agent)}</span><span class="model">${esc(s.model)}</span><button class="pin ${isPinned(s) ? 'on' : ''}" data-pin aria-label="${esc(t('pin'))}">📌</button></div>
    <div class="state"><span class="d"></span>${esc(stateText)}</div>
    ${s.title ? `<div class="c-title">${esc(s.title)}</div>` : ''}${body}
    ${inline ? `<div class="c-act"><button class="danger" data-act="deny">${esc(t('deny'))}</button><button class="warn" data-act="approve">${esc(t('approve'))}</button></div>` : ''}`;
}
function patch(container, items, keyOf, htmlOf, make) {
  const existing = new Map([...container.children].map(el => [el.dataset.key, el]));
  let prev = null;
  for (const it of items) {
    const key = keyOf(it);
    let el = existing.get(key);
    if (!el) { el = make(it); el.dataset.key = key; }
    existing.delete(key);
    const html = htmlOf(it);
    if (el._h !== html) { el.innerHTML = html; el._h = html; }
    if (el.dataset.cls !== (it.cls || '')) { el.className = `${el._base || ''} ${it.cls || ''}`.trim(); el.dataset.cls = it.cls || ''; }
    const want = prev ? prev.nextSibling : container.firstChild;
    if (want !== el) container.insertBefore(el, want);
    prev = el;
  }
  existing.forEach(el => el.remove());
}
function renderHome() {
  const all = sorted();
  const blocked = all.filter(s => s.bucket === 'blocked').length;
  $('#summary').innerHTML = !all.length ? '' : blocked ? t('summaryBlocked', {n: blocked}) : t('summaryCalm', {n: all.filter(s => s.bucket === 'working').length});
  if (navigator.setAppBadge) { blocked ? navigator.setAppBadge(blocked).catch(() => {}) : navigator.clearAppBadge?.().catch(() => {}); }
  const groups = $('#groups');
  if (!all.length) { groups.innerHTML = `<div class="empty">${t('empty')}</div>`; return; }
  if (groups.querySelector('.empty')) groups.innerHTML = '';
  const pinned = all.filter(isPinned);
  const defs = [...(pinned.length ? [{id: 'pinned', title: t('g_pinned'), items: pinned}] : []), ...GROUPS.map(id => ({id, title: t('g_' + id), items: all.filter(s => !isPinned(s) && (s.bucket === id || (id === 'idle' && !['blocked', 'working', 'done'].includes(s.bucket))))}))].filter(g => g.items.length);
  patch(groups, defs, g => g.id, g => `<h2 class="${g.id === 'idle' ? 'tap' : ''}" data-toggle="${g.id === 'idle' ? 1 : ''}"><span>${g.title} · ${g.items.length}</span>${g.id === 'idle' ? `<span>${state.idleOpen ? t('hide') : t('show')}</span>` : ''}</h2><div class="cards"></div>`, g => {
    const el = document.createElement('div'); el._base = 'group'; return el;
  });
  for (const g of defs) {
    const wrap = groups.querySelector(`[data-key="${g.id}"]`);
    wrap.className = `group g-${g.id}`;
    const box = wrap.querySelector('.cards');
    box.hidden = g.id === 'idle' && !state.idleOpen;
    patch(box, g.items, s => s.pane_id, cardHtml, s => { const b = document.createElement('div'); b._base = 'card'; b.setAttribute('role', 'button'); b.tabIndex = 0; return b; });
    for (const el of box.children) { const s = state.sessions.get(el.dataset.key); el.className = `card b-${s.bucket}`; }
  }
}
$('#groups').addEventListener('click', e => {
  if (e.target.closest('[data-toggle="1"]')) { state.idleOpen = !state.idleOpen; store.set('idleOpen', state.idleOpen ? '1' : '0'); renderHome(); return; }
  const card = e.target.closest('.card'); if (!card) return;
  const pb = e.target.closest('[data-pin]');
  if (pb) { e.stopPropagation(); const s = state.sessions.get(card.dataset.key); if (s) { const k = pinKey(s); pins.has(k) ? pins.delete(k) : pins.add(k); renderHome(); savePins(); } return; }
  const pane = card.dataset.key, a = e.target.closest('[data-act]');
  if (a) { e.stopPropagation(); inlineDecide(pane, a.dataset.act, a); return; }
  go(pane);
});
async function inlineDecide(pane, decision, btn) {
  const s = state.sessions.get(pane); if (!s || !s.pending) return;
  const r = await act(btn, () => api(`/api/sessions/${encodeURIComponent(pane)}/approve`, {decision, fingerprint: s.pending.fingerprint}), decision === 'approve' ? t('approved') : t('denied'));
  if (r) setTimeout(loadList, 700);
}

// ---------- data ----------
async function loadList() {
  try {
    const rows = await api('/api/sessions');
    state.sessions = new Map(rows.map(s => [s.pane_id, s]));
    renderHome();
    if (state.current) { if (!state.sessions.has(state.current)) closedBanner(); else updateSessionHeader(); }
    return true;
  } catch (e) { setConn(false, e.message); return false; }
}
let es = null, retry = 0;
function setConn(ok, msg) {
  state.live = ok;
  const c = $('#conn');
  if (ok) { c.hidden = true; return; }
  c.hidden = false; const bad = msg === t('offline'); c.className = 'conn' + (bad ? ' bad' : ''); c.textContent = bad ? t('offline') : t('reconnecting');
}
function connect() {
  if (es) es.close();
  es = new EventSource('/api/events');
  es.addEventListener('ready', () => { retry = 0; setConn(true); loadList(); loadPins(); if (state.current) loadChat(); });
  es.onmessage = ev => {
    const m = JSON.parse(ev.data);
    if (m.type === 'session') { state.sessions.set(m.session.pane_id, m.session); renderHome(); if (m.session.pane_id === state.current) { updateSessionHeader(); loadChat(); } }
    else if (m.type === 'removed') { state.sessions.delete(m.pane); renderHome(); if (m.pane === state.current) closedBanner(); }
  };
  es.onerror = () => { setConn(false, t('reconnecting')); es.close(); setTimeout(connect, Math.min(8000, 1000 * 2 ** retry++) + Math.random() * 400); };
}
document.addEventListener('visibilitychange', () => {
  if (document.hidden) return;
  // Phones kill sockets while asleep: probe, then reconnect if the stream is dead.
  Promise.race([loadList(), new Promise(r => setTimeout(() => r(false), 2500))]).then(ok => { if (!ok || !es || es.readyState !== 1) connect(); });
  if (state.current) loadChat();
});
setInterval(() => { if (!document.hidden) { renderHome(); if (state.current) loadChat(); } }, 4000);

// ---------- routing ----------
function go(pane) { history.pushState({pane}, '', `#/s/${pane}`); route(); }
function route() {
  const m = location.hash.match(/^#\/s\/(.+)$/);
  if (m) openSession(decodeURIComponent(m[1])); else showHome();
}
window.addEventListener('popstate', route);
$('#back').onclick = () => (history.length > 1 && history.state ? history.back() : (location.hash = '', route()));
function showHome() {
  state.current = null; $('#session').hidden = true; $('#home').hidden = false; $('#promptZone').innerHTML = ''; hideSheet();
}

// ---------- session screen ----------
function openSession(pane) {
  if (state.current === pane && !$('#session').hidden) return;
  state.current = pane; state.chatHash = ''; state.rendered = 0; state.prompt = null;
  $('#home').hidden = true; $('#session').hidden = false;
  $('#messages').innerHTML = `<div class="empty">${t('loading')}</div>`; $('#promptZone').innerHTML = ''; $('#keybar').hidden = true;
  $('#message').value = store.get('draft:' + pane) || ''; autosize();
  updateSessionHeader(); loadChat(true);
}
function updateSessionHeader() {
  const s = state.sessions.get(state.current); if (!s) return;
  $('#sTitle').textContent = s.project;
  $('#sSub').textContent = [TOOLNAME[s.agent] || s.agent, s.model, statusText(s.status), ago(s.since)].filter(Boolean).join(' · ');
}
function closedBanner() {
  $('#promptZone').innerHTML = `<div class="prompt"><p class="q">${esc(t('closedSession'))}</p><button class="primary" id="toList">${esc(t('backToList'))}</button></div>`;
  $('#toList').onclick = () => history.state ? history.back() : (location.hash = '', route());
}
const md = t => {
  let h = esc(t);
  h = h.replace(/```[^\n]*\n([\s\S]*?)```/g, (_, c) => `<pre><code>${c.replace(/\n$/, '')}</code></pre>`);
  h = h.replace(/`([^`\n]+)`/g, '<code>$1</code>').replace(/\*\*([^*\n]+)\*\*/g, '<b>$1</b>');
  return h;
};
function msgNode(m) {
  if (m.role === 'tool') {
    const d = document.createElement('details'); d.className = 'tool';
    d.innerHTML = `<summary><b>${esc(m.tool || t('tool'))}</b><span>${esc(m.text.split('\n')[0])}</span></summary>${m.result || m.text.includes('\n') ? `<pre>${esc(m.result || m.text)}</pre>` : ''}`;
    return d;
  }
  const d = document.createElement('div'); d.className = `msg ${m.role}`; d.innerHTML = md(m.text); return d;
}
let loading = false, again = false;
async function loadChat(first) {
  const pane = state.current; if (!pane) return;
  if (loading) { again = true; return; }  // an update arrived mid-request: run once more afterwards, never drop it
  loading = true;
  try {
    const r = await api(`/api/sessions/${encodeURIComponent(pane)}/chat`);
    if (pane !== state.current) return;
    const key = JSON.stringify([r.messages.length, r.messages.at(-1), r.hint]);
    if (key !== state.chatHash) { state.chatHash = key; renderMessages(r, first); }
    renderPrompt(r.prompt, r.status);
    if (r.session) { state.sessions.set(pane, {...state.sessions.get(pane), ...r.session}); updateSessionHeader(); }
  } catch (e) { if (/404/.test(e.message) || e.message === errText('Session is closed or unknown')) closedBanner(); else setConn(false, e.message); }
  finally { loading = false; if (again) { again = false; loadChat(); } }
}
function renderMessages(r, first) {
  const box = $('#messages');
  const near = box.scrollHeight - box.scrollTop - box.clientHeight < 120;
  const fromBottom = box.scrollHeight - box.scrollTop;
  box.querySelector('.hint')?.remove(); box.querySelector('.empty')?.remove();
  const msgs = r.messages;
  const same = state.rendered > 0 && state.sig === sig(msgs[Math.max(0, state.rendered - 1)]) && msgs.length >= state.rendered && state.source === r.source;
  if (!same) { box.innerHTML = ''; state.rendered = 0; }
  for (const m of msgs.slice(state.rendered)) box.appendChild(msgNode(m));
  state.rendered = msgs.length; state.sig = sig(msgs[msgs.length - 1]); state.source = r.source;
  if (!msgs.length) box.innerHTML = `<div class="empty">${t('noChat')}</div>`;
  if (r.hint) { const h = document.createElement('div'); h.className = 'hint'; h.textContent = r.hint.replace(/\(.*$/, '').trim() || t('working'); box.appendChild(h); }
  if (first || near) box.scrollTop = box.scrollHeight;
  else if (!same) box.scrollTop = box.scrollHeight - fromBottom;
  $('#jump').hidden = first || near;
}
const sig = m => m ? `${m.role}|${m.text.length}|${m.text.slice(-24)}` : '';
$('#messages').addEventListener('scroll', e => { const b = e.target; if (b.scrollHeight - b.scrollTop - b.clientHeight < 80) $('#jump').hidden = true; });
$('#jump').onclick = () => { const b = $('#messages'); b.scrollTo({top: b.scrollHeight, behavior: 'smooth'}); $('#jump').hidden = true; };

// ---------- prompts: approval, choices, unknown ----------
function renderPrompt(p, status) {
  const zone = $('#promptZone');
  const sig = p ? p.fingerprint + (p.options || []).map(o => o.checked).join() : status === 'blocked' ? 'unknown' : '';
  if (zone.dataset.sig === sig && !zone.querySelector('.loading')) return;
  zone.dataset.sig = sig; state.prompt = p;
  if (!p && status !== 'blocked') { zone.innerHTML = ''; return; }
  if (!p) { zone.innerHTML = `<div class="prompt"><h3>${esc(t('unknownScreen'))}</h3><p class="q">${esc(t('unknownHelp'))}</p></div>`; $('#keybar').hidden = false; buildKeybar(); return; }
  const tag = p.destructive ? `<span class="tag">${esc(t('destructive'))}</span>` : '';
  if (p.kind === 'approval') {
    const roles = Object.fromEntries(p.options.map(o => [o.role, o]));
    const denyOnly = p.cut;
    zone.innerHTML = `<div class="prompt"><h3>${esc(t('waitingApproval'))} ${tag}</h3><pre>${esc(p.details)}</pre>${denyOnly ? `<p class="q">${esc(t('cutNote'))}</p>` : ''}
      <div class="acts"><button class="danger" data-d="deny">${esc(t('deny'))}</button>${denyOnly ? '' : `<button class="warn" data-d="approve">${esc(t('approve'))}</button>`}</div>
      ${!denyOnly && roles.always && !p.destructive ? `<button class="also" data-d="always">${esc(t('alwaysAllow'))}</button>` : ''}</div>`;
    zone.querySelectorAll('[data-d]').forEach(b => b.onclick = () => decide(b.dataset.d, b));
    return;
  }
  if (p.kind === 'dismiss') { zone.innerHTML = `<div class="prompt"><h3>${esc(t('filePicker'))}</h3><div class="acts"><button data-k="esc">${esc(t('closeEsc'))}</button></div></div>`; zone.querySelector('[data-k]').onclick = e => sendKeys(['esc'], e.currentTarget); return; }
  zone.innerHTML = `<div class="prompt"><h3>${esc(p.header || (p.kind === 'trust' ? t('trustTitle') : t('question')))}</h3><p class="q">${esc(p.kind === 'trust' ? t('trustQ', {f: p.question}) : p.question)}</p>
    <div class="opts">${p.options.map(o => `<button class="opt ${o.checked ? 'on' : ''}" data-n="${o.n}"><span class="n">${p.kind === 'trust' ? '•' : o.n}</span><span class="t">${esc(o.label)}${o.desc ? `<small>${esc(o.desc)}</small>` : ''}</span></button>`).join('')}</div>
    ${p.multi ? `<button class="also" data-k="right">${esc(t('cont'))}</button>` : ''}</div>`;
  zone.querySelectorAll('[data-n]').forEach(b => b.onclick = () => choose(+b.dataset.n, b));
  zone.querySelector('[data-k]')?.addEventListener('click', e => sendKeys(['right'], e.currentTarget));
}
function afterDecision(r) {
  if (!r) return;
  if (r.next && r.next !== state.current) { toast(t('nextBlocked'), ''); setTimeout(() => go(r.next), 350); }
  else if (!r.next && location.hash.startsWith('#/s/')) setTimeout(() => (history.state ? history.back() : (location.hash = '', route())), 450);
}
async function decide(decision, btn, token) {
  const p = state.prompt; if (!p) return;
  const url = `/api/sessions/${encodeURIComponent(state.current)}/approve`;
  const body = {decision, fingerprint: p.fingerprint, confirm_token: token};
  const r = await act(btn, () => api(url, body), token || !p.destructive || decision === 'deny' ? (decision === 'deny' ? t('denied') : t('approved')) : '');
  if (!r) { loadChat(); return; }
  if (r.confirm_required) { confirmSheet(r, () => decide(decision, null, r.confirm_token)); return; }
  hideSheet(); afterDecision(r);
}
function confirmSheet(r, onYes) {
  showSheet(`<h3>${esc(t('irreversible'))}</h3><p>${esc(t('irreversibleP'))}</p><pre class="c-cmd" style="max-height:30dvh;overflow:auto">${esc(r.command)}</pre>
    <div class="acts"><button class="ghost" data-x>${esc(t('cancel'))}</button><button class="warn" data-y>${esc(t('yesRun'))}</button></div>`);
  $('#sheet [data-x]').onclick = hideSheet;
  $('#sheet [data-y]').onclick = async e => { e.currentTarget.classList.add('loading'); await onYes(); };
}
async function choose(n, btn) {
  const p = state.prompt; if (!p) return;
  const r = await act(btn, () => api(`/api/sessions/${encodeURIComponent(state.current)}/choose`, {fingerprint: p.fingerprint, n}), p.multi ? '' : t('sent'));
  if (r) { if (p.multi) setTimeout(loadChat, 300); else afterDecision(r); } else loadChat();
}
async function sendKeys(keys, btn) {
  const r = await act(btn, () => api(`/api/sessions/${encodeURIComponent(state.current)}/keys`, {keys}));
  if (r) setTimeout(loadChat, 250);
}
function buildKeybar() {
  const kb = $('#keybar');
  if (kb.dataset.built) return; kb.dataset.built = 1;
  const keys = [['esc', 'Esc'], ['enter', 'Enter'], ['up', '↑'], ['down', '↓'], ['left', '←'], ['right', '→'], ['tab', 'Tab'], ['1', '1'], ['2', '2'], ['3', '3'], ['4', '4'], ['y', 'y'], ['n', 'n']];
  kb.innerHTML = keys.map(([k, l]) => `<button data-k="${k}">${l}</button>`).join('');
  kb.onclick = e => { const b = e.target.closest('[data-k]'); if (b) sendKeys([b.dataset.k], b); };
}
$('#keysBtn').onclick = () => { buildKeybar(); const kb = $('#keybar'); kb.hidden = !kb.hidden; };

// ---------- composer ----------
const ta = $('#message');
function autosize() { ta.style.height = 'auto'; const h = Math.min(ta.scrollHeight, window.innerHeight * 0.38); ta.style.height = h + 'px'; ta.style.overflowY = ta.scrollHeight > h ? 'auto' : 'hidden'; $('#send').disabled = !ta.value.trim(); }
ta.addEventListener('keydown', e => { if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) { e.preventDefault(); $('#composer').requestSubmit(); } });
ta.addEventListener('input', () => { autosize(); store.set('draft:' + state.current, ta.value); });
$('#composer').addEventListener('submit', async e => {
  e.preventDefault();
  const text = ta.value.trim(); if (!text) return;
  const btn = $('#send');
  const r = await act(btn, () => api(`/api/sessions/${encodeURIComponent(state.current)}/prompt`, {text}), '');
  if (r) { ta.value = ''; store.set('draft:' + state.current, ''); autosize(); toast(t('sent'), 'ok'); setTimeout(loadChat, 500); }
});
// attach: upload the file to the server, then put its path in the draft (agents read files by path)
$('#attachBtn').onclick = () => $('#fileIn').click();
$('#fileIn').onchange = async e => {
  const files = [...e.target.files]; e.target.value = ''; if (!files.length) return;
  const btn = $('#attachBtn');
  const paths = [];
  const r = await act(btn, async () => {
    for (const f of files) {  // one upload per file, sequential; paths are collected as they succeed
      let res;
      try { res = await fetch(`/api/sessions/${encodeURIComponent(state.current)}/upload`, {method: 'POST', headers: {'Content-Type': f.type || 'application/octet-stream'}, body: f}); } catch { throw new Error(t('offline')); }
      if (!res.ok) { let m = `${res.status}`; try { m = (await res.json()).detail || m; } catch {} throw new Error(errText(m)); }
      paths.push((await res.json()).path);
    }
    return true;
  }, t('attached'));
  if (paths.length) { ta.value = (ta.value ? ta.value.replace(/\s*$/, ' ') : '') + paths.join(' ') + ' '; store.set('draft:' + state.current, ta.value); autosize(); ta.focus(); }
};
// keep the composer above the iOS keyboard
if (/iPhone|iPad|iPod/.test(navigator.userAgent)) document.documentElement.classList.add('ios');
if (window.visualViewport) {
  let baseH = 0;
  const sa = navigator.standalone === true || matchMedia('(display-mode: standalone)').matches;
  if (sa) document.documentElement.classList.add('sa');
  const fit = () => {
    baseH = Math.max(baseH, visualViewport.height);  // tallest viewport seen = keyboard closed (innerHeight also shrinks in a home-screen app)
    const kb = baseH - visualViewport.height > 120;
    document.documentElement.classList.toggle('kb', kb);
    // Measured on iPhone (?debug=1, Safari tab): visualViewport already ends above the URL pill and ^ v ✓ bar, so no reserve is needed
    document.documentElement.style.setProperty('--vvh', visualViewport.height + 'px'); window.scrollTo(0, 0); const b = $('#messages'); if (!$('#session').hidden && document.activeElement === ta) b.scrollTop = b.scrollHeight; };
  let debugOn = false;
  const startDebug = () => { if (debugOn) return; debugOn = true;  // on-screen metrics for diagnosing keyboard gaps
    const d = document.createElement('pre');
    d.style.cssText = 'position:fixed;top:0;left:0;right:0;z-index:99999;margin:0;padding:4px 6px;font:11px/1.3 monospace;background:#000c;color:#0f0;pointer-events:none;white-space:pre-wrap';
    document.body.appendChild(d);
    const probe = document.createElement('div');  // resolves env(safe-area-inset-*) to pixels
    probe.style.cssText = 'position:fixed;visibility:hidden;pointer-events:none;top:0;left:0;padding:env(safe-area-inset-top) env(safe-area-inset-right) env(safe-area-inset-bottom) env(safe-area-inset-left)';
    document.body.appendChild(probe);
    const dbg = () => { const c = $('#composer'), r = c && c.getBoundingClientRect(), a = $('#app'), ar = a && a.getBoundingClientRect(), cs = getComputedStyle(probe), de = document.documentElement; d.textContent =
      `vv.h=${visualViewport.height.toFixed(0)} vv.top=${visualViewport.offsetTop.toFixed(0)} inner=${innerHeight} outer=${outerHeight} screen=${screen.height} base=${baseH.toFixed(0)}\n` +
      `html.clientH=${de.clientHeight} body.clientH=${document.body.clientHeight} app.bottom=${ar ? ar.bottom.toFixed(0) : '-'} composer.bottom=${r ? r.bottom.toFixed(0) : '-'} --vvh=${de.style.getPropertyValue('--vvh')}\n` +
      `safe t/r/b/l=${cs.paddingTop}/${cs.paddingRight}/${cs.paddingBottom}/${cs.paddingLeft} standalone=${navigator.standalone === true || matchMedia('(display-mode: standalone)').matches} scrollY=${scrollY.toFixed(0)} cls=${de.className}`; };
    visualViewport.addEventListener('resize', dbg); visualViewport.addEventListener('scroll', dbg); document.addEventListener('focusin', () => setTimeout(dbg, 300)); setInterval(dbg, 500); dbg();
  };
  if (/[?&]debug=1/.test(location.search)) startDebug();
  { let taps = []; document.addEventListener('pointerdown', () => { const n = Date.now(); taps = taps.filter(x => n - x < 2000); taps.push(n); if (taps.length >= 6) { taps = []; startDebug(); } }); }  // 6 quick taps enable the strip (home-screen app has no URL bar)
  visualViewport.addEventListener('resize', fit); visualViewport.addEventListener('scroll', () => window.scrollTo(0, 0)); fit();
}

// ---------- sheets ----------
function showSheet(html) { $('#sheet').innerHTML = html; $('#sheet').hidden = false; $('#sheetBack').hidden = false; }
function hideSheet() { $('#sheet').hidden = true; $('#sheetBack').hidden = true; $('#sheet').innerHTML = ''; }
$('#sheetBack').onclick = hideSheet;

let quickLabel = '';
function setQuickLabel() { $('#toQuick').textContent = quickLabel ? t('quickTo', {x: quickLabel}) : t('quickSend'); }
api('/api/config').then(c => { quickLabel = c.quick_label || ''; $('#toQuick').hidden = !quickLabel; setQuickLabel(); }).catch(() => {});
$('#toQuick').hidden = true;
$('#toQuick').onclick = () => {
  showSheet(`<h3>${esc(t('quickTo', {x: quickLabel}))}</h3><textarea id="quickText" placeholder="${esc(t('quickPh'))}" autofocus></textarea><div class="acts"><button class="ghost" data-x>${esc(t('cancel'))}</button><button class="primary" data-y>${esc(t('send'))}</button></div>`);
  $('#sheet [data-x]').onclick = hideSheet;
  $('#sheet [data-y]').onclick = async e => { const text = $('#quickText').value.trim(); if (!text) return; const r = await act(e.currentTarget, () => api('/api/quick', {text}), t('sent')); if (r) hideSheet(); };
  setTimeout(() => $('#quickText')?.focus(), 250);
};
$('#newSession').onclick = async () => {
  const info = await act(null, () => api('/api/projects')); if (!info) return;
  let cwd = '', tool = 'claude', model = '';
  const draw = (filter = '') => {
    const rows = info.projects.filter(p => (p.display + p.name).toLowerCase().includes(filter.toLowerCase())).slice(0, 60);
    $('#plist').innerHTML = rows.map(p => `<button type="button" class="${p.path === cwd ? 'on' : ''}" data-p="${esc(p.path)}"><span>${esc(p.name)}</span><small>${esc(p.display)}</small></button>`).join('') || `<p class="empty">${t('noFolder')}</p>`;
  };
  const models = () => `<option value="">${esc(t('defaultModel'))}</option>` + (info.tools[tool] || []).map(m => `<option>${esc(m)}</option>`).join('');
  showSheet(`<h3>${esc(t('newTitle'))}</h3><input type="search" id="pfilter" placeholder="${esc(t('folderSearch'))}" autocomplete="off"><div class="plist" id="plist"></div>
    <div class="seg" id="tools">${Object.keys(info.tools).map(t => `<button type="button" data-t="${t}" class="${t === tool ? 'on' : ''}">${esc(TOOLNAME[t])}</button>`).join('')}</div>
    <select id="modelSel">${models()}</select>
    <div class="acts"><button class="ghost" data-x>${esc(t('cancel'))}</button><button class="primary" data-y disabled>${esc(t('start'))}</button></div>`);
  draw();
  $('#pfilter').oninput = e => draw(e.target.value);
  $('#plist').onclick = e => { const b = e.target.closest('[data-p]'); if (!b) return; cwd = b.dataset.p; draw($('#pfilter').value); $('#sheet [data-y]').disabled = false; };
  $('#tools').onclick = e => { const b = e.target.closest('[data-t]'); if (!b) return; tool = b.dataset.t; $('#tools').querySelectorAll('button').forEach(x => x.classList.toggle('on', x === b)); $('#modelSel').innerHTML = models(); };
  $('#sheet [data-x]').onclick = hideSheet;
  $('#sheet [data-y]').onclick = async e => {
    const r = await act(e.currentTarget, () => api('/api/sessions', {cwd, tool, model: $('#modelSel').value}), t('starting'));
    if (r) { hideSheet(); for (let i = 0; i < 20 && !state.sessions.has(r.pane); i++) { await new Promise(ok => setTimeout(ok, 300)); await loadList(); } go(r.pane); }
  };
};
$('#sMenu').onclick = () => {
  const s = state.sessions.get(state.current) || {};
  showSheet(`<h3>${esc(s.project || t('pane'))}</h3><p>${esc(s.cwd || '')}</p><div class="acts" style="flex-direction:column"><button data-r>${esc(t('refreshSession'))}</button><button class="danger" data-c>${esc(t('closeSession'))}</button><button class="ghost" data-x>${esc(t('cancel'))}</button></div>`);
  $('#sheet [data-x]').onclick = hideSheet;
  $('#sheet [data-r]').onclick = () => { hideSheet(); state.chatHash = ''; loadChat(true); };
  $('#sheet [data-c]').onclick = async e => {
    const r = await act(e.currentTarget, () => api(`/api/sessions/${encodeURIComponent(state.current)}/close`, {}));
    if (!r || !r.confirm_required) return;
    showSheet(`<h3>${esc(t('closeAsk'))}</h3><p><b>${esc(r.project)}</b>${esc(r.working ? t('closeWorking') : t('closeIdle'))}${esc(t('cannotUndo'))}</p><div class="acts"><button class="ghost" data-x>${esc(t('cancel'))}</button><button class="danger" data-y>${esc(t('yesClose'))}</button></div>`);
    $('#sheet [data-x]').onclick = hideSheet;
    $('#sheet [data-y]').onclick = async b => { const ok = await act(b.currentTarget, () => api(`/api/sessions/${encodeURIComponent(state.current)}/close`, {confirm_token: r.confirm_token}), t('closed')); if (ok) { hideSheet(); history.state ? history.back() : (location.hash = '', route()); } };
  };
};
$('#refresh').onclick = e => act(e.currentTarget, loadList, '');

// ---------- pull to refresh ----------
(() => {
  const list = $('#list'), ptr = $('#ptr'); let y0 = null, dy = 0;
  list.addEventListener('touchstart', e => { y0 = list.scrollTop <= 0 ? e.touches[0].clientY : null; dy = 0; }, {passive: true});
  list.addEventListener('touchmove', e => { if (y0 === null) return; dy = e.touches[0].clientY - y0; ptr.classList.toggle('show', dy > 20); ptr.textContent = dy > 70 ? t('pullRelease') : t('pull'); }, {passive: true});
  list.addEventListener('touchend', async () => { const go = dy > 70; ptr.classList.remove('show'); y0 = null; if (go) { ptr.textContent = ''; await loadList(); toast(t('refreshed'), 'ok'); } });
})();

// ---------- push ----------
let swReg = null;
async function pushState() {
  const bell = $('#bell');
  if (!('serviceWorker' in navigator) || !('PushManager' in window) || !swReg) { bell.hidden = true; return; }
  const sub = await swReg.pushManager.getSubscription();
  let known = false;
  if (sub && Notification.permission === 'granted') { try { known = (await api('/api/push/known', {endpoint: sub.endpoint})).known; } catch { known = true; } }
  bell.hidden = known;
}
$('#bell').onclick = e => act(e.currentTarget, async () => {
  if (Notification.permission !== 'granted' && await Notification.requestPermission() !== 'granted') throw new Error(t('noPermission'));
  const {key} = await api('/api/push/public'); if (!key) throw new Error(t('noPushKey'));
  const pad = key.replace(/-/g, '+').replace(/_/g, '/').padEnd(Math.ceil(key.length / 4) * 4, '=');
  const sub = await swReg.pushManager.getSubscription() || await swReg.pushManager.subscribe({userVisibleOnly: true, applicationServerKey: Uint8Array.from(atob(pad), c => c.charCodeAt(0))});
  await api('/api/push/subscribe', {...sub.toJSON(), lang: LANG}); await pushState();
}, t('bellOn'));
if ('serviceWorker' in navigator) {
  navigator.serviceWorker.register('/sw.js').then(r => { swReg = r; pushState(); }).catch(() => {});
  navigator.serviceWorker.addEventListener('message', e => { if (e.data?.type === 'open' && e.data.pane) { history.pushState({pane: e.data.pane}, '', `#/s/${e.data.pane}`); route(); } });
}

// ---------- boot ----------
(() => {
  const q = new URLSearchParams(location.search).get('pane');
  if (q) history.replaceState({pane: q}, '', `/#/s/${q}`);
  loadList().then(() => route());
  connect();
})();

// ---------- notification history ----------
$('#history').onclick = e => act(e.currentTarget, async () => {
  const {items} = await api('/api/notifications');
  const rows = items.length ? items.map(n => `<button class="card b-${n.status === 'blocked' ? 'blocked' : 'done'}" data-pane="${esc(n.pane || '')}"><b>${esc(histTitle(n))}</b><div>${esc(histBody(n))}</div><small>${ago(n.ts)}</small></button>`).join('') : `<div class="empty">${t('noNotifs')}</div>`;
  showSheet(`<h3>${esc(t('notifications'))}</h3><div class="scroll" style="max-height:65vh">${rows}</div>`);
  $('#sheet').querySelectorAll('[data-pane]').forEach(b => b.onclick = () => { const p = b.dataset.pane; hideSheet(); if (p && state.sessions.has(p)) go(p); });
});

function histTitle(n) { return n.project ? t(n.status === 'blocked' ? 'needsYouTitle' : 'doneTitle', {p: n.project}) : (n.title || ''); }
function histBody(n) { const b = n.body || ''; return n.project ? ((n.destructive ? '⚠ ' : '') + (b || t(n.status === 'blocked' ? 'askBody' : 'finishedBody'))) : b; }

// ---------- limits ----------
let limitData = null;
const lvl = used => used >= 85 ? 'hot' : used >= 60 ? 'warn' : 'ok';
function fmtIn(secs) {
  if (secs <= 0) return t('justNow');
  const d = Math.floor(secs / 86400), h = Math.floor(secs % 86400 / 3600), m = Math.floor(secs % 3600 / 60);
  return d ? `${d}${t('day')} ${h}${t('hour')}` : h ? `${h}${t('hour')} ${m}${t('min')}` : `${Math.max(1, m)}${t('min')}`;
}
const resetText = (ts, now) => ts ? t('resets', {t: t('resetsIn', {t: fmtIn(ts - now)})}) : '';
function renderLimits() {
  const box = $('#limits');
  const ps = limitData && limitData.providers || [];
  box.hidden = !ps.length;
  if (!ps.length) return;
  const now = Date.now() / 1000;
  box.innerHTML = ps.map(p => {
    const show = [[...p.items].sort((a, b) => b.used - a.used)[0]];  // the tightest window; details on tap
    const stale = p.updated && now - p.updated > 1800;
    return `<span class="lim${stale ? ' stale' : ''}"><b>${esc(p.label)}</b>${show.map(i => `<span class="lv ${lvl(i.used)}" title="${esc(i.label)}"><i style="width:${Math.min(100, i.used)}%"></i></span><em>${esc(i.label.split(' · ').pop())} ${i.used}%</em>`).join('')}</span>`;
  }).join('');
}
function limitsSheet() {
  const now = Date.now() / 1000;
  const ps = limitData && limitData.providers || [];
  const body = ps.length ? ps.map(p => {
    const stale = p.updated && now - p.updated > 1800;
    return `<section class="lp"><h4>${esc(p.label)}${stale ? `<small>${esc(t('stale', {t: fmtIn(now - p.updated)}))}</small>` : ''}</h4>${p.items.map(i => `<div class="li"><div class="lt"><span>${esc(i.label)}</span><span>${esc(t('used', {n: i.used}))} · ${esc(t('left', {n: Math.max(0, 100 - i.used)}))}</span></div><div class="lv big ${lvl(i.used)}"><i style="width:${Math.min(100, i.used)}%"></i></div><small>${esc(resetText(i.resets_at, now))}</small></div>`).join('')}</section>`;
  }).join('') : `<div class="empty">${esc(t('noLimits'))}</div>`;
  showSheet(`<h3>${esc(t('limitsTitle'))}</h3><div class="scroll" style="max-height:65vh">${body}</div>`);
}
async function loadLimits() {
  try { limitData = await api('/api/limits'); renderLimits(); } catch {}
}
$('#limits').onclick = limitsSheet;
setInterval(() => { if (!document.hidden) loadLimits(); }, 60000);
document.addEventListener('visibilitychange', () => { if (!document.hidden) loadLimits(); });

// ---------- language ----------
function refreshLang() {
  applyStaticI18n();
  $('#lang').textContent = LANG.toUpperCase();
  document.title = t('appName');
  setQuickLabel();
  state.sessions.size && renderHome();
  renderLimits();
  if (state.current) { updateSessionHeader(); $('#promptZone').dataset.sig = ''; state.chatHash = ''; loadChat(); }
}
$('#lang').onclick = async () => {
  LANG = LANG === 'tr' ? 'en' : 'tr';
  store.set('lang', LANG);
  hideSheet(); refreshLang();
  try { const sub = await swReg?.pushManager.getSubscription(); if (sub) await api('/api/push/lang', {endpoint: sub.endpoint, lang: LANG}); } catch {}
};
refreshLang();
loadLimits();
