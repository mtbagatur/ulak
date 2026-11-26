'use strict';
// Ulak v2 front end. No build step. Re-render only what changed: cards are patched by key,
// chat messages are appended, the composer and its draft are never touched by updates.
const $ = s => document.querySelector(s);
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const statusText = s => t('st_' + (['blocked', 'working', 'done', 'idle'].includes(s) ? s : 'unknown'));
const TOOLNAME = {claude: 'Claude', codex: 'Codex', agy: 'agy', terminal: 'Terminal'};
const store = {get(k){try{return localStorage.getItem(k)}catch{return null}}, set(k,v){try{localStorage.setItem(k,v)}catch{}}};

const state = {sessions: new Map(), current: null, view: 0, chatHash: '', rendered: 0, prompt: null, idleOpen: store.get('idleOpen') === '1', busy: new Set(), live: false};
const presenceId = crypto.randomUUID ? crypto.randomUUID() : 'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g, c => { const r = Math.random() * 16 | 0; return (c === 'x' ? r : (r & 3 | 8)).toString(16); });
function reportPresence(visible = !document.hidden) {
  fetch('/api/presence', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({client_id: presenceId, visible}), keepalive: true}).catch(() => {});
}
reportPresence(!document.hidden);
setInterval(() => { if (!document.hidden) reportPresence(true); }, 4000);
window.addEventListener('pagehide', () => reportPresence(false));

// ---------- network ----------
async function api(url, body, method, signal) {
  const opt = body === undefined ? {signal} : {method: method || 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)};
  let r;
  try { r = await fetch(url, opt); } catch (error) {
    if (signal?.aborted) throw error;
    throw Object.assign(new Error(t('offline')), {network: true});
  }
  if (!r.ok) {
    let msg = `${r.status}`;
    try { const j = await r.json(); msg = j.detail || msg; } catch {}
    throw Object.assign(new Error(errText(msg)), {status: r.status});
  }
  return r.json();
}
async function readSnapshot(url) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), 15000);
  try { return await api(url, undefined, undefined, controller.signal); }
  finally { clearTimeout(timer); }
}
function toast(text, kind = '') {
  const el = document.createElement('div');
  el.className = `toast ${kind}`; el.textContent = text;
  $('#toasts').appendChild(el);
  if (kind === 'ok' && navigator.vibrate) navigator.vibrate(12);
  setTimeout(() => { el.classList.add('out'); setTimeout(() => el.remove(), 200); }, kind === 'err' ? 4200 : 2200);
}
async function act(btn, fn, okText) {
  // Button loading state + toast, errors never become unhandled rejections.
  if (btn) btn.classList.add('loading');
  try { const r = await fn(); if (okText) toast(okText, 'ok'); return r; }
  catch (e) { toast(e.message, 'err'); return null; }
  finally { if (btn) btn.classList.remove('loading'); }
}
function setPaneActionsBusy(pane, busy) {
  const card = [...document.querySelectorAll('.card')].find(el => el.dataset.key === pane);
  const buttons = [
    ...(card ? card.querySelectorAll('[data-act]') : []),
    ...(state.current === pane ? $('#promptZone').querySelectorAll('button') : []),
    ...(state.current === pane ? $('#keybar').querySelectorAll('button') : []),
    ...(state.current === pane ? $('#sheet').querySelectorAll('[data-confirm-approval]') : []),
  ];
  for (const button of buttons) {
    if (busy) {
      if (!('actionWasAriaDisabled' in button.dataset)) button.dataset.actionWasAriaDisabled = button.getAttribute('aria-disabled') || '';
      button.setAttribute('aria-disabled', 'true');
    } else if ('actionWasAriaDisabled' in button.dataset) {
      const previous = button.dataset.actionWasAriaDisabled;
      previous ? button.setAttribute('aria-disabled', previous) : button.removeAttribute('aria-disabled');
      delete button.dataset.actionWasAriaDisabled;
    }
  }
  for (const root of [card, ...(state.current === pane ? [$('#promptZone'), $('#keybar')] : [])]) {
    if (root) busy ? root.setAttribute('aria-busy', 'true') : root.removeAttribute('aria-busy');
  }
}
function beginPaneAction(pane) {
  if (!pane || state.busy.has(pane)) return null;
  state.busy.add(pane);
  setPaneActionsBusy(pane, true);
  return () => { state.busy.delete(pane); setPaneActionsBusy(pane, false); };
}

// ---------- time ----------
function ago(ts) {
  if (!ts) return '';
  const s = Math.max(0, Date.now() / 1000 - ts);
  if (s < 45) return t('justNow');
  if (s < 3600) return `${Math.round(s / 60)} ${t('min')}`;
  if (s < 86400) return `${Math.floor(s / 3600)} ${t('hour')}`;
  if (s < 30 * 86400) return `${Math.floor(s / 86400)} ${t('day')}`;
  return new Date(ts * 1000).toLocaleDateString(LANG === 'tr' ? 'tr-TR' : 'en-GB', {day: 'numeric', month: 'short', year: 'numeric'});
}

// ---------- home ----------
const GROUPS = ['blocked', 'working', 'done', 'unknown', 'idle'];
const rank = s => ({blocked: 0, working: 1, done: 2, unknown: 3, idle: 4}[s.bucket] ?? 3);
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
// One stroke weight for every mark; the seal (a ring around the U) is the only one that carries meaning.
const ICONS = {
  seal: '<circle cx="12" cy="12" r="8.7"/><path d="M9.2 8.6v3.6a2.8 2.8 0 0 0 5.6 0V8.6"/>',
  pin: '<path d="M9 4h6l-1 6 3 3.5H7l3-3.5z"/><path d="M12 13.5V20"/>',
  close: '<path d="M6.5 6.5l11 11M17.5 6.5l-11 11"/>',
  bell: '<path d="M6 16.5V11a6 6 0 0 1 12 0v5.5l1.5 2h-15z"/><path d="M10 20.5a2 2 0 0 0 4 0"/>',
  refresh: '<path d="M19 12a7 7 0 1 1-2.05-4.95"/><path d="M19 4.5V8h-3.5"/>',
  timer: '<circle cx="12" cy="13" r="7.5"/><path d="M12 9v4l2.5 2M9.5 2.5h5"/>',
};
// The seal you pressed, left on the question it answered. `fresh` plays the press once; a redraw of the same question keeps the mark still.
const sealMark = '<svg viewBox="0 0 32 32" aria-hidden="true"><use class="w" href="#wax"/><circle class="r" cx="16" cy="16" r="9.6"/><path class="u" d="M12 11.2v5.2a4 4 0 0 0 8 0v-5.2"/></svg>';
const isSealed = (pane, p) => !!p && state.sealed?.pane === pane && state.sealed.fingerprint === p.fingerprint;
const stampHtml = p => `<span class="stamp">${sealMark}${p.destructive ? sealMark : ''}</span>`;
function pressSeal(pane, p, el) {
  state.sealed = {pane, fingerprint: p.fingerprint};
  if (!el || el.querySelector('.stamp')) return;
  state.sealHold = performance.now() + 1000;  // agents often move on at once; the seal stays long enough to be seen
  el.insertAdjacentHTML('beforeend', stampHtml(p));
  el.classList.add('stamped', 'pressing');
  if (navigator.vibrate) navigator.vibrate(18);
}
// The answer could not be seen leaving: take the seal back, the question is still open.
function liftSeal() {
  state.sealed = null; state.sealHold = 0;
  document.querySelectorAll('.stamp').forEach(el => { el.parentElement.classList.remove('stamped', 'pressing'); el.remove(); });
}
const icon = (name, size = 18) => `<svg class="ic" width="${size}" height="${size}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${ICONS[name]}</svg>`;
function cardHtml(s) {
  const dup = [...state.sessions.values()].some(o => o !== s && o.project === s.project);
  const p = s.pending;
  // Inline actions include the full command. Longer commands require opening the session.
  const shown = p ? (p.details || p.question || '') : '';
  const inline = p && p.kind === 'approval' && !p.destructive && !p.cut && p.roles.includes('approve') && shown.length <= 160 && shown.split('\n').length <= 3;
  const stateText = s.bucket === 'idle' && s.status === 'done' ? (ago(s.since) ? t('doneAgo', {t: ago(s.since)}) : statusText('done')) : [statusText(s.status), s.status === 'unknown' ? '' : ago(s.since)].filter(Boolean).join(' · ');
  const body = p ? `<pre class="c-cmd${inline ? ' full-command' : ''}">${esc(p.details || p.question)}</pre>` : (s.preview ? `<div class="c-prev">${esc(s.preview)}</div>` : '');
  const pinned = isPinned(s);
  return `<button class="card-open" type="button" aria-label="${esc(t('openSession', {project: s.project}))}"></button><div class="c-top"><span class="proj">${esc(s.project)}</span>${dup && s.workspace_label ? `<span class="ws">${esc(s.workspace_label)}</span>` : ''}<span class="chip ${esc(s.agent)}">${esc(TOOLNAME[s.agent] || s.agent)}</span><span class="model">${esc(s.model)}</span><button class="pin ${pinned ? 'on' : ''}" data-pin aria-label="${esc(t(pinned ? 'unpin' : 'pin'))}" aria-pressed="${pinned}">${icon('pin', 16)}</button></div>
    <div class="state"><span class="d"></span>${esc(stateText)}</div>
    ${s.title ? `<div class="c-title">${esc(s.title)}</div>` : ''}${p && p.destructive ? `<div class="seals">${icon('seal', 14)}${icon('seal', 14)}<span>${esc(t('destructive'))}</span></div>` : ''}${body}
    ${inline ? `<div class="c-act"><button class="deny" data-act="deny">${esc(t('deny'))}</button><button class="seal" data-act="approve">${icon('seal', 17)}${esc(t('approve'))}</button></div>` : ''}${isSealed(s.pane_id, p) ? stampHtml(p) : ''}`;
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
  const hold = (state.sealHold || 0) - performance.now();
  if (hold > 0 && $('#groups .stamp')) { clearTimeout(renderHome.held); renderHome.held = setTimeout(renderHome, hold); return; }
  const all = sorted();
  const blocked = all.filter(s => s.bucket === 'blocked').length;
  const unknown = all.filter(s => s.bucket === 'unknown').length;
  $('#summary').innerHTML = !all.length ? '' : blocked ? t('summaryBlocked', {n: blocked}) : unknown ? t('summaryUnknown', {n: unknown}) : t('summaryCalm', {n: all.filter(s => s.bucket === 'working').length});
  if (navigator.setAppBadge) { blocked ? navigator.setAppBadge(blocked).catch(() => {}) : navigator.clearAppBadge?.().catch(() => {}); }
  const groups = $('#groups');
  if (!all.length) { groups.innerHTML = `<div class="empty">${t('empty')}</div>`; return; }
  if (groups.querySelector('.empty')) groups.innerHTML = '';
  const pinned = all.filter(isPinned);
  const defs = [...(pinned.length ? [{id: 'pinned', title: t('g_pinned'), items: pinned}] : []), ...GROUPS.map(id => ({id, title: t('g_' + id), items: all.filter(s => !isPinned(s) && s.bucket === id)}))].filter(g => g.items.length);
  patch(groups, defs, g => g.id, g => {
    const label = `${g.title} · ${g.items.length}`;
    return g.id === 'idle'
      ? `<h2><button type="button" class="idle-toggle" data-toggle aria-controls="idleCards">${label}<span></span></button></h2><div class="cards" id="idleCards"></div>`
      : `<h2>${label}</h2><div class="cards"></div>`;
  }, g => {
    const el = document.createElement('div'); el._base = 'group'; return el;
  });
  for (const g of defs) {
    const wrap = groups.querySelector(`[data-key="${g.id}"]`);
    wrap.className = `group g-${g.id}`;
    const box = wrap.querySelector('.cards');
    box.hidden = g.id === 'idle' && !state.idleOpen;
    if (g.id === 'idle') {
      const toggle = wrap.querySelector('[data-toggle]');
      toggle.setAttribute('aria-expanded', String(state.idleOpen));
      toggle.querySelector('span').textContent = t(state.idleOpen ? 'hide' : 'show');
    }
    patch(box, g.items, s => s.pane_id, cardHtml, s => { const b = document.createElement('div'); b._base = 'card'; return b; });
    for (const el of box.children) { const s = state.sessions.get(el.dataset.key); el.className = `card b-${s.bucket}${s.pending?.destructive ? ' two-seals' : ''}${isSealed(s.pane_id, s.pending) ? ' stamped' : ''}`; }
  }
  for (const pane of state.busy) setPaneActionsBusy(pane, true);
}
$('#groups').addEventListener('click', e => {
  if (e.target.closest('[data-toggle]')) { state.idleOpen = !state.idleOpen; store.set('idleOpen', state.idleOpen ? '1' : '0'); renderHome(); return; }
  const card = e.target.closest('.card'); if (!card) return;
  const pb = e.target.closest('[data-pin]');
  if (pb) { e.stopPropagation(); const s = state.sessions.get(card.dataset.key); if (s) { const pane = card.dataset.key, k = pinKey(s); pins.has(k) ? pins.delete(k) : pins.add(k); renderHome(); savePins(); requestAnimationFrame(() => [...document.querySelectorAll('.card')].find(el => el.dataset.key === pane)?.querySelector('[data-pin]')?.focus({preventScroll: true})); } return; }
  const pane = card.dataset.key, a = e.target.closest('[data-act]');
  if (a) { e.stopPropagation(); inlineDecide(pane, a.dataset.act, a); return; }
  go(pane);
});
async function inlineDecide(pane, decision, btn) {
  const s = state.sessions.get(pane); if (!s || !s.pending) return;
  const view = state.view, fingerprint = s.pending.fingerprint;
  const unlock = beginPaneAction(pane); if (!unlock) return;
  try {
    const r = await act(btn, () => api(`/api/sessions/${encodeURIComponent(pane)}/approve`, {decision, fingerprint}));
    if (r?.ok) {
      if (decision !== 'deny') pressSeal(pane, s.pending, btn.closest('.card'));
      if (!await watchApprovalReply(pane, fingerprint, view)) liftSeal();
    }
    else {
      if (r?.confirm_required) toast(t('reviewInSession'), 'err');
      loadList();
    }
  } finally { unlock(); }
}

// ---------- data ----------
let es = null, streamReady = false, retryTimer = null, retries = 0;
let snapshotGeneration = 0, listReady = false, chatReadyView = -1, networkError = false;
let listRequest = null;
function loadList() {
  const generation = snapshotGeneration;
  if (listRequest?.generation === generation) return listRequest.promise;
  const request = {generation, events: new Map()};
  listRequest = request;
  request.promise = (async () => {
    try {
      const rows = await readSnapshot('/api/sessions');
      if (generation !== snapshotGeneration) return false;
      state.sessions = new Map(rows.map(s => [s.pane_id, s]));
      for (const [pane, session] of request.events) {
        if (session) state.sessions.set(pane, session);
        else state.sessions.delete(pane);
      }
      renderHome();
      if (state.current) {
        if (state.sessions.has(state.current)) updateSessionHeader();
        else { closedBanner(); chatReadyView = state.view; }
      }
      listReady = true;
      networkError = false;
      updateConnection();
      return true;
    } catch (e) {
      if (generation === snapshotGeneration) snapshotFailed(e);
      return false;
    } finally {
      if (listRequest === request) listRequest = null;
    }
  })();
  return request.promise;
}
function updateConnection() {
  const offline = navigator.onLine === false || networkError;
  if (!state.live && !offline && streamReady && listReady && (!state.current || chatReadyView === state.view)) {
    state.live = true;
    retries = 0;
    clearRetry();
  }
  const banner = $('#conn');
  const text = state.live ? '' : t(offline ? 'offline' : streamReady ? 'syncing' : 'reconnecting');
  banner.hidden = state.live;
  banner.classList.toggle('bad', offline);
  if (banner.textContent !== text) banner.textContent = text;
}
function beginRecovery(network = false) {
  // A response from before this recovery cannot certify the current snapshot.
  snapshotGeneration++;
  state.live = false;
  listReady = false;
  chatReadyView = -1;
  networkError = network;
  updateConnection();
}
function snapshotFailed(error) {
  beginRecovery(error.network);
  if (streamReady) scheduleRetry();
}
function clearRetry() {
  clearTimeout(retryTimer);
  retryTimer = null;
}
function scheduleRetry() {
  if (retryTimer !== null || document.hidden || navigator.onLine === false) return;
  retryTimer = setTimeout(resumeConnection, Math.min(8000, 1000 * 2 ** retries++));
}
function refreshSnapshots() {
  const generation = snapshotGeneration;
  loadList().then(ok => {
    if (ok && generation === snapshotGeneration && state.sessions.has(state.current)) loadChat(!state.chatHash);
  });
}
function resumeConnection() {
  clearRetry();
  if (document.hidden || navigator.onLine === false) return;
  if (es?.readyState === EventSource.CLOSED) {
    es = null;
    streamReady = false;
    beginRecovery();
  }
  if (!es) connect();
  refreshSnapshots();
}
function connect() {
  const source = new EventSource('/api/events');
  es = source;
  source.addEventListener('ready', () => {
    if (es !== source) return;
    clearRetry();
    streamReady = true;
    beginRecovery();
    refreshSnapshots();
    loadPins();
  });
  source.onmessage = ev => {
    if (es !== source || !streamReady) return;
    const m = JSON.parse(ev.data);
    if (m.type === 'receipt') { if (m.pane === state.current) loadChat(); return; }
    if (m.type !== 'session' && m.type !== 'removed') return;
    const session = m.type === 'session' ? m.session : null;
    const pane = session ? session.pane_id : m.pane;
    if (listRequest?.generation === snapshotGeneration) listRequest.events.set(pane, session);
    if (session) state.sessions.set(pane, session);
    else state.sessions.delete(pane);
    renderHome();
    if (pane !== state.current) return;
    if (session) { updateSessionHeader(); loadChat(); }
    else { closedBanner(); chatReadyView = state.view; updateConnection(); }
  };
  source.onerror = () => {
    if (es !== source) return;
    clearRetry();
    streamReady = false;
    beginRecovery();
    // EventSource retries automatically while CONNECTING.
    if (source.readyState === EventSource.CLOSED) {
      es = null;
      scheduleRetry();
    }
  };
}
document.addEventListener('visibilitychange', () => {
  reportPresence(!document.hidden);
  if (document.hidden) clearRetry();
  else resumeConnection();
});
window.addEventListener('offline', () => {
  clearRetry();
  const source = es;
  es = null;
  source?.close();
  streamReady = false;
  beginRecovery();
});
window.addEventListener('online', resumeConnection);
setInterval(() => { if (!document.hidden) { renderHome(); if (state.current) loadChat(); } }, 1500);

// ---------- routing ----------
function go(pane, replace) { history[replace ? 'replaceState' : 'pushState']({pane}, '', `#/s/${pane}`); route(); }
function route() {
  const m = location.hash.match(/^#\/s\/(.+)$/);
  if (m) openSession(decodeURIComponent(m[1])); else showHome();
}
window.addEventListener('popstate', route);
$('#back').onclick = () => (history.length > 1 && history.state ? history.back() : (location.hash = '', route()));
function showHome() {
  state.view++;
  state.current = null; state.prompt = null; $('#session').hidden = true; $('#home').hidden = false;
  $('#promptZone').innerHTML = ''; $('#promptStatus').textContent = ''; hideSheet();
  updateConnection();
}

// ---------- session screen ----------
function openSession(pane) {
  if (state.current === pane && !$('#session').hidden) return;
  hideSheet();
  state.view++;
  state.current = pane; state.chatHash = ''; state.rendered = 0; state.prompt = null;
  loadSlash(pane);
  $('#home').hidden = true; $('#session').hidden = false;
  $('#messages').innerHTML = `<div class="empty">${t('loading')}</div>`; $('#promptZone').innerHTML = ''; $('#keybar').hidden = true;
  delete $('#promptZone').dataset.sig;
  $('#receipt').hidden = true;
  $('#promptStatus').textContent = '';
  $('#message').value = store.get('draft:' + pane) || ''; autosize();
  updateSessionHeader(); loadChat(true);
}
function updateSessionHeader() {
  const s = state.sessions.get(state.current); if (!s) return;
  $('#sTitle').textContent = s.project;
  $('#sSub').textContent = [TOOLNAME[s.agent] || s.agent, s.model, statusText(s.status), ago(s.since)].filter(Boolean).join(' · ');
  // A pane with no agent in it is a shell: it can be read, the server refuses to type into it.
  const plain = isPlain(s);
  $('#composer').hidden = plain; $('#plainNote').hidden = !plain;
  if (plain) $('#keybar').hidden = true;
}
const isPlain = s => !!s && (s.agent || 'terminal') === 'terminal';
function closedBanner() {
  chatRequest = null;
  if ($('#sheet [data-confirm-approval]')) hideSheet();
  state.prompt = null;
  delete $('#promptZone').dataset.sig;
  $('#promptStatus').textContent = '';
  $('#promptZone').innerHTML = `<div class="prompt"><p class="q">${esc(t('closedSession'))}</p><button class="primary" id="toList">${esc(t('backToList'))}</button></div>`;
  $('#toList').onclick = () => history.state ? history.back() : (location.hash = '', route());
}
const md = value => {
  const held = [];
  const hold = html => `\u0001${held.push(html) - 1}\u0002`;
  const link = (label, href) => {
    const external = /^https?:\/\//i.test(href);
    let target = href;
    if (!external) {
      const suffix = href.split(/[?#]/, 1)[0].split('.').pop().toLowerCase();
      if (!/^(pdf|png|jpe?g|webp|gif|heic|txt|md|csv|json)$/.test(suffix) || /^(?:[a-z]+:|\/\/)/i.test(href)) return null;
      target = `/api/sessions/${encodeURIComponent(state.current)}/file?path=${encodeURIComponent(href)}`;
    }
    return `<a href="${esc(target)}" target="_blank" rel="noopener noreferrer">${esc(label)}</a>`;
  };
  let source = String(value ?? '');
  source = source.replace(/```[^\n]*\n([\s\S]*?)```/g, (_, code) => hold(`<pre><code>${esc(code.replace(/\n$/, ''))}</code></pre>`));
  source = source.replace(/`([^`\n]+)`/g, (_, code) => hold(`<code>${esc(code)}</code>`));
  source = source.replace(/\[([^\]]+)\]\(([^)\s]+)\)/g, (whole, label, href) => {
    const html = link(label, href);
    return html ? hold(html) : whole;
  });
  // A path starts where a word starts, so docs/notes.md links whole (relative to the session's folder), not from its first slash.
  source = source.replace(/(^|[^\w.\-\/])((?:\/|\.\.?\/|[\w.\-]+\/)[^\s<>"'`]+)/g, (raw, before, found) => {
    let path = found, trailing = '';
    while (/[),.!?;:]$/.test(path)) { trailing = path.slice(-1) + trailing; path = path.slice(0, -1); }
    const html = link(path, path);
    return html ? before + hold(html) + trailing : raw;
  });
  let html = esc(source).replace(/\*\*([^*\n]+)\*\*/g, '<b>$1</b>');
  html = html.replace(/https?:\/\/[^\s<>"']+/gi, raw => {
    let url = raw, trailing = '';
    while (/[),.!?;:]$/.test(url)) { trailing = url.slice(-1) + trailing; url = url.slice(0, -1); }
    return (link(url, url) || esc(url)) + trailing;
  });
  return html.replace(/\u0001(\d+)\u0002/g, (_, n) => held[Number(n)] || '');
};
// A subagent is not one more tool line: it is someone sent off on an errand, so it gets a card that says what for and whether it is back.
function subNode(d, m) {
  const state = ['running', 'done', 'failed'].includes(m.agent.state) ? m.agent.state : 'running';
  d.className = 'tool sub'; d.dataset.state = state;
  d.innerHTML = `<summary><span class="sub-mark" aria-hidden="true"></span><span class="sub-body"><span class="sub-kind">${esc(t('subagent') + (m.agent.kind ? ' · ' + m.agent.kind : ''))}</span><span class="sub-what">${esc(m.text || t('subagent'))}</span></span><span class="sub-state">${esc(t({running: 'subRunning', done: 'subDone', failed: 'subFailed'}[state]))}</span></summary>${m.result ? `<pre>${md(m.result)}</pre>` : ''}`;
  return d;
}
const subSig = m => m.agent ? `${m.agent.state}|${(m.result || '').length}` : '';
function msgNode(m) {
  if (m.role === 'tool' && m.agent) { const d = subNode(document.createElement('details'), m); d.dataset.sig = subSig(m); return d; }
  if (m.role === 'tool') {
    const d = document.createElement('details'); d.className = 'tool';
    d.innerHTML = `<summary><b>${esc(m.tool || t('tool'))}</b><span>${md(m.text.split('\n')[0])}</span></summary>${m.result || m.text.includes('\n') ? `<pre>${md(m.result || m.text)}</pre>` : ''}`;
    return d;
  }
  const d = document.createElement('div'); d.className = `msg ${m.role}`; d.innerHTML = md(m.text); return d;
}
let chatRequest = null;
async function loadChat(first = false) {
  const pane = state.current, view = state.view, generation = snapshotGeneration;
  if (!pane || navigator.onLine === false || (!state.live && !listReady)) return;
  if (chatRequest?.view === view && chatRequest.generation === generation) {
    chatRequest.again = true;
    chatRequest.first ||= first;
    return;
  }
  const request = {view, generation, again: false, first: false};
  chatRequest = request;
  try {
    const r = await readSnapshot(`/api/sessions/${encodeURIComponent(pane)}/chat`);
    if (chatRequest !== request || view !== state.view || generation !== snapshotGeneration) return;
    const key = JSON.stringify([r.messages.length, r.messages.at(-1), r.hint, r.messages.map(subSig).join()]);
    if (key !== state.chatHash) { state.chatHash = key; renderMessages(r, first); }
    renderPrompt(r.prompt, r.status);
    renderReceipt(r.receipt);
    if (state.busy.has(pane)) setPaneActionsBusy(pane, true);
    if (r.session) { state.sessions.set(pane, {...state.sessions.get(pane), ...r.session}); updateSessionHeader(); }
    chatReadyView = view;
    networkError = false;
    updateConnection();
  } catch (e) {
    if (chatRequest !== request || view !== state.view || generation !== snapshotGeneration) return;
    if (e.status === 404) {
      closedBanner();
      chatReadyView = view;
      updateConnection();
    } else snapshotFailed(e);
  } finally {
    if (chatRequest === request) {
      chatRequest = null;
      if (request.again && view === state.view && generation === snapshotGeneration) loadChat(request.first);
    }
  }
}
function renderMessages(r, first) {
  const box = $('#messages');
  const near = box.scrollHeight - box.scrollTop - box.clientHeight < 120;
  const fromBottom = box.scrollHeight - box.scrollTop;
  box.querySelector('.hint')?.remove(); box.querySelector('.empty')?.remove();
  const msgs = r.messages;
  const same = state.rendered > 0 && state.sig === sig(msgs[Math.max(0, state.rendered - 1)]) && msgs.length >= state.rendered && state.source === r.source;
  if (!same) { box.innerHTML = ''; state.rendered = 0; }
  // A subagent comes back long after its card was drawn: update that card where it stands.
  msgs.slice(0, state.rendered).forEach((m, i) => { const el = box.children[i]; if (m.agent && el?.classList.contains('sub') && el.dataset.sig !== subSig(m)) { subNode(el, m); el.dataset.sig = subSig(m); } });
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
  const confirmation = $('#sheet [data-confirm-approval]');
  if (confirmation && confirmation.dataset.fingerprint !== p?.fingerprint) hideSheet();
  state.prompt = p; state.promptStatus = status;
  if (zone.dataset.sig === sig) return;
  const hold = (state.sealHold || 0) - performance.now(), pane = state.current;
  if (hold > 0 && pane === state.sealed?.pane && zone.querySelector('.stamp')) {
    clearTimeout(renderPrompt.held);
    renderPrompt.held = setTimeout(() => { if (state.current === pane) renderPrompt(state.prompt, state.promptStatus); }, hold);
    return;
  }
  zone.dataset.sig = sig;
  const promptAnnouncement = !p
    ? status === 'blocked' ? t('unknownScreen') : ''
    : p.kind === 'approval' ? t('waitingApproval')
      : p.kind === 'trust' ? t('trustQ', {f: p.question})
        : p.kind === 'dismiss' ? t('filePicker')
          : [p.header, p.question].filter(Boolean).join('. ') || t('question');
  const promptStatus = $('#promptStatus');
  if (promptStatus.textContent !== promptAnnouncement) promptStatus.textContent = promptAnnouncement;
  if (!p && status !== 'blocked') { zone.innerHTML = ''; return; }
  if (!p) { zone.innerHTML = `<div class="prompt"><h3>${esc(t('unknownScreen'))}</h3><p class="q">${esc(t('unknownHelp'))}</p></div>`; $('#keybar').hidden = isPlain(state.sessions.get(state.current)); buildKeybar(); return; }
  const two = p.destructive ? ' two-seals' : '';
  const tag = p.destructive ? `<span class="tag">${esc(t('destructive'))}</span>` : '';
  if (p.kind === 'approval') {
    const roles = Object.fromEntries(p.options.map(o => [o.role, o]));
    const denyOnly = p.cut;
    zone.innerHTML = `<div class="prompt${two}${isSealed(state.current, p) ? ' stamped' : ''}"><h3>${p.destructive ? `${icon('seal', 15)}${icon('seal', 15)}<span>${esc(t('destructive'))}</span>` : esc(t('waitingApproval'))}</h3><pre>${esc(p.details)}</pre>${p.hook_note ? `<p class="hook-note">${esc(p.hook_note)}</p>` : ''}${denyOnly ? `<p class="q">${esc(t('cutNote'))}</p>` : ''}${p.destructive && !denyOnly ? `<p class="hook-note two-note">${esc(t('twoSealsNote'))}</p>` : ''}
      <div class="acts"><button class="deny" data-d="deny">${esc(t('deny'))}</button>${denyOnly ? '' : p.destructive ? `<button class="seal red" data-d="approve">${esc(t('review'))}</button>` : `<button class="seal" data-d="approve">${icon('seal', 18)}${esc(t('approve'))}</button>`}</div>
      ${!denyOnly && roles.always && !p.destructive ? `<details class="more"><summary>${esc(t('otherAnswers'))}</summary><p>${esc(t('alwaysNote'))}</p><button class="also" data-d="always">${esc(t('alwaysAllow'))}</button></details>` : ''}${isSealed(state.current, p) ? stampHtml(p) : ''}</div>`;
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
// ---------- receipt: sent -> the agent has it ----------
const clock = ts => new Date(ts * 1000).toLocaleTimeString(LANG === 'tr' ? 'tr-TR' : 'en-GB', {hour: '2-digit', minute: '2-digit'});
const DECIDED = {approve: 'approved', deny: 'denied', always: 'allowedSession'};
const arrival = r => r.delivered ? t('rcptArrived', {t: clock(r.delivered)}) : r.delivered === false ? t('rcptUnknown') : t('rcptPending');
function renderReceipt(r) {
  const el = $('#receipt');
  const fresh = !!r && Date.now() / 1000 - r.ts < 600;  // a receipt is news for ten minutes, then it lives in Decisions
  el.hidden = !fresh;
  if (!fresh) return;
  // The road: sent -> on its way -> the agent has it. The last stop is drawn as a seal once the prompt left the agent's screen.
  const sent = `${t(DECIDED[r.decision] || 'sent')} ${clock(r.ts)}`;
  const label = `${sent} · ${arrival(r)}`;
  if (el.getAttribute('aria-label') === label) return;
  el.setAttribute('aria-label', label);
  const lost = r.delivered === false;
  el.className = 'receipt' + (r.delivered ? ' ok' : lost ? ' unk' : '');
  el.innerHTML = `<span class="leg on">${r.decision === 'deny' ? '' : icon('seal', 14)}${esc(sent)}</span><i class="on"></i><span class="leg on">${esc(r.delivered ? '' : arrival(r))}</span><i class="${r.delivered ? 'on' : ''}"></i><span class="leg${r.delivered ? ' on' : ''}">${r.delivered ? icon('seal', 14) + esc(arrival(r)) : ''}</span>`;
}
function afterDecision(r, view) {
  if (!r || state.view !== view) return;
  const next = r.next;
  if (next === state.current) return;
  if (!next) return;  // nothing else is waiting: stay and watch the answer reach the agent
  toast(t('nextBlocked'), '');
  setTimeout(() => { if (state.view === view) go(next, !!state.current); }, 350);  // from a session: take its place, so back still leads home
}
async function watchApprovalReply(pane, fingerprint, view) {
  toast(t('replySent'), 'ok');
  const deadline = performance.now() + 6500;
  for (const delay of [700, 2500, 2300]) {
    if (performance.now() + delay >= deadline) break;
    await new Promise(resolve => setTimeout(resolve, delay));
    const remaining = deadline - performance.now();
    if (remaining <= 0) break;
    let timer;
    const fresh = await Promise.race([
      loadList(),
      new Promise(resolve => { timer = setTimeout(resolve, remaining, false); }),
    ]);
    clearTimeout(timer);
    if (!fresh) continue;
    const session = state.sessions.get(pane);
    if (session?.pending?.fingerprint && session.pending.fingerprint !== fingerprint) return 'changed';
    if (['working', 'idle', 'done'].includes(session?.status)) return 'cleared';
  }
  if (state.view === view || state.current === pane) toast(t('replyUnverified'), 'err');
  return null;
}
async function decide(decision, btn, confirmation) {
  const pane = state.current, p = state.prompt, view = state.view;
  if (confirmation && (pane !== confirmation.pane || p?.fingerprint !== confirmation.fingerprint)) {
    hideSheet();
    toast(errText('The approval screen changed; refresh'), 'err');
    return;
  }
  if (!pane || !p) return;
  const unlock = beginPaneAction(pane); if (!unlock) return;
  try {
    const url = `/api/sessions/${encodeURIComponent(pane)}/approve`;
    const body = {decision, fingerprint: confirmation?.fingerprint ?? p.fingerprint, confirm_token: confirmation?.token};
    const r = await act(btn, () => api(url, body));
    if (r?.ok) {
      if (confirmation && state.view === view) hideSheet();
      if (decision !== 'deny') pressSeal(pane, p, state.current === pane && state.prompt?.fingerprint === p.fingerprint ? $('#promptZone .prompt') : null);
      const observed = await watchApprovalReply(pane, p.fingerprint, view);
      if (!observed) liftSeal();
      if (state.current === pane) loadChat();
      if (observed === 'cleared') {
        const next = state.sessions.get(r.next)?.status === 'blocked' ? r.next : null;
        afterDecision({next}, view);
      }
      return;
    }
    if (state.view !== view) return;
    if (!r) { if (confirmation) hideSheet(); loadChat(); return; }
    if (r.confirm_required) {
      if (state.prompt?.fingerprint !== p.fingerprint) {
        toast(errText('The approval screen changed; refresh'), 'err');
        return;
      }
      const context = {pane, fingerprint: p.fingerprint, token: r.confirm_token};
      confirmSheet(r, context, button => decide(decision, button, context));
      return;
    }
  } finally { unlock(); }
}
function confirmSheet(r, context, onYes) {
  showSheet(`<h3>${esc(t('irreversible'))}</h3><p>${esc(t('irreversibleP'))}</p><pre class="c-cmd" style="max-height:30dvh;overflow:auto">${esc(r.command)}</pre>
    <div class="acts"><button class="ghost" data-x>${esc(t('cancel'))}</button><button class="seal red" data-y data-confirm-approval>${icon('seal', 18)}${esc(t('yesRun'))}</button></div>`);
  $('#sheet [data-x]').onclick = hideSheet;
  const yes = $('#sheet [data-y]');
  yes.dataset.fingerprint = context.fingerprint;
  yes.onclick = e => {
    if (e.currentTarget.closest('#sheet')?.inert === false) onYes(e.currentTarget);
  };
}
async function choose(n, btn) {
  const pane = state.current, p = state.prompt, view = state.view; if (!pane || !p) return;
  const unlock = beginPaneAction(pane); if (!unlock) return;
  try {
    const r = await act(btn, () => api(`/api/sessions/${encodeURIComponent(pane)}/choose`, {fingerprint: p.fingerprint, n}), p.multi ? '' : t('replySent'));
    if (state.view !== view) return;
    if (r) { if (p.multi) { await new Promise(resolve => setTimeout(resolve, 300)); await loadChat(); } else afterDecision(r, view); } else loadChat();
  } finally { unlock(); }
}
async function sendKeys(keys, btn) {
  const pane = state.current; if (!pane) return;
  const unlock = beginPaneAction(pane); if (!unlock) return;
  try {
    const r = await act(btn, () => api(`/api/sessions/${encodeURIComponent(pane)}/keys`, {keys}));
    if (r) setTimeout(loadChat, 250);
  } finally { unlock(); }
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
const slashMenu = $('#slashOptions');
let slashIndex = 0;
// Commands come from the machine (built-ins, ~/.claude, the project's .claude, Codex prompts); the short list is the fallback while offline or on an older server.
const SLASH_FALLBACK = {claude: ['/clear', '/compact', '/help', '/model', '/config'], codex: ['/clear', '/compact', '/help', '/model', '/status'], agy: ['/clear', '/help']};
const slashCache = new Map();
function slashCommands() {
  return slashCache.get(state.current) || (SLASH_FALLBACK[state.sessions.get(state.current)?.agent] || []).map(name => ({name, description: ''}));
}
async function loadSlash(pane) {
  if (slashCache.has(pane)) return;
  try {
    const r = await fetch(`/api/sessions/${encodeURIComponent(pane)}/commands`);
    if (r.ok) slashCache.set(pane, (await r.json()).commands.filter(c => /^\/[\w.:-]+$/.test(c.name)));
  } catch {}
}
function updateSlashOptions() {
  const match = ta.value.match(/^\/([^\s]*)$/);
  const q = match ? match[1].toLowerCase() : '';
  const all = match ? slashCommands() : [];
  const starts = all.filter(c => c.name.slice(1).toLowerCase().startsWith(q));
  const commands = [...starts, ...(q ? all.filter(c => !starts.includes(c) && c.name.toLowerCase().includes(q)) : [])].slice(0, 40);
  if (!commands.length) { slashMenu.hidden = true; slashMenu.replaceChildren(); return; }
  slashIndex = Math.min(slashIndex, commands.length - 1);
  slashMenu.innerHTML = commands.map((command, i) => `<button type="button" role="option" aria-selected="${i === slashIndex}" data-slash-cmd="${esc(command.name)}"><span>${esc(command.name)}</span>${command.description ? `<small>${esc(command.description)}</small>` : ''}</button>`).join('');
  slashMenu.hidden = false;
}
function chooseSlash(command) {
  ta.value = command + ' ';
  store.set('draft:' + state.current, ta.value);
  slashMenu.hidden = true;
  autosize();
  ta.focus();
}
slashMenu.addEventListener('pointerdown', e => { if (e.target.closest('[data-slash-cmd]')) e.preventDefault(); });
slashMenu.addEventListener('click', e => { const button = e.target.closest('[data-slash-cmd]'); if (button) chooseSlash(button.dataset.slashCmd); });
function autosize() { ta.style.height = 'auto'; const h = Math.min(ta.scrollHeight, window.innerHeight * 0.38); ta.style.height = h + 'px'; ta.style.overflowY = ta.scrollHeight > h ? 'auto' : 'hidden'; $('#send').disabled = !ta.value.trim(); }
ta.addEventListener('keydown', e => {
  const options = [...slashMenu.querySelectorAll('[data-slash-cmd]')];
  if (!slashMenu.hidden && options.length && (e.key === 'ArrowDown' || e.key === 'ArrowUp')) {
    e.preventDefault(); slashIndex = (slashIndex + (e.key === 'ArrowDown' ? 1 : -1) + options.length) % options.length; updateSlashOptions(); return;
  }
  if (!slashMenu.hidden && options.length && (e.key === 'Tab' || (e.key === 'Enter' && !e.shiftKey && !e.isComposing))) {
    e.preventDefault(); chooseSlash(options[slashIndex].dataset.slashCmd); return;
  }
  if (e.key === 'Escape' && !slashMenu.hidden) { e.preventDefault(); slashMenu.hidden = true; return; }
  if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) { e.preventDefault(); $('#composer').requestSubmit(); }
});
ta.addEventListener('input', () => { slashIndex = 0; updateSlashOptions(); autosize(); store.set('draft:' + state.current, ta.value); });
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
  // Only a home-screen app that draws under the status bar (black-translucent, top inset > 0) loses its bottom strip; one that starts below the bar reaches the screen bottom and keeps the real inset.
  const probe = document.createElement('div'); probe.style.cssText = 'position:fixed;visibility:hidden;padding-top:env(safe-area-inset-top)'; document.body.append(probe);
  const insetTop = parseFloat(getComputedStyle(probe).paddingTop) || 0; probe.remove();
  if (sa && insetTop > 0) document.documentElement.classList.add('sa');
  if (sa) document.documentElement.classList.add('installed');
  const fit = () => {
    baseH = Math.max(baseH, visualViewport.height);  // tallest viewport seen = keyboard closed (innerHeight also shrinks in a home-screen app)
    const kb = baseH - visualViewport.height > 120;
    document.documentElement.classList.toggle('kb', kb);
    // Measured on iPhone (?debug=1, Safari tab): visualViewport already ends above the URL pill and ^ v done bar, so no reserve is needed
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
let sheetTimer = 0;
let sheetOpener = null;
function showSheet(html) {
  const s = $('#sheet');
  if (s.hidden) sheetOpener = document.activeElement === document.body ? null : document.activeElement;
  clearTimeout(sheetTimer); s.classList.remove('closing'); $('#sheetBack').classList.remove('closing');
  s.innerHTML = `<div class="grab" aria-hidden="true"></div>${html}`;
  // Sheets with their own Cancel keep a two-button focus loop; the rest get one consistent close control.
  if (!s.querySelector('[data-x]')) { s.insertAdjacentHTML('beforeend', `<button class="icon sheet-x" data-close aria-label="${esc(t('close'))}">${icon('close', 20)}</button>`); s.querySelector('[data-close]').onclick = hideSheet; }
  const title = s.querySelector('h3');
  if (title) { title.id = 'sheetTitle'; s.setAttribute('aria-labelledby', title.id); }
  else s.removeAttribute('aria-labelledby');
  s.hidden = false; s.inert = false; $('#sheetBack').hidden = false;
  (s.querySelector('[autofocus],button:not(:disabled):not([data-close]),input:not(:disabled),textarea:not(:disabled),select:not(:disabled),[data-close]') || s).focus({preventScroll: true});
}
function hideSheet() {
  const s = $('#sheet'), b = $('#sheetBack');
  if (s.hidden || s.inert) return;
  s.inert = true;
  clearTimeout(sheetTimer); s.classList.add('closing'); b.classList.add('closing');
  sheetTimer = setTimeout(() => {
    s.hidden = true; b.hidden = true; s.innerHTML = ''; s.classList.remove('closing'); b.classList.remove('closing');
    if (document.activeElement === document.body) {
      const opener = sheetOpener?.isConnected && !sheetOpener.closest('[hidden]') ? sheetOpener : $(state.current ? '#back' : '#newSession');
      opener.focus({preventScroll: true});
    }
    sheetOpener = null;
  }, 200);
}
document.addEventListener('keydown', e => {
  const s = $('#sheet');
  if (s.hidden || s.inert) return;
  if (e.key === 'Escape') { e.preventDefault(); hideSheet(); return; }
  if (e.key !== 'Tab') return;
  const focusable = [...s.querySelectorAll('a[href],button:not(:disabled),input:not(:disabled),textarea:not(:disabled),select:not(:disabled),[tabindex]:not([tabindex="-1"])')].filter(el => !el.hidden);
  if (!focusable.length) { e.preventDefault(); s.focus(); return; }
  const first = focusable[0], last = focusable[focusable.length - 1];
  if (e.shiftKey && (document.activeElement === first || !s.contains(document.activeElement))) { e.preventDefault(); last.focus(); }
  else if (!e.shiftKey && (document.activeElement === last || !s.contains(document.activeElement))) { e.preventDefault(); first.focus(); }
});
$('#sheetBack').onclick = hideSheet;
// Sheet: drag down to dismiss. Track = 1:1 transform, release = distance OR velocity.
(() => {
  const s = $('#sheet'), b = $('#sheetBack');
  let y0 = null, dy = 0, t0 = 0, drag = false;
  s.addEventListener('touchstart', e => {
    if (e.touches.length !== 1 || s.classList.contains('closing')) { y0 = null; return; }
    y0 = e.touches[0].clientY; dy = 0; t0 = performance.now(); drag = false;
  }, {passive: true});
  s.addEventListener('touchmove', e => {
    if (y0 === null) return;
    const d = e.touches[0].clientY - y0;
    if (!drag) { if (d > 6 && s.scrollTop <= 0) { drag = true; s.style.transition = b.style.transition = 'none'; y0 = e.touches[0].clientY; t0 = performance.now(); dy = 0; } else return; }
    e.preventDefault();
    dy = Math.max(0, e.touches[0].clientY - y0);
    s.style.transform = `translateY(${dy}px)`;
    b.style.opacity = String(Math.max(0, 1 - dy / s.offsetHeight));
  }, {passive: false});
  const end = () => {
    if (!drag) { y0 = null; return; }
    const v = dy / Math.max(1, performance.now() - t0);
    drag = false; y0 = null;
    s.style.transition = b.style.transition = '';
    if (dy > s.offsetHeight * 0.4 || v > 0.5) { s.style.transform = ''; b.style.opacity = ''; hideSheet(); return; }
    s.style.transition = 'transform .25s var(--ease-drawer)'; b.style.transition = 'opacity .25s';
    s.style.transform = ''; b.style.opacity = '';
    setTimeout(() => { s.style.transition = ''; b.style.transition = ''; }, 260);
  };
  s.addEventListener('touchend', end); s.addEventListener('touchcancel', end);
})();

let quickLabel = '', approvalTimeout = 0;
function setQuickLabel() { $('#toQuick').textContent = quickLabel ? t('quickTo', {x: quickLabel}) : t('quickSend'); }
api('/api/config').then(c => { quickLabel = c.quick_label || ''; approvalTimeout = +c.approval_timeout_minutes || 0; $('#toQuick').hidden = !quickLabel; setQuickLabel(); }).catch(() => {});
$('#toQuick').hidden = true;
$('#toQuick').onclick = () => {
  showSheet(`<h3>${esc(t('quickTo', {x: quickLabel}))}</h3><textarea id="quickText" aria-label="${esc(t('quickPh'))}" placeholder="${esc(t('quickPh'))}" autofocus></textarea><div class="acts"><button class="ghost" data-x>${esc(t('cancel'))}</button><button class="primary" data-y>${esc(t('send'))}</button></div>`);
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
  showSheet(`<h3>${esc(t('newTitle'))}</h3><input type="search" id="pfilter" aria-label="${esc(t('folderSearch'))}" placeholder="${esc(t('folderSearch'))}" autocomplete="off"><div class="plist" id="plist"></div>
    <div class="seg" id="tools">${Object.keys(info.tools).map(t => `<button type="button" data-t="${t}" class="${t === tool ? 'on' : ''}">${esc(TOOLNAME[t])}</button>`).join('')}</div>
    <select id="modelSel" aria-label="${esc(t('modelLabel'))}">${models()}</select>
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
// The agent's terminal as text. Looking only: there is no input here and the request cannot send a key.
let screenTimer = 0;
function screenSheet() {
  const pane = state.current;
  showSheet(`<h3>${esc(t('agentScreen'))}</h3><p class="note">${esc(t('agentScreenNote'))}</p><pre class="term" tabindex="0">${esc(t('loading'))}</pre>`);
  const pre = $('#sheet pre.term');
  let first = true;
  const tick = async () => {
    if (!pre.isConnected || state.current !== pane) { clearInterval(screenTimer); return; }
    if (document.hidden) return;
    try {
      const {text} = await api(`/api/sessions/${encodeURIComponent(pane)}/screen`);
      if (!pre.isConnected || pre.textContent === text) return;
      const pinned = first || pre.scrollTop + pre.clientHeight >= pre.scrollHeight - 24;
      pre.textContent = text;
      if (pinned) pre.scrollTop = pre.scrollHeight;
      first = false;
    } catch {}
  };
  clearInterval(screenTimer); screenTimer = setInterval(tick, 2000); tick();
}
// What this session changed since the last commit. Looking only: one GET, no path sent from the phone.
function diffHtml(diff) {
  let head = true;
  return diff.replace(/\n+$/, '').split('\n').map(line => {
    if (line.startsWith('diff --git ')) head = true;
    let c = '';
    if (line.startsWith('@@')) { head = false; c = 'at'; }
    else if (head) c = 'h';
    else if (line[0] === '+') c = 'add';
    else if (line[0] === '-') c = 'del';
    return `<span class="${c}">${esc(line)}</span>`;
  }).join('\n');
}
async function changesSheet() {
  const pane = state.current;
  showSheet(`<h3>${esc(t('changes'))}</h3><p class="note">${esc(t('changesNote'))}</p><div class="chg"><p class="empty">${esc(t('loading'))}</p></div>`);
  const box = $('#sheet .chg');
  let c;
  try { c = await api(`/api/sessions/${encodeURIComponent(pane)}/changes`); }
  catch { if (box.isConnected) box.innerHTML = `<p class="empty">${esc(t('changesFailed'))}</p>`; return; }
  if (!box.isConnected || state.current !== pane) return;
  if (c.branch) $('#sheet .note').textContent = t('changesBranch', {b: c.branch});
  const files = c.files || [];
  let html = '';
  if (files.length) html += `<ul class="files">${files.map(f => `<li><b>${esc(f.code)}</b><span>${esc(f.path)}</span></li>`).join('')}</ul>`;
  if (c.more_files) html += `<p class="note files-more">${esc(t('moreFiles', {n: c.more_files}))}</p>`;
  if (c.diff) html += `<pre class="term diff" tabindex="0">${diffHtml(c.diff)}</pre><p class="note cut"${c.truncated ? '' : ' hidden'}>${esc(t('diffCut'))}</p>`;
  else html += `<p class="empty">${esc(t(!c.repo ? 'notRepo' : files.length ? 'onlyNew' : 'nothingChanged'))}</p>`;
  box.innerHTML = html;
}
$('#sMenu').onclick = () => {
  const s = state.sessions.get(state.current) || {};
  showSheet(`<h3>${esc(s.project || t('pane'))}</h3><p>${esc(s.cwd || '')}</p><div class="acts" style="flex-direction:column"><button data-s>${esc(t('agentScreen'))}</button><button data-d>${esc(t('changes'))}</button><button data-r>${esc(t('refreshSession'))}</button><button class="danger" data-c>${esc(t('closeSession'))}</button><button class="ghost" data-x>${esc(t('cancel'))}</button></div>`);
  $('#sheet [data-x]').onclick = hideSheet;
  $('#sheet [data-r]').onclick = () => { hideSheet(); state.chatHash = ''; loadChat(true); };
  $('#sheet [data-s]').onclick = screenSheet;
  $('#sheet [data-d]').onclick = changesSheet;
  $('#sheet [data-c]').onclick = async e => {
    const r = await act(e.currentTarget, () => api(`/api/sessions/${encodeURIComponent(state.current)}/close`, {}));
    if (!r || !r.confirm_required) return;
    showSheet(`<h3>${esc(t('closeAsk'))}</h3><p><b>${esc(r.project)}</b>${esc(r.working ? t('closeWorking') : t('closeIdle'))}${esc(t('cannotUndo'))}</p><div class="acts"><button class="ghost" data-x>${esc(t('cancel'))}</button><button class="danger" data-y>${esc(t('yesClose'))}</button></div>`);
    $('#sheet [data-x]').onclick = hideSheet;
    $('#sheet [data-y]').onclick = async b => { const ok = await act(b.currentTarget, () => api(`/api/sessions/${encodeURIComponent(state.current)}/close`, {confirm_token: r.confirm_token}), t('closed')); if (ok) { hideSheet(); history.state ? history.back() : (location.hash = '', route()); } };
  };
};


// ---------- pull to refresh ----------
(() => {
  const list = $('#list'), ptr = $('#ptr'); let y0 = null, dy = 0;
  list.addEventListener('touchstart', e => { y0 = list.scrollTop <= 0 ? e.touches[0].clientY : null; dy = 0; }, {passive: true});
  list.addEventListener('touchmove', e => { if (y0 === null) return; dy = e.touches[0].clientY - y0; ptr.classList.toggle('show', dy > 20); ptr.textContent = dy > 70 ? t('pullRelease') : t('pull'); }, {passive: true});
  list.addEventListener('touchend', async () => { const go = dy > 70; ptr.classList.remove('show'); y0 = null; if (go) { ptr.textContent = ''; if (await loadList()) toast(t('refreshed'), 'ok'); } });
})();

// ---------- push ----------
let swReg = null;
async function pushState() {
  if (!('serviceWorker' in navigator) || !('PushManager' in window) || !swReg) { setBell('none'); return; }
  const sub = await swReg.pushManager.getSubscription();
  let known = false;
  if (sub && Notification.permission === 'granted') { try { known = (await api('/api/push/known', {endpoint: sub.endpoint})).known; } catch { known = true; } }
  setBell(known ? 'on' : 'off');
}
// 'none' = this browser cannot push, 'off' = it can and is not set up (the settings button carries a dot), 'on' = done
let bellState = 'none';
function setBell(v) { bellState = v; $('#settings').classList.toggle('dot', v === 'off'); if ($('#sheet [data-settings]')) settingsSheet(); }
const enableBell = btn => act(btn, async () => {
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
  route();
  resumeConnection();
})();

// ---------- notification history ----------
$('#history').onclick = e => act(e.currentTarget, async () => {
  const [{items}, audit] = await Promise.all([api('/api/notifications'), api('/api/audit')]);
  const rows = items.length ? items.map(n => `<button class="card b-${n.status === 'blocked' ? 'blocked' : 'done'}" data-pane="${esc(n.pane || '')}"><b>${esc(histTitle(n))}</b><div>${esc(histBody(n))}</div><small>${ago(n.ts)}</small></button>`).join('') : `<div class="empty">${t('noNotifs')}</div>`;
  const outcome = a => a.outcome === 'refused' ? `${t('outRefused')}: ${errText(a.reason)}` : a.outcome === 'confirm_requested' ? t('outConfirm') : a.outcome === 'timeout' ? `${t('outTimeout', {n: (/[\d.]+/.exec(a.reason || '') || [''])[0]})} · ${arrival(a)}` : `${t(DECIDED[a.decision] || 'sent')} · ${arrival(a)}`;
  const decisions = audit.items.length ? audit.items.map(a => `<div class="audit ${a.outcome === 'refused' ? 'no' : ''}"><b>${esc(a.project || a.pane)}</b><span>${esc(outcome(a))}</span>${a.command ? `<pre>${esc(a.command)}</pre>` : ''}<small>${esc(a.login || (a.outcome === 'timeout' ? 'Ulak' : ''))} · ${esc(new Date(a.ts * 1000).toLocaleString(LANG === 'tr' ? 'tr-TR' : 'en-GB', {dateStyle: 'medium', timeStyle: 'short'}))}</small></div>`).join('') : `<div class="empty">${t('noDecisions')}</div>`;
  showSheet(`<h3 class="sr-only">${esc(t('history'))}</h3><div class="tabs" role="tablist"><button role="tab" data-tab="notifications" aria-selected="true">${esc(t('notifications'))}</button><button role="tab" data-tab="decisions" aria-selected="false">${esc(t('decisions'))}</button></div>
    <div class="scroll" style="max-height:65vh" data-panel="notifications">${rows}</div><div class="scroll" style="max-height:65vh" data-panel="decisions" hidden>${decisions}</div>`);
  $('#sheet').querySelectorAll('[data-tab]').forEach(tab => tab.onclick = () => {
    $('#sheet').querySelectorAll('[data-tab]').forEach(x => x.setAttribute('aria-selected', String(x === tab)));
    $('#sheet').querySelectorAll('[data-panel]').forEach(x => { x.hidden = x.dataset.panel !== tab.dataset.tab; });
  });
  $('#sheet').querySelectorAll('[data-pane]').forEach(b => b.onclick = () => { const p = b.dataset.pane; hideSheet(); if (p && state.sessions.has(p)) go(p); });
});

function histTitle(n) { return n.project ? t(n.status === 'blocked' ? 'needsYouTitle' : 'doneTitle', {p: n.project}) : (n.title || ''); }
function histBody(n) { const b = n.body || ''; return n.project ? (b || t(n.status === 'blocked' ? 'askBody' : 'finishedBody')) : b; }

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
  // Three or more providers do not fit a phone with the window name; the name stays one tap away.
  const tight = ps.length > 2;
  box.classList.toggle('tight', tight);
  box.innerHTML = ps.map(p => {
    const show = [[...p.items].sort((a, b) => b.used - a.used)[0]];  // the tightest window; details on tap
    const stale = p.updated && now - p.updated > 1800;
    return `<span class="lim${stale ? ' stale' : ''}"><b>${esc(p.label)}</b>${show.map(i => `<span class="lv ${lvl(i.used)}" title="${esc(i.label)}"><i style="width:${Math.min(100, i.used)}%"></i></span><em>${tight ? '' : esc(i.label.split(' · ').pop()) + ' '}${i.used}%</em>`).join('')}</span>`;
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
  updateConnection();
  document.title = t('appName');
  setQuickLabel();
  state.sessions.size && renderHome();
  renderLimits();
  if (state.current) { updateSessionHeader(); $('#promptZone').dataset.sig = ''; state.chatHash = ''; loadChat(); }
}
async function toggleLang() {
  LANG = LANG === 'tr' ? 'en' : 'tr';
  store.set('lang', LANG);
  hideSheet(); refreshLang();
  try { const sub = await swReg?.pushManager.getSubscription(); if (sub) await api('/api/push/lang', {endpoint: sub.endpoint, lang: LANG}); } catch {}
}
function settingsSheet() {
  const bell = bellState === 'off' ? `<button id="bell" class="row">${icon('bell', 20)}<span>${esc(t('enableBell'))}</span></button>` : `<div class="row quiet">${icon('bell', 20)}<span>${esc(t(bellState === 'on' ? 'bellOn' : 'bellNone'))}</span></div>`;
  showSheet(`<h3 data-settings>${esc(t('settings'))}</h3><div class="rows">${bell}
    <button id="lang" class="row"><b class="lang">${LANG.toUpperCase()}</b><span>${esc(t('language'))}</span><small>${LANG === 'tr' ? 'English' : 'Türkçe'}</small></button>
    <button class="row" data-theme-toggle><svg class="moon" width="20" height="20" viewBox="0 0 24 24" aria-hidden="true"><defs><mask id="hilal2"><rect x="-4" y="-4" width="32" height="32" fill="#fff"/><circle class="bite" cx="16.4" cy="8.6" r="7.2" fill="#000"/></mask></defs><circle class="halo" cx="12" cy="12" r="9.5"/><circle class="disc" cx="12" cy="12" r="8.2" fill="currentColor" mask="url(#hilal2)"/></svg><span>${esc(t('appearance'))}</span><small><i class="when-dark">${esc(t('themeDark'))}</i><i class="when-light">${esc(t('themeLight'))}</i></small></button>
    <button id="refresh" class="row">${icon('refresh', 20)}<span>${esc(t('refresh'))}</span></button>
    ${approvalTimeout ? `<div class="row quiet timeout">${icon('timer', 20)}<span>${esc(t('timeoutOn', {n: approvalTimeout}))}</span></div>` : ''}</div>`);
  $('#sheet #lang').onclick = toggleLang;
  $('#sheet #refresh').onclick = e => act(e.currentTarget, loadList, t('refreshed'));
  const b = $('#sheet #bell'); if (b) b.onclick = e => enableBell(e.currentTarget);
}
$('#settings').onclick = settingsSheet;
setTimeout(() => $('#home .mark').classList.remove('draw'), 1300);  // drawn once per opening, not each time the home screen comes back
refreshLang();
loadLimits();
