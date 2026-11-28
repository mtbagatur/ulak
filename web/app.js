'use strict';
// Ulak v2 front end. No build step. Re-render only what changed: cards are patched by key,
// chat messages are appended, the composer and its draft are never touched by updates.
const $ = s => document.querySelector(s);
// The screens are repainted on a clock: write only what differs, so a quiet screen stays untouched.
const put = (el, prop, value) => { if (el[prop] !== value) el[prop] = value; };
const putHtml = (el, html) => { if (el._h !== html) { el.innerHTML = html; el._h = html; } };
const statusText = s => t('st_' + (['blocked', 'working', 'done', 'idle'].includes(s) ? s : 'unknown'));
const TOOLNAME = {claude: 'Claude', codex: 'Codex', agy: 'agy', terminal: 'Terminal'};
const store = {get(k){try{return localStorage.getItem(k)}catch{return null}}, set(k,v){try{localStorage.setItem(k,v)}catch{}}, del(k){try{localStorage.removeItem(k)}catch{}}, keys(){try{return Object.keys(localStorage)}catch{return []}}};

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
// A question in a session other than the one on screen: one line on top that carries the question, and a tap goes there.
// Not on the list (the card is already in view) and not for the open session (its question is on screen).
function dropCall(pane) { for (const el of $('#toasts').querySelectorAll('.call')) if (!pane || el.dataset.pane === pane) el.remove(); }
function callOut(s) {
  if (!s || !state.current || s.pane_id === state.current || document.hidden) return;
  dropCall(s.pane_id);
  const p = s.pending, el = document.createElement('button');
  // Text with characters that cannot be seen is not repeated here; the session shows it with its warning.
  const line = p && !(p.hidden && p.hidden.length) ? (p.details || p.question || '').split('\n').map(l => l.trim()).find(Boolean) : '';
  el.className = 'toast call'; el.dataset.pane = s.pane_id;
  el.innerHTML = `<b>${esc(s.project || s.pane_id)} · ${esc(t('st_blocked'))}</b><span>${esc((line || '').slice(0, 200))}</span>`;
  if (!line) el.lastChild.remove();
  el.onclick = () => { dropCall(s.pane_id); history.pushState({pane: s.pane_id}, '', `#/s/${s.pane_id}`); route(); };
  $('#toasts').appendChild(el);
  setTimeout(() => { if (!el.isConnected) return; el.classList.add('out'); setTimeout(() => el.remove(), 200); }, 7000);
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
    ...(card ? card.querySelectorAll('[data-act], [data-n]') : []),
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
// An idle session whose handoff is under way is listed with what just finished: the idle group is folded, and its countdown has a deadline.
const shelf = s => s.bucket === 'idle' && s.handoff && ['asked', 'written'].includes(s.handoff.phase) ? 'done' : s.bucket;
function sorted() {
  return [...state.sessions.values()].sort((a, b) => rank(a) - rank(b) || ((b.active || b.since) - (a.active || a.since)));
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
  up: '<path d="M12 18.5V5.5M6.5 11 12 5.5l5.5 5.5"/>',
  down: '<path d="M12 5.5v13M6.5 13l5.5 5.5 5.5-5.5"/>',
  chips: '<rect x="3.5" y="7.5" width="17" height="9" rx="4.5"/><path d="M8.5 12h7"/>',
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
  const unseen = p && p.hidden && p.hidden.length ? p.hidden.join(', ') : '';
  const inline = p && p.kind === 'approval' && !p.destructive && !p.cut && !unseen && p.roles.includes('approve') && shown.length <= 160 && shown.split('\n').length <= 3;
  // A short single choice is answered from the card; the server sends its options only when the whole question fits.
  const choices = p && p.kind === 'choice' && !unseen && p.options?.length ? p.options : [];
  const stateText = s.bucket === 'idle' && s.status === 'done' ? (ago(s.since) ? t('doneAgo', {t: ago(s.since)}) : statusText('done')) : [statusText(s.status), s.status === 'unknown' ? '' : ago(s.since)].filter(Boolean).join(' · ');
  const body = p ? `<pre class="c-cmd${inline ? ' full-command' : choices.length ? ' full-command asked' : ''}">${esc(p.details || p.question)}</pre>${unseen ? `<div class="hidden-note">${esc(t('hiddenShort', {c: unseen}))}</div>` : ''}` : (s.preview ? `<div class="c-prev">${esc(s.preview)}</div>` : '');
  const pinned = isPinned(s);
  return `<button class="card-open" type="button" aria-label="${esc(t('openSession', {project: s.project}))}"></button><div class="c-top"><span class="proj">${esc(s.project)}</span>${dup && s.workspace_label ? `<span class="ws">${esc(s.workspace_label)}</span>` : ''}<span class="chip ${esc(s.agent)}">${esc(TOOLNAME[s.agent] || s.agent)}</span><span class="model">${esc(s.model)}</span><button class="pin ${pinned ? 'on' : ''}" data-pin aria-label="${esc(t(pinned ? 'unpin' : 'pin'))}" aria-pressed="${pinned}">${icon('pin', 16)}</button></div>
    <div class="state"><span class="d"></span><span>${s.bucket === 'working' && stateText.startsWith(statusText('working')) ? `<span class="blink">${esc(statusText('working'))}</span>${esc(stateText.slice(statusText('working').length))}` : esc(stateText)}</span></div>${cardMeters(s)}
    ${s.title ? `<div class="c-title">${esc(s.title)}</div>` : ''}${p && p.destructive ? `<div class="seals">${icon('seal', 14)}${icon('seal', 14)}<span>${esc(t('destructive'))}</span></div>` : ''}${body}
    ${inline ? `<div class="c-act"><button class="deny" data-act="deny">${esc(t('deny'))}</button><button class="seal" data-act="approve">${icon('seal', 17)}${esc(t('approve'))}</button></div>` : ''}${choices.length ? `<div class="c-act choices">${choices.map(o => `<button data-n="${+o.n}">${esc(o.label)}</button>`).join('')}</div>` : ''}${isSealed(s.pane_id, p) ? stampHtml(p) : ''}`;
}
// While a handoff is under way the line leads with it and its way out sits at the end, a button of its own above the card's.
function cardMeters(s) {
  const line = meterLine(s.meters, true, s.handoff);
  if (!line) return '';
  return handoffText(s.handoff) ? `<div class="c-meters has-x"><span>${line}</span><button type="button" class="hand-x" data-handoff-cancel>${esc(t('cancel'))}</button></div>` : `<div class="c-meters">${line}</div>`;
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
function setTitle(waiting = [...state.sessions.values()].filter(s => s.bucket === 'blocked').length) { const text = (waiting ? `(${waiting}) ` : '') + t('appName'); if (document.title !== text) document.title = text; }
function renderHome() {
  const hold = (state.sealHold || 0) - performance.now();
  if (hold > 0 && $('#groups .stamp')) { clearTimeout(renderHome.held); renderHome.held = setTimeout(renderHome, hold); return; }
  const all = sorted();
  const blocked = all.filter(s => s.bucket === 'blocked').length;
  const unknown = all.filter(s => s.bucket === 'unknown').length;
  putHtml($('#summary'), !all.length ? '' : blocked ? t('summaryBlocked', {n: blocked}) : unknown ? t('summaryUnknown', {n: unknown}) : t('summaryCalm', {n: all.filter(s => s.bucket === 'working').length}));
  setTitle(blocked);  // the one sign that shows without notification permission
  if (navigator.setAppBadge) { blocked ? navigator.setAppBadge(blocked).catch(() => {}) : navigator.clearAppBadge?.().catch(() => {}); }
  const groups = $('#groups');
  if (!all.length) { putHtml(groups, `<div class="empty">${t('empty')}</div>`); return; }
  if (groups.querySelector('.empty')) { groups.innerHTML = ''; groups._h = ''; }
  const pinned = all.filter(isPinned);
  const defs = [...(pinned.length ? [{id: 'pinned', title: t('g_pinned'), items: pinned}] : []), ...GROUPS.map(id => ({id, title: t('g_' + id), items: all.filter(s => !isPinned(s) && shelf(s) === id)}))].filter(g => g.items.length);
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
    put(wrap, 'className', `group g-${g.id}`);
    const box = wrap.querySelector('.cards');
    put(box, 'hidden', g.id === 'idle' && !state.idleOpen);
    if (g.id === 'idle') {
      const toggle = wrap.querySelector('[data-toggle]');
      if (toggle.getAttribute('aria-expanded') !== String(state.idleOpen)) toggle.setAttribute('aria-expanded', String(state.idleOpen));
      put(toggle.querySelector('span'), 'textContent', t(state.idleOpen ? 'hide' : 'show'));
    }
    patch(box, g.items, s => s.pane_id, cardHtml, s => { const b = document.createElement('div'); b._base = 'card'; return b; });
    for (const el of box.children) { const s = state.sessions.get(el.dataset.key); put(el, 'className', `card b-${s.bucket}${s.pending?.destructive ? ' two-seals' : ''}${isSealed(s.pane_id, s.pending) ? ' stamped' : ''}`); }
  }
  for (const pane of state.busy) setPaneActionsBusy(pane, true);
}
$('#groups').addEventListener('click', e => {
  if (e.target.closest('[data-toggle]')) { state.idleOpen = !state.idleOpen; store.set('idleOpen', state.idleOpen ? '1' : '0'); renderHome(); return; }
  const card = e.target.closest('.card'); if (!card) return;
  const pb = e.target.closest('[data-pin]');
  if (pb) { e.stopPropagation(); const s = state.sessions.get(card.dataset.key); if (s) { const pane = card.dataset.key, k = pinKey(s); pins.has(k) ? pins.delete(k) : pins.add(k); renderHome(); savePins(); requestAnimationFrame(() => [...document.querySelectorAll('.card')].find(el => el.dataset.key === pane)?.querySelector('[data-pin]')?.focus({preventScroll: true})); } return; }
  const pane = card.dataset.key, a = e.target.closest('[data-act]');
  if (e.target.closest('[data-handoff-cancel]')) { e.stopPropagation(); cancelHandoff(pane, e.target.closest('[data-handoff-cancel]')); return; }
  if (a) { e.stopPropagation(); inlineDecide(pane, a.dataset.act, a); return; }
  const c = e.target.closest('[data-n]');
  if (c) { e.stopPropagation(); cardChoose(pane, +c.dataset.n, c); return; }
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
async function cardChoose(pane, n, btn) {
  const s = state.sessions.get(pane); if (!s || !s.pending) return;
  const view = state.view, fingerprint = s.pending.fingerprint;
  const unlock = beginPaneAction(pane); if (!unlock) return;
  try {
    const r = await act(btn, () => api(`/api/sessions/${encodeURIComponent(pane)}/choose`, {n, fingerprint}));
    if (r?.ok) await watchApprovalReply(pane, fingerprint, view);
    else loadList();
  } finally { unlock(); }
}

// ---------- data ----------
let es = null, streamReady = false, retryTimer = null, retries = 0;
let snapshotGeneration = 0, listReady = false, chatReadyView = -1, networkError = false, failReason = '';
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
      pruneOutbox();
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
  if (state.live || listReady) failReason = '';
  const text = state.live ? '' : offline ? t('offline') : failReason || t(streamReady ? 'syncing' : 'reconnecting');
  put(banner, 'hidden', state.live);
  banner.classList.toggle('bad', offline || !!failReason);
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
  // A server that answers with a reason (Herdr is down, say) is worth quoting; a bare status number is not.
  failReason = error.status && !/^\d+$/.test(error.message) ? error.message : '';
  updateConnection();
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
    const was = state.sessions.get(pane);
    if (session) state.sessions.set(pane, session);
    else state.sessions.delete(pane);
    outbox.set(pane, session ? (outbox.get(pane) || []).filter(item => ownMail(pane, item)) : []); saveOutbox(pane);
    renderHome();
    if (session?.bucket !== 'blocked') dropCall(pane);
    else if (was?.bucket !== 'blocked' || was.pending?.fingerprint !== session.pending?.fingerprint) callOut(session);
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
// A keyboard user's focus must not stay behind on a screen that just went away. Nothing is focused for someone who was not already focused there.
const focusIsIn = sel => !!document.activeElement?.closest(sel);
function showHome() {
  state.view++;
  const from = focusIsIn('#session') && state.current;
  state.current = null; state.prompt = null; $('#session').hidden = true; $('#home').hidden = false;
  $('#promptZone').innerHTML = ''; $('#promptStatus').textContent = ''; hideSheet(); dropCall();
  updateConnection();
  if (from) ([...document.querySelectorAll('.card')].find(el => el.dataset.key === from)?.querySelector('.card-open') || $('#newSession')).focus({preventScroll: true});
}

// ---------- limits ----------
let limitData = null;
const lvl = used => used >= 85 ? 'hot' : used >= 60 ? 'warn' : 'ok';
function fmtIn(secs) {
  if (secs <= 0) return t('justNow');
  const d = Math.floor(secs / 86400), h = Math.floor(secs % 86400 / 3600), m = Math.floor(secs % 3600 / 60);
  return d ? `${d}${t('day')} ${h}${t('hour')}` : h ? `${h}${t('hour')} ${m}${t('min')}` : `${Math.max(1, m)}${t('min')}`;
}
// A window kept for one model goes by the model's name: "7d 88%" would read as the whole account's week.
const winName = i => i.model ? String(i.label).split(' · ')[0] : String(i.label).split(' · ').pop();
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
    return `<span class="lim${stale ? ' stale' : ''}"><b>${esc(p.label)}</b>${show.map(i => `<span class="lv${stale ? '' : ' ' + lvl(i.used)}" title="${esc(i.label)}"><i style="width:${Math.min(100, i.used)}%"></i></span><em>${tight ? '' : esc(winName(i)) + ' '}${i.used}%</em>`).join('')}</span>`;
  }).join('');
}
function limitsSheet() {
  const now = Date.now() / 1000;
  const ps = limitData && limitData.providers || [];
  const body = ps.length ? ps.map(p => {
    const stale = p.updated && now - p.updated > 1800;
    return `<section class="lp"><h4>${esc(p.label)}${stale ? `<small>${esc(t('stale', {t: fmtIn(now - p.updated)}))}</small>` : p.updated ? `<small class="fresh">${esc(t('readAgo', {t: fmtIn(now - p.updated)}))}</small>` : ''}</h4>${p.items.map(i => `<div class="li"><div class="lt"><span>${esc(i.label)}</span><span>${esc(t('used', {n: i.used}))} · ${esc(t('left', {n: Math.max(0, 100 - i.used)}))}</span></div><div class="lv big${stale ? '' : ' ' + lvl(i.used)}"><i style="width:${Math.min(100, i.used)}%"></i></div><small>${esc(resetText(i.resets_at, now))}</small></div>`).join('')}</section>`;
  }).join('') : `<div class="empty">${esc(t('noLimits'))}</div>`;
  showSheet(`<h3>${esc(t('limitsTitle'))}</h3><div class="scroll" style="max-height:65vh">${body}</div>`);
}
async function loadLimits() {
  try { limitData = await api('/api/limits'); renderLimits(); renderMeters(); } catch {}
}
// ---------- what this session has used: context, cache, limit ----------
// Only what is known is shown. A number nobody measured is left out, not guessed.
const fmtTokens = n => n < 1000 ? String(n) : n < 999500 ? Math.round(n / 1000) + 'k' : (n / 1e6).toFixed(1).replace(/\.0$/, '') + 'M';
const serverNow = () => Date.now() / 1000 + (state.skew || 0);
const agoText = secs => secs < 60 ? t('justNow') : t('agoT', {t: fmtIn(secs)});
const cacheMins = left => Math.max(1, Math.floor(left / 60));
function cacheLeft(meters = state.meters) {
  const c = meters?.cache;
  return c && typeof c.ttl === 'number' && typeof c.since === 'number' ? c.ttl - (serverNow() - c.since) : null;
}
// The limit is the account's, for the tool this session runs. A window whose reset time has passed says nothing any more,
// and a window kept for one model is this session's only when it runs that model.
function sessionLimits() {
  const s = state.sessions.get(state.current), agent = s?.agent, now = serverNow(), model = String(s?.model || '').toLowerCase();
  const p = (limitData?.providers || []).find(p => p.id === agent);
  const items = (p?.items || []).filter(i => typeof i.used === 'number' && !(typeof i.resets_at === 'number' && i.resets_at <= now) && (!i.model || model.includes(String(i.model).toLowerCase())));
  return items.length ? {id: p.id, label: p.label, updated: p.updated, items} : null;
}
// The same words above a conversation and on its card. Nothing known, nothing said.
// The card says the count short: at 320 px the long form pushes the cache off the line.
// What an automatic handoff under way says of itself, or nothing. The minutes are the computer's, rounded up; a late server never shows less than "shortly".
function handoffText(h) {
  if (h?.phase === 'asked') return t('hoAsked');
  if (h?.phase !== 'written') return '';
  const left = typeof h.due === 'number' ? h.due - serverNow() : 0;
  return left < 60 ? t('hoSoon') : t('hoWritten', {n: Math.ceil(left / 60)});
}
const handCancelling = new Set();
async function cancelHandoff(pane, btn) {
  if (handCancelling.has(pane)) return;
  handCancelling.add(pane);
  try { applySession(await act(btn, () => api(`/api/sessions/${encodeURIComponent(pane)}/handoff/cancel`, {}), t('hoCancelled'))); }
  finally { handCancelling.delete(pane); }
}
// The server answers a handoff call with the session as it now stands.
function applySession(card) {
  if (!card?.pane_id || !state.sessions.has(card.pane_id)) return;
  state.sessions.set(card.pane_id, {...state.sessions.get(card.pane_id), ...card});
  renderHome();
  if (card.pane_id === state.current) updateSessionHeader();
}
function meterLine(meters, short, handoff) {
  const m = meters || {}, left = cacheLeft(m), bits = [], out = m.agents?.running, hand = handoffText(handoff);
  // The handoff leads: it is the one part with a deadline, and at a narrow width the end is what gets cut.
  if (hand) bits.push(`<span data-handoff>${esc(hand).replace(/\d+/, '<b>$&</b>')}</span>`);
  // First in the line: at a narrow width the end is what gets cut, and this is the part that changes.
  if (Number.isInteger(out) && out > 0) bits.push(`<span data-agents>${esc(many(short ? 'agentsShort' : 'agentsRunning', out)).replace(/\d+/, '<b>$&</b>')}</span>`);
  if (typeof m.context?.tokens === 'number') bits.push(t('ctxShort', {n: `<b>${fmtTokens(m.context.tokens)}</b>`}));
  if (left !== null) bits.push(left > 0 ? t('cacheShort', {n: `<b>${t('cacheMins', {n: cacheMins(left)})}</b>`}) : esc(t('cacheCold')));
  return bits.join(' · ');
}
function renderMeters() {
  const meta = $('#sMeta'), ring = $('#sRing'), hand = state.sessions.get(state.current)?.handoff, live = handoffText(hand);
  // A Claude session with nothing measured yet still has the switch: the line names it, so the sheet can be reached.
  const line = meterLine(state.meters, false, hand) || (hand ? esc(t(hand.on ? 'hoOn' : 'hoOff')) : '');
  put(meta, 'hidden', !line); putHtml(meta, `<span>${line}</span>`);
  // The header line is 94 px wide on a narrow phone with the ring up, so the countdown is said whole above the message box, its way out beside it.
  put($('#handBar'), 'hidden', !live); put($('#handBar span'), 'textContent', live); put($('#handBar button'), 'textContent', t('cancel'));
  const lim = sessionLimits();
  put(ring, 'hidden', !lim);
  if (!lim) return;
  const top = lim.items.reduce((a, b) => b.used > a.used ? b : a), used = Math.max(0, Math.min(100, Math.round(top.used)));
  const name = winName(top), short = top.model ? '' : {'5h': 'win5h', '7d': 'win7d'}[name];
  ring.classList.toggle('stale', !!lim.updated && serverNow() - lim.updated > 1800);
  ring.setAttribute('aria-label', `${lim.label} ${top.label}: ${t('used', {n: top.used})}`);
  putHtml(ring, `<span class="o"><svg viewBox="0 0 36 36" aria-hidden="true"><circle cx="18" cy="18" r="16"/><circle class="fill" cx="18" cy="18" r="16" pathLength="100" stroke-dasharray="${used} 100"/></svg><b>${used}</b></span><small>${esc(short ? t(short) : name)}</small>`);
}
function metersSheet() {
  const m = state.meters || {}, now = serverNow(), left = cacheLeft(), lim = sessionLimits(), rows = [];
  const row = (name, value, note, bar) => `<div class="meter"><div class="l"><span>${esc(name)}</span><span>${esc(value)}</span></div>${bar === undefined ? '' : `<div class="bar"><i style="width:${bar}%"></i></div>`}<small>${esc(note)}</small></div>`;
  const record = ts => typeof ts === 'number' ? `${t('fromRecord')}, ${agoText(now - ts)}` : t('fromRecord');
  if (typeof m.context?.tokens === 'number') rows.push(row(t('mContext'), t('tokens', {n: m.context.tokens.toLocaleString(LANG === 'tr' ? 'tr-TR' : 'en-US')}), record(m.context.ts)));
  if (left !== null) rows.push(row(`${t('mCache')} · ${t(m.cache.ttl >= 3600 ? 'cache1h' : 'cache5m')}`, left > 0 ? t('cacheLeft', {n: cacheMins(left)}) : t('cacheCold'), record(m.cache.since)));
  for (const i of lim?.items || []) rows.push(row(`${lim.label} · ${i.label}`, t('used', {n: i.used}), [resetText(i.resets_at, now), t(lim.id === 'codex' ? 'limCodex' : 'limFile'), lim.updated ? agoText(now - lim.updated) : ''].filter(Boolean).join(' · '), Math.max(0, Math.min(100, i.used))));
  const pane = state.current, hand = state.sessions.get(pane)?.handoff;
  if (hand) rows.push(`<button type="button" class="switch" role="switch" aria-checked="${!!hand.on}"><span>${esc(t('hoSwitch'))}</span><i></i></button><small class="switch-note">${esc(t('hoNote', {n: +(handoffGrace / 60).toFixed(1)}))}</small>`);
  if (!rows.length) return;
  showSheet(`<h3>${esc(t('metersTitle'))}</h3><div class="scroll" style="max-height:65vh">${rows.join('')}</div>`);
  const sw = $('#sheet [role=switch]');
  if (sw) sw.onclick = async () => {
    const on = sw.getAttribute('aria-checked') !== 'true';
    sw.disabled = true; sw.setAttribute('aria-checked', String(on));
    let card = null;
    try { card = await api(`/api/sessions/${encodeURIComponent(pane)}/handoff`, {on}, 'PUT'); }
    catch (e) { toast(e.message, 'err'); }
    if (sw.isConnected) { sw.setAttribute('aria-checked', String(card ? !!card.handoff?.on : !on)); sw.disabled = false; }
    applySession(card);
  };
}
$('#sRing').onclick = metersSheet;
$('#handBar button').onclick = e => cancelHandoff(state.current, e.currentTarget);
// One line, two answers: the count takes you to whoever is still out, the measures open their sheet.
// The count's own letters are 12 px tall, so it is told apart by where across the line the finger landed.
$('#sMeta').onclick = e => {
  const a = $('#sMeta [data-agents]');
  if (!a || !(e.target.closest('[data-agents]') || e.detail && e.clientX <= a.getBoundingClientRect().right + 6)) return metersSheet();
  [...$('#messages').querySelectorAll('details.sub[data-state=running]')].at(-1)?.scrollIntoView({block: 'center', behavior: matchMedia('(prefers-reduced-motion: reduce)').matches ? 'auto' : 'smooth'});
};
setInterval(() => { if (!document.hidden && state.current) renderMeters(); }, 60000);
$('#limits').onclick = limitsSheet;
setInterval(() => { if (!document.hidden) loadLimits(); }, 60000);
document.addEventListener('visibilitychange', () => { if (!document.hidden) loadLimits(); });

// ---------- session screen ----------
function openSession(pane) {
  if (state.current === pane && !$('#session').hidden) return;
  hideSheet(); dropCall(pane);
  const carry = focusIsIn('#home');
  state.view++;
  state.current = pane; state.chatHash = ''; state.prompt = null; openRuns.clear(); state.meters = {}; renderMeters();
  state.anchor = state.cursor = state.start = null; state.conversation = undefined; state.found = null; $('#sSearch').hidden = true;
  loadSlash(pane);
  $('#home').hidden = true; $('#session').hidden = false;
  if (carry) $('#back').focus({preventScroll: true});
  $('#messages').innerHTML = `<div class="empty">${t('loading')}</div>`; $('#promptZone').innerHTML = ''; $('#keybar').hidden = true;
  delete $('#promptZone').dataset.sig;
  $('#receipt').hidden = true;
  $('#promptStatus').textContent = '';
  $('#message').value = store.get('draft:' + pane) || ''; autosize();
  updateSessionHeader(); loadChat(true);
}
function updateSessionHeader() {
  const s = state.sessions.get(state.current); if (!s) return;
  put($('#sTitle'), 'textContent', s.project);
  put($('#sSub'), 'textContent', [TOOLNAME[s.agent] || s.agent, s.model, statusText(s.status), ago(s.since)].filter(Boolean).join(' · '));
  // A pane with no agent in it is a shell: it can be read, the server refuses to type into it.
  const plain = isPlain(s);
  put($('#composer'), 'hidden', plain); put($('#plainNote'), 'hidden', !plain);
  if (plain) $('#keybar').hidden = true;
  renderMeters();
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
// A link in a transcript opens on the web, or reads a file of the session's own folder. Anything else stays text.
const fileUrl = path => `/api/sessions/${encodeURIComponent(state.current)}/file?path=${encodeURIComponent(path)}`;
const fileLink = (label, href) => {
  const external = /^https?:\/\//i.test(href);
  let target = href;
  if (!external) {
    const suffix = href.split(/[?#]/, 1)[0].split('.').pop().toLowerCase();
    if (!/^(pdf|png|jpe?g|webp|gif|heic|txt|md|csv|json)$/.test(suffix) || /^(?:[a-z]+:|\/\/)/i.test(href)) return null;
    target = fileUrl(href);
  }
  return `<a href="${esc(target)}" target="_blank" rel="noopener noreferrer">${esc(label)}</a>`;
};
const md = value => inlineMd(value, fileLink);
// What an agent wrote is markdown; what you typed and what the terminal shows stay as they are.
const rich = value => renderMarkdown(value, {link: fileLink, copyLabel: t('copy')});
document.addEventListener('click', async e => {
  const button = e.target.closest('.code button.copy');
  if (!button) return;
  try {
    await navigator.clipboard.writeText(button.closest('.code').querySelector('code').textContent);
    button.textContent = t('copied');
    setTimeout(() => { button.textContent = t('copy'); }, 2000);
  } catch { toast(t('copyFail'), 'err'); }
});
// A subagent is not one more tool line: it is someone sent off on an errand, so it gets a card that says what for and whether it is back.
function subNode(d, m) {
  const state = ['running', 'done', 'failed', 'unknown'].includes(m.agent.state) ? m.agent.state : 'running';
  d.className = 'tool sub'; d.dataset.state = state;
  d.innerHTML = `<summary><span class="sub-mark" aria-hidden="true"></span><span class="sub-body"><span class="sub-kind">${esc(t('subagent') + (m.agent.kind ? ' · ' + m.agent.kind : ''))}</span><span class="sub-what">${esc(m.text || t('subagent'))}</span></span><span class="sub-state">${esc(t({running: 'subRunning', done: 'subDone', failed: 'subFailed', unknown: 'noResult'}[state]))}</span></summary>${m.result ? `<div class="md">${rich(m.result)}</div>` : ''}`;
  return d;
}
const subSig = m => m.agent ? `${m.agent.state}|${(m.result || '').length}` : '';
// ---------- the conversation as keyed pieces ----------
// Every piece has a key that survives the next reading of the transcript, so what is on screen is kept and only what changed is redrawn.
const KINDS = {Read: 'kRead', NotebookRead: 'kRead', Grep: 'kSearch', Glob: 'kSearch', WebSearch: 'kSearch', ToolSearch: 'kSearch', Edit: 'kEdit', MultiEdit: 'kEdit', Write: 'kEdit', NotebookEdit: 'kEdit', Bash: 'kCommand', WebFetch: 'kPage'};
const openRuns = new Set();  // tool ids of the runs you opened: they stay open while the conversation moves
const many = (key, n) => { const forms = t(key).split('|'); return (n === 1 ? forms[0] : forms.at(-1)).replace('{n}', n); };
const firstLine = text => String(text || '').split('\n')[0];
const callName = m => [m.tool || t('tool'), firstLine(m.text)].filter(Boolean).join(' ');
const counted = m => typeof m.added === 'number' && typeof m.removed === 'number';
const plusMinus = (added, removed) => `<span class="add">+${added}</span> <span class="del">−${removed}</span>`;
const toolSig = m => [m.id, m.state, (m.text || '').length, (m.result || '').length, m.added, m.removed, m.lines, subSig(m)].join('|');
function itemsOf(messages, source) {
  const items = [], seen = new Map();
  let run = null;
  for (const m of messages) {
    // Only the transcript says how a call ended; without that nothing is folded.
    const call = source === 'transcript' && m.role === 'tool' && !m.agent && m.id && m.state;
    if (call && m.state !== 'failed' && m.state !== 'rejected') {
      if (!run) items.push(run = {kind: 'run', key: 't:' + m.id, messages: []});
      run.messages.push(m);
      continue;
    }
    run = null;
    if (call) { items.push({kind: 'card', key: 't:' + m.id, messages: [m]}); continue; }
    const text = m.text || '';
    const base = m.role === 'tool' && m.id ? 't:' + m.id : `${m.role}|${m.tool || ''}|${m.ts || ''}|${text.length}|${text.slice(-24)}`;
    const nth = seen.get(base) || 0; seen.set(base, nth + 1);  // the same words said twice are two messages
    items.push({kind: m.agent ? 'sub' : 'msg', key: `${base}#${nth}`, messages: [m]});
  }
  for (const item of items) { item.source = source; item.sig = [item.kind, source, LANG, ...item.messages.map(m => m.role === 'tool' ? toolSig(m) : '')].join('~'); }
  return items;
}
function runLabel(calls) {
  const done = calls.filter(m => m.state !== 'running'), running = calls.findLast(m => m.state === 'running'), counts = new Map();
  for (const m of done) { const k = KINDS[m.tool] || 'kOther'; counts.set(k, (counts.get(k) || 0) + 1); }
  let html = [...counts].map(([k, n]) => esc(many(k, n))).join(', ');
  const edits = done.filter(m => m.state === 'ok' && counted(m));
  if (edits.length) html += ` · ${plusMinus(edits.reduce((n, m) => n + m.added, 0), edits.reduce((n, m) => n + m.removed, 0))}`;
  if (running) html += (html ? ' · ' : '') + esc(t('runNow', {x: callName(running)}));
  return html;
}
function stepHtml(m) {
  const diff = Array.isArray(m.diff) && m.diff.length ? `<div class="diff">${m.diff.map(([kind, line]) => `<div${kind === '+' ? ' class="a"' : kind === '-' ? ' class="d"' : kind === '…' ? ' class="gap"' : ''}>${kind === '…' ? '…' : esc(line) || ' '}</div>`).join('')}</div>` : '';
  const notes = [m.state === 'unknown' ? t('noResult') : '', typeof m.lines === 'number' ? many('wrote', m.lines) : '', diff && m.diff_cut ? t('editCut') : ''].filter(Boolean);
  const text = m.text || '';
  return `<div class="h"><b>${esc(m.tool || t('tool'))}</b><span>${md(firstLine(text))}</span>${counted(m) ? `<em class="cnt">${plusMinus(m.added, m.removed)}</em>` : ''}</div>${diff || (m.result || text.includes('\n') ? `<pre class="out">${md(m.result || text)}</pre>` : '')}${notes.map(n => `<small>${esc(n)}</small>`).join('')}`;
}
function fillRun(node, item) {
  const calls = item.messages, open = calls.some(m => openRuns.has(m.id));
  if (open) calls.forEach(m => openRuns.add(m.id));
  if (!node.firstChild) node.innerHTML = '<button type="button"><span class="chev" aria-hidden="true">›</span><span class="sum"></span></button><div class="fold"><div><div class="box"></div></div></div>';
  node.className = open ? 'run open' : 'run'; node._ids = calls.map(m => m.id);
  node.querySelector('button').setAttribute('aria-expanded', open);
  putHtml(node.querySelector('.sum'), runLabel(calls));
  const box = node.querySelector('.box'), old = new Map([...box.children].map(el => [el.dataset.id, el]));
  box.replaceChildren(...calls.map(m => {
    const sig = toolSig(m) + LANG;
    let el = old.get(m.id);
    if (!el) { el = document.createElement('div'); el.className = 'step'; el.dataset.id = m.id; }
    if (el._sig !== sig) { el.innerHTML = stepHtml(m); el._sig = sig; }
    return el;
  }));
}
// What failed and what you refused are never folded away.
function fillCard(node, m) {
  const refused = m.state === 'rejected';
  node.className = refused ? 'act denied' : 'act bad';
  node.innerHTML = `<div class="h"><b>${esc(t(refused ? 'rejectedCall' : 'failedCall'))}</b><span>${esc(callName(m))}</span></div>${refused ? `<p class="why">${esc(t('didNotRun'))}</p>` : ''}${m.result ? `<pre class="out">${md(m.result)}</pre>` : ''}`;
}
function fill(node, item) {
  const m = item.messages[0];
  if (item.kind === 'run') fillRun(node, item);
  else if (item.kind === 'card') fillCard(node, m);
  else if (item.kind === 'sub') subNode(node, m);
  else if (m.role === 'tool') {
    node.className = 'tool';
    node.innerHTML = `<summary><b>${esc(m.tool || t('tool'))}</b><span>${md(m.text.split('\n')[0])}</span></summary>${m.result || m.text.includes('\n') ? `<pre>${md(m.result || m.text)}</pre>` : ''}`;
  } else {
    const blocks = item.source === 'transcript' && m.role === 'assistant';
    node.className = `msg ${m.role}${blocks ? ' md' : ''}`; node.innerHTML = m.role === 'command' ? esc(m.text) : blocks ? rich(m.text) : md(m.text);
    const paths = m.role === 'assistant' || m.role === 'user' ? imagePaths(m.text) : [];
    if (paths.length) { const box = document.createElement('div'); box.className = 'shots'; box.append(...paths.map(shotNode)); node.append(box); }
  }
  node._sig = item.sig;
}
// ---------- pictures ----------
// A picture a message points at is shown under it, read through the same /file rules as its link. Nothing remote is fetched.
function imagePaths(text) {
  const lines = [], found = new Set();
  let fenced = false;
  for (const line of String(text || '').split('\n')) {
    if (/^[ \t]*```/.test(line)) fenced = !fenced;
    else if (!fenced) lines.push(line);
  }
  for (const m of lines.join('\n').matchAll(/\]\(([^)\s]{1,500})\)|(?:^|[^\w.\-\/])((?:\/|\.\.?\/|[\w.\-]+\/)[^\s<>"'`]+)/g)) {
    const path = m[1] || splitTrailing(m[2])[0];
    if (/\.(?:png|jpe?g|webp|gif|heic)$/i.test(path) && !/^(?:[a-z]+:|\/\/)/i.test(path)) found.add(path);
    if (found.size === 4) break;
  }
  return [...found];
}
function shotNode(path) {
  const b = document.createElement('button'), name = path.split('/').pop();
  b.type = 'button'; b.className = 'shot'; b.setAttribute('aria-label', t('openImage', {x: name}));
  b.innerHTML = `<img loading="lazy" alt="" src="${esc(fileUrl(path))}"><small>${esc(name)}</small>`;
  // What cannot be shown goes away with its box; the link in the text stays.
  b.firstChild.addEventListener('error', () => {
    const drop = () => { const box = b.parentElement; b.remove(); if (box && !box.children.length) box.remove(); };
    const scroller = b.closest('#messages');
    scroller ? keepPlace(scroller, drop) : drop();
  });
  return b;
}
let viewerOpener = null;
function openViewer(img, opener) {
  const v = $('#viewer');
  viewerOpener = opener;
  v.querySelector('img').src = img.getAttribute('src');
  v.hidden = false; $('main').inert = true;
  history.pushState({...history.state, viewer: true}, '');
  $('#viewerClose').focus({preventScroll: true});
}
function hideViewer() {
  const v = $('#viewer');
  if (v.hidden) return;
  v.hidden = true; $('main').inert = false; v.querySelector('img').removeAttribute('src');
  if (viewerOpener?.isConnected) viewerOpener.focus({preventScroll: true});
  viewerOpener = null;
}
// Closing is going back: the phone's back gesture and the button do the same thing.
const closeViewer = () => history.state?.viewer ? history.back() : hideViewer();
window.addEventListener('popstate', () => { if (!history.state?.viewer) hideViewer(); });
$('#viewer').addEventListener('click', e => { if (e.target.tagName !== 'IMG') closeViewer(); });
document.addEventListener('keydown', e => { if (e.key === 'Escape' && !$('#viewer').hidden) { e.preventDefault(); e.stopPropagation(); closeViewer(); } }, true);  // before the sheet under it hears the key
document.addEventListener('click', e => { const shot = e.target.closest('.shot'); if (shot) openViewer(shot.querySelector('img'), shot); });
function buildNode(item) {
  const node = document.createElement(item.kind === 'sub' || (item.kind === 'msg' && item.messages[0].role === 'tool') ? 'details' : 'div');
  node._key = item.key; node._kind = item.kind;
  fill(node, item);
  return node;
}
const nodesFor = (messages, source) => itemsOf(messages, source).map(buildNode);
document.addEventListener('click', e => {
  const button = e.target.closest('.run > button');
  if (!button) return;
  const run = button.parentElement, open = run.classList.toggle('open');
  button.setAttribute('aria-expanded', open);
  (run._ids || []).forEach(id => open ? openRuns.add(id) : openRuns.delete(id));
});
// Whatever the drawing adds or removes, the line you were reading stays where it was.
function keepPlace(box, draw, toEnd = false) {
  const top = box.getBoundingClientRect().top, bottom = top + box.clientHeight, seen = [];
  if (!toEnd) for (const el of box.querySelectorAll(':scope > .past > *, :scope > .live > *')) {
    const r = el.getBoundingClientRect();
    if (r.top >= bottom) break;
    if (r.bottom > top) seen.push([el, r.top]);
  }
  draw();
  const held = seen.find(([el]) => el.isConnected);
  if (toEnd) box.scrollTop = box.scrollHeight;
  else if (held) box.scrollTop += held[0].getBoundingClientRect().top - held[1];
}
// ---------- outbox: what you sent shows at once and stays until the conversation carries it ----------
const outbox = new Map();  // pane -> [{id, text, status: 'sending' | 'sent' | 'unseen' | 'unsent', seen: string[] | null, count, conversation, born, ts}]; seen === null: no baseline taken yet
const OUTBOX = 'outbox:', kept = new Map(), written = new WeakMap();  // written: record -> the view it was sent from
// A pane id is given out again; the mark on its card is not. A record with another mark was written to a pane that has closed; one without a mark takes this pane's.
function ownMail(pane, item) {
  const born = state.sessions.get(pane)?.born || '';
  if (item.born && born && item.born !== born) return false;
  if (!item.born && born) item.born = born;
  return true;
}
const gist = text => String(text || '').replace(/\s+/g, ' ').trim().slice(0, 60);
const copies = (messages, text) => messages.filter(m => (m.role === 'user' || m.role === 'command') && gist(m.text) === gist(text));
const copyKey = m => `${m.role}|${m.ts}|${m.text}`;
const MAIL = {sending: 'mailSending', sent: 'mailSent', unseen: 'mailUnseen', unsent: 'mailUnsent'};
const stalled = item => item.status === 'unsent' || item.status === 'unseen';
function saveOutbox(pane) {
  const mail = outbox.get(pane) || [], text = mail.length ? JSON.stringify(mail) : '';
  if (!mail.length) outbox.delete(pane);
  if (kept.get(pane) === text) return;
  kept.set(pane, text);
  text ? store.set(OUTBOX + pane, text) : store.del(OUTBOX + pane);
}
// What was still on its way when the app closed may or may not have left: it comes back as "not sent" and is never sent again on its own.
function loadOutbox() {
  for (const key of store.keys()) {
    if (!key.startsWith(OUTBOX)) continue;
    let mail = []; try { mail = JSON.parse(store.get(key)); } catch {}
    mail = (Array.isArray(mail) ? mail : []).filter(x => x && typeof x.text === 'string' && x.text)
      .map(x => ({...x, status: x.status === 'unseen' || x.status === 'sent' ? 'unseen' : 'unsent', seen: Array.isArray(x.seen) ? x.seen : null}));
    const pane = key.slice(OUTBOX.length);
    outbox.set(pane, mail); saveOutbox(pane);
  }
}
// A pane that is gone takes its outbox with it: its id may be given to another session.
function pruneOutbox() {
  for (const pane of [...outbox.keys()]) { outbox.set(pane, state.sessions.has(pane) ? outbox.get(pane).filter(item => ownMail(pane, item)) : []); saveOutbox(pane); }
  renderOutbox();
}
function renderOutbox() {
  const box = $('#messages');
  // Stored text waits for this view's conversation check; a message just sent here belongs to this view.
  const mail = (outbox.get(state.current) || []).filter(item => state.settled === state.view || written.get(item) === state.view);
  box.querySelectorAll('.msg.pending').forEach(el => { if (!mail.includes(el._item)) el.remove(); });
  for (const item of mail) {
    let el = [...box.querySelectorAll('.msg.pending')].find(e => e._item === item);
    if (!el) { el = document.createElement('div'); el.className = 'msg user pending'; el._item = item; el.innerHTML = '<span class="body"></span><small></small><span class="mail-acts"><button type="button" data-mail="retry"></button><button type="button" data-mail="drop"></button></span>'; el.querySelector('.body').textContent = item.text; }
    if (el !== box.lastElementChild || el.parentElement !== box) box.appendChild(el);
    el.dataset.status = item.status;
    put(el.querySelector('small'), 'textContent', t(MAIL[item.status]));
    put(el.querySelector('.mail-acts'), 'hidden', !stalled(item));
    put(el.querySelector('[data-mail=retry]'), 'textContent', t('mailRetry'));
    put(el.querySelector('[data-mail=drop]'), 'textContent', t('mailDrop'));
  }
  if (mail.length) box.querySelector('.empty')?.remove();
}
// A message has arrived once the conversation shows a copy that was not there when it was sent.
function settleOutbox(pane, messages, conversation) {
  state.shown = {pane, messages};
  if (pane === state.current) state.settled = state.view;
  const mail = outbox.get(pane); if (!mail) return;
  const taken = new Set();
  outbox.set(pane, mail.filter(item => {
    if (!ownMail(pane, item)) return false;
    if (item.conversation && conversation && item.conversation !== conversation) return false;  // written to another conversation of this pane id
    if (!item.conversation && conversation) item.conversation = conversation;
    const now = copies(messages, item.text);
    if (item.seen === null) { item.seen = now.map(copyKey); item.count = now.length; return true; }  // sent before the conversation was on screen: what is here now is the baseline
    if (item.status === 'sending') return true;
    const fresh = now.find(m => m.ts ? !item.seen.includes(copyKey(m)) && !taken.has(copyKey(m)) : now.length > item.count);
    if (!fresh) return true;
    taken.add(copyKey(fresh));
    return false;
  }));
  saveOutbox(pane);
}
let leaving = false;
addEventListener('pagehide', () => { leaving = true; });
addEventListener('pageshow', () => { leaving = false; });
// The one way a message leaves: the first send and "Try again" both come through here.
async function sendItem(pane, item) {
  const has = () => (outbox.get(pane) || []).includes(item);
  item.status = 'sending'; saveOutbox(pane); renderOutbox();
  const r = await act($('#send'), () => api(`/api/sessions/${encodeURIComponent(pane)}/prompt`, {text: item.text}), '');
  // A request cut off by the page closing is not a refusal: timers do not run on a page that is going away, so the record stays and comes back as "not sent".
  if (!r) await new Promise(done => setTimeout(done, 80));
  if (!has() || leaving) return;
  if (!r) {  // it never left: the words go back where they were typed
    outbox.set(pane, outbox.get(pane).filter(x => x !== item)); saveOutbox(pane);
    const draft = [item.text, store.get('draft:' + pane)].filter(Boolean).join('\n');
    store.set('draft:' + pane, draft);
    if (state.current === pane) { ta.value = draft; autosize(); renderOutbox(); }
    return;
  }
  item.status = 'sent'; saveOutbox(pane);
  setTimeout(() => { if (item.status === 'sent' && has()) { item.status = 'unseen'; saveOutbox(pane); renderOutbox(); } }, window.UNSEEN_MS || 20000);
  renderOutbox();
  setTimeout(loadChat, 500);
}
$('#messages').addEventListener('click', e => {
  const button = e.target.closest('[data-mail]'), item = button?.closest('.msg.pending')?._item, pane = state.current;
  if (!item || !stalled(item)) return;
  if (button.dataset.mail === 'retry') return sendItem(pane, item);
  outbox.set(pane, (outbox.get(pane) || []).filter(x => x !== item)); saveOutbox(pane);
  renderOutbox();
});
loadOutbox();
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
    const anchor = state.anchor;
    const r = await readSnapshot(`/api/sessions/${encodeURIComponent(pane)}/chat${anchor === null ? '' : `?from=${anchor}`}`);
    if (chatRequest !== request || view !== state.view || generation !== snapshotGeneration) return;
    if (anchor !== state.anchor) { request.again = true; return; }  // asked from another place than the one now shown: a jump or "load earlier" came in between
    if (state.conversation !== undefined && r.conversation !== state.conversation) {  // /clear: another conversation
      forgetPast();
      if (anchor !== null) { state.conversation = r.conversation; request.again = true; return; }
    }
    state.conversation = r.conversation;
    if (state.anchor !== null && r.start > state.anchor) forgetPast();  // better to load again than to show a hole
    if (state.anchor === null) state.start = r.start ?? null;
    const items = itemsOf(r.messages, r.source);
    const key = JSON.stringify([r.hint, items.map(item => item.key + '~' + item.sig)]);
    settleOutbox(pane, r.messages, r.conversation);
    if (key !== state.chatHash) { state.chatHash = key; renderMessages(r, first, items); }
    renderEarlier(r.source);
    renderOutbox();
    put($('#sSearch'), 'hidden', r.source !== 'transcript');
    if (state.found !== null && anchor === state.found) showFound();
    renderPrompt(r.prompt, r.status);
    renderReceipt(r.receipt);
    if (state.busy.has(pane)) setPaneActionsBusy(pane, true);
    if (r.session) { state.sessions.set(pane, {...state.sessions.get(pane), ...r.session}); updateSessionHeader(); }
    state.meters = r.meters || {};
    if (typeof r.now === 'number') state.skew = r.now - Date.now() / 1000;  // the phone's clock is not the computer's
    renderMeters();
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
// ---------- earlier messages ----------
// The live part follows the conversation; what was loaded from before it sits above and is not asked for again.
function parts() {
  const box = $('#messages');
  if (!box.querySelector(':scope > .live')) { box.innerHTML = '<div class="earlier"></div><div class="past"></div><div class="live"></div>'; }
  return {box, earlier: box.querySelector('.earlier'), past: box.querySelector('.past'), live: box.querySelector('.live')};
}
function forgetPast() { state.anchor = null; state.cursor = null; state.start = null; state.found = null; $('#messages .past')?.replaceChildren(); state.chatHash = ''; }
function renderEarlier(source) {
  const {earlier} = parts();
  const at = state.anchor === null ? state.start : state.cursor;
  const kind = source !== 'transcript' ? '' : at > 0 ? 'loadEarlier' : at === 0 ? 'conversations' : '';
  putHtml(earlier, kind ? `<button type="button" data-kind="${kind}">${esc(t(kind))}</button>` : '');
}
async function loadEarlier(button) {
  const pane = state.current, view = state.view, conversation = state.conversation;
  if (state.anchor === null) { state.anchor = state.cursor = state.start; }
  const r = await act(button, () => api(`/api/sessions/${encodeURIComponent(pane)}/history?before=${state.cursor}`));
  if (!r || view !== state.view || conversation !== state.conversation) return;
  const {box, past} = parts();
  keepPlace(box, () => {
    past.prepend(...nodesFor(r.messages, 'transcript'));
    state.cursor = r.start;
    renderEarlier('transcript');
  });
}
$('#messages').addEventListener('click', e => {
  const button = e.target.closest('.earlier button');
  if (!button || button.classList.contains('loading')) return;
  button.dataset.kind === 'loadEarlier' ? loadEarlier(button) : conversationsSheet();
});
async function conversationsSheet() {
  const pane = state.current;
  showSheet(`<h3>${esc(t('conversations'))}</h3><p class="note">${esc(t('conversationsNote'))}</p><div class="past-list scroll" style="max-height:60vh"><p class="empty">${esc(t('loading'))}</p></div>`);
  const box = $('#sheet .past-list');
  const r = await act(null, () => api(`/api/sessions/${encodeURIComponent(pane)}/conversations`));
  if (!box.isConnected || state.current !== pane) return;
  const items = r?.items || [];
  box.innerHTML = items.map(c => `<button type="button" class="row" data-c="${esc(c.id)}"><span>${esc(c.title)}</span><small>${esc(ago(c.ts))}</small></button>`).join('') || `<p class="empty">${esc(t('noConversations'))}</p>`;
  box.onclick = e => { const b = e.target.closest('[data-c]'); if (b) pastSheet(pane, b.dataset.c, b.querySelector('span').textContent); };
}
// ---------- search: a word, anywhere this folder's conversations said it ----------
let searchTimer = 0, searchAbort = null;
function searchSheet() {
  const pane = state.current;
  clearTimeout(searchTimer); searchAbort?.abort();
  showSheet(`<h3>${esc(t('search'))}</h3><input type="search" maxlength="80" enterkeyhint="search" autocomplete="off" autocapitalize="none" autocorrect="off" spellcheck="false" placeholder="${esc(t('searchPh'))}" aria-label="${esc(t('search'))}"><div class="hits scroll" style="max-height:56dvh" aria-live="polite"></div>`);
  const input = $('#sheet input'), box = $('#sheet .hits');
  const note = (cls, text) => { const p = document.createElement('p'); p.className = cls; p.textContent = text; return p; };
  const row = h => {
    const b = document.createElement('button'), head = document.createElement('small'), snip = document.createElement('span'), mark = document.createElement('mark');
    b.type = 'button'; b.className = 'hit'; snip.className = 'snip';
    head.textContent = [t(h.role === 'user' ? 'roleYou' : 'roleAgent'), ago(Date.parse(h.ts) / 1000), h.title].filter(Boolean).join(' · ');
    mark.textContent = h.snippet.slice(h.at[0], h.at[1]);
    snip.append(h.snippet.slice(0, h.at[0]), mark, h.snippet.slice(h.at[1]));  // what was said is text, never markup
    b.append(head, snip);
    b.onclick = () => {
      if (!h.near) return pastSheet(pane, h.conversation, h.title || state.sessions.get(pane)?.project || t('pane'), {before: h.end, mark: true});
      hideSheet(); forgetPast();
      state.anchor = state.cursor = state.found = h.offset;
      loadChat(true);
    };
    return b;
  };
  const run = async () => {
    const q = input.value.trim();
    searchAbort?.abort();
    if (q.length < 2) { box.replaceChildren(); return; }
    const mine = searchAbort = new AbortController();
    let r;
    try { r = await api(`/api/sessions/${encodeURIComponent(pane)}/search?q=${encodeURIComponent(q)}`, undefined, undefined, mine.signal); }
    catch (e) {
      if (!mine.signal.aborted && box.isConnected) box.replaceChildren(note('empty', e.status === 404 ? t('searchClosed') : e.status === 409 ? t('searchNoRecord') : e.message));
      return;
    }
    if (mine.signal.aborted || !box.isConnected) return;
    const hits = r.hits || [];
    box.replaceChildren(...hits.map(row), ...(r.partial ? [note('partial', t('searchPartial'))] : hits.length ? [] : [note('empty', t('searchNone'))]));
    box.scrollTop = 0;
  };
  input.oninput = () => { clearTimeout(searchTimer); searchTimer = setTimeout(run, 250); };
  input.onkeydown = e => { if (e.key === 'Enter') { e.preventDefault(); clearTimeout(searchTimer); run(); input.blur(); } };  // the keyboard steps aside for what was found
}
$('#sSearch').onclick = searchSheet;
// The chat now starts at what was looked for: bring it to the top and point at it for a moment.
function showFound() {
  const box = $('#messages'), el = box.querySelector('.live .msg');
  state.found = null;
  if (!el) return;
  el.classList.add('found');
  box.scrollTop += el.getBoundingClientRect().top - box.getBoundingClientRect().top - 8;
  $('#jump').hidden = true;
  setTimeout(() => el.classList.remove('found'), 2000);
}
async function pastSheet(pane, id, title, at) {
  showSheet(`<h3>${esc(title)}</h3><p class="note">${esc(t('conversationsNote'))}</p><div class="past-chat scroll" style="max-height:62vh"><p class="empty">${esc(t('loading'))}</p></div>`);
  const box = $('#sheet .past-chat');
  const page = async before => {
    const r = await act(box.querySelector('.past-more'), () => api(`/api/sessions/${encodeURIComponent(pane)}/history?conversation=${encodeURIComponent(id)}${before ? `&before=${before}` : ''}`));
    if (!r || !box.isConnected) return;
    const fromBottom = box.scrollHeight - box.scrollTop;
    const first = !box.querySelector('.past-more') && !!box.querySelector('.empty'), nodes = nodesFor(r.messages, 'transcript');
    box.querySelector('.past-more')?.remove(); box.querySelector('.empty')?.remove();
    if (first && at?.mark) nodes.findLast(n => n.classList.contains('msg'))?.classList.add('found');
    box.prepend(...nodes);
    if (r.start) {
      const more = document.createElement('button'); more.type = 'button'; more.className = 'past-more'; more.textContent = t('loadEarlier');
      more.onclick = () => { if (!more.classList.contains('loading')) page(r.start); };
      box.prepend(more);
    }
    box.scrollTop = first ? box.scrollHeight : box.scrollHeight - fromBottom;
  };
  page(at?.before || 0);
}
function renderMessages(r, first, items = itemsOf(r.messages, r.source)) {
  const {box, live} = parts();
  const near = box.scrollHeight - box.scrollTop - box.clientHeight < 120;
  keepPlace(box, () => {
    box.querySelector('.hint')?.remove(); box.querySelector('.empty')?.remove();
    const kept = new Map(), owner = new Map(), used = new Set();
    for (const el of live.children) {
      if (el._kind === 'run') el._ids.forEach(id => owner.set(id, el));
      else if (el._key) kept.set(el._kind + el._key, el);
    }
    // A run takes over the node that already holds one of its calls: it grows, shrinks or splits where it stands.
    const nodes = items.map(item => {
      let node = item.kind === 'run' ? item.messages.map(m => owner.get(m.id)).find(el => el && !used.has(el)) : kept.get(item.kind + item.key);
      if (!node) node = buildNode(item);
      else if (node._sig !== item.sig) fill(node, item);
      used.add(node);
      return node;
    });
    nodes.forEach((node, i) => { if (live.children[i] !== node) live.insertBefore(node, live.children[i] || null); });
    while (live.children.length > nodes.length) live.lastElementChild.remove();
    if (!nodes.length) live.innerHTML = `<div class="empty">${t('noChat')}</div>`;
    if (r.hint) { const h = document.createElement('div'); h.className = 'hint'; h.textContent = r.hint.replace(/\(.*$/, '').trim() || t('working'); box.appendChild(h); }
  }, first || near);
  $('#jump').hidden = first || near;
}
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
    const denyOnly = p.cut && !p.approvable;  // the record pinned every character: it can be answered like any other
    const why = {no_record: 'cutNoRecord', no_match: 'cutNoMatch', too_long: 'cutTooLong'}[p.cut_reason] || 'cutNote';
    const unseen = (p.hidden || []).join(', ');  // a command that is not what it looks like is answered once, here, never for the whole session
    zone.innerHTML = `<div class="prompt${two}${p.full || denyOnly ? ' fit' : ''}${isSealed(state.current, p) ? ' stamped' : ''}"><h3>${p.destructive ? `${icon('seal', 15)}${icon('seal', 15)}<span>${esc(t('destructive'))}</span>` : esc(t('waitingApproval'))}</h3>${p.full ? `<p class="full-note">${esc(t('fullFromRecord'))}</p><pre class="full" tabindex="0">${esc(p.full)}</pre>` : `<pre>${esc(p.details)}</pre>`}${unseen ? `<p class="hidden-note">${esc(t('hiddenNote', {c: unseen}))}</p>` : ''}${p.hook_note ? `<p class="hook-note">${esc(p.hook_note)}</p>` : ''}${denyOnly ? `<p class="q cut-note">${esc(t(why))}</p>` : ''}${p.destructive && !denyOnly ? `<p class="hook-note two-note">${esc(t('twoSealsNote'))}</p>` : ''}
      <div class="acts"><button class="deny" data-d="deny">${esc(t('deny'))}</button>${denyOnly ? '' : p.destructive ? `<button class="seal red" data-d="approve">${esc(t('review'))}</button>` : `<button class="seal" data-d="approve">${icon('seal', 18)}${esc(t('approve'))}</button>`}</div>
      ${!denyOnly && !p.full && roles.always && !p.destructive && !unseen ? `<details class="more"><summary>${esc(t('otherAnswers'))}</summary><p>${esc(t('alwaysNote'))}</p><button class="also" data-d="always">${esc(t('alwaysAllow'))}</button></details>` : ''}${isSealed(state.current, p) ? stampHtml(p) : ''}</div>`;
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
const DECIDED = {approve: 'approved', deny: 'denied', always: 'allowedSession', choose: 'chose', keys: 'keysSent'};
const arrival = r => r.delivered ? t('rcptArrived', {t: clock(r.delivered)}) : r.delivered === false ? t('rcptUnknown') : t('rcptPending');
function renderReceipt(r) {
  const el = $('#receipt');
  const fresh = !!r && Date.now() / 1000 - r.ts < 600;  // a receipt is news for ten minutes, then it lives in Decisions
  put(el, 'hidden', !fresh);
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
  slashMenu.innerHTML = commands.map((command, i) => `<button type="button" role="option" aria-selected="${i === slashIndex}" data-slash-cmd="${esc(command.name)}"><span>${esc(command.name)}</span>${command.description ? `<small>${esc(command.description)}</small>` : ''}${command.kind === 'menu' ? `<small class="note">${esc(t('slashMenuNote'))}</small>` : ''}</button>`).join('');
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
// A command that leaves the session closed is asked about once; the short list covers a command list that did not load.
const ENDS_FALLBACK = ['/exit', '/quit', '/logout'];
function endsSession(text) {
  const first = text.match(/^\/\S+/)?.[0], known = slashCache.get(state.current);
  return first && (known ? known.some(c => c.name === first && c.kind === 'ends') : ENDS_FALLBACK.includes(first)) ? first : '';
}
function endsSheet(command, onYes) {
  showSheet(`<h3>${esc(t('endsTitle'))}</h3><p>${esc(t('endsBody', {c: command}))}</p>
    <div class="acts"><button class="ghost" data-x>${esc(t('cancel'))}</button><button class="danger" data-ends>${esc(t('endsYes', {c: command}))}</button></div>`);
  $('#sheet [data-x]').onclick = hideSheet;
  $('#sheet [data-ends]').onclick = e => { e.currentTarget.onclick = null; hideSheet(); onYes(); };  // a second tap sends nothing
}
$('#composer').addEventListener('submit', e => {
  e.preventDefault();
  const text = ta.value.trim(); if (!text) return;
  const ends = endsSession(text), pane = state.current;
  if (ends) endsSheet(ends, () => { if (state.current === pane && ta.value.trim() === text) sendDraft(text); });
  else sendDraft(text);
});
async function sendDraft(text) {
  const pane = state.current, shown = state.shown?.pane === pane, before = shown ? copies(state.shown.messages, text) : null;
  const item = {id: Date.now().toString(36) + Math.random().toString(36).slice(2, 8), text, status: 'sending', seen: before && before.map(copyKey), count: before ? before.length : 0, conversation: (shown && state.conversation) || null, born: state.sessions.get(pane)?.born || null, ts: Date.now()};
  written.set(item, state.view);
  outbox.set(pane, [...(outbox.get(pane) || []), item]);
  ta.value = ''; store.set('draft:' + pane, ''); autosize(); slashMenu.hidden = true;
  $('#messages').scrollTop = $('#messages').scrollHeight;
  await sendItem(pane, item);
}
// ---------- quick replies: short texts of your own, a tap puts one in the draft ----------
const CHIP_MAX = 12, CHIP_LEN = 200;
const clipChip = v => { const s = typeof v === 'string' ? v.trim().slice(0, CHIP_LEN) : ''; return (/[\uD800-\uDBFF]$/.test(s) ? s.slice(0, -1) : s).trim(); };
let chips = []; try { const kept = JSON.parse(store.get('chips') || '[]'); if (Array.isArray(kept)) chips = kept.map(clipChip).filter(Boolean).slice(0, CHIP_MAX); } catch {}
let chipsUnkept = false;  // the browser refused to keep them: they live until the page closes
function saveChips() { try { localStorage.setItem('chips', JSON.stringify(chips)); chipsUnkept = false; } catch { chipsUnkept = true; } }
function renderChips() {
  const row = $('#chips');
  row.replaceChildren(...chips.map(text => { const b = document.createElement('button'); b.type = 'button'; b.textContent = text; b.setAttribute('aria-label', text); return b; }));  // text, never markup
  row.hidden = !chips.length; row.scrollLeft = 0;
  $('#composer').classList.toggle('chipped', chips.length > 0);
}
$('#chips').addEventListener('pointerdown', e => { if (e.target.closest('button')) e.preventDefault(); });  // the keyboard stays up
$('#chips').addEventListener('click', e => {
  const b = e.target.closest('button'); if (!b) return;
  ta.setRangeText(b.textContent, ta.selectionStart, ta.selectionEnd, 'end');
  slashIndex = 0; updateSlashOptions(); autosize(); store.set('draft:' + state.current, ta.value);
  ta.focus();
});
function chipsSheet() {
  saveChips();  // also finds out whether the browser keeps anything
  showSheet(`<h3>${esc(t('chips'))}</h3><ul class="chip-list scroll"></ul><p class="chip-empty">${esc(t('chipsEmpty'))}</p>
    <form class="chip-new"><input type="text" maxlength="${CHIP_LEN}" enterkeyhint="done" autocomplete="off" placeholder="${esc(t('chipPh'))}" aria-label="${esc(t('chipPh'))}"><button class="primary">${esc(t('chipAdd'))}</button></form>
    <p class="chip-off" hidden>${esc(t('storageOff'))}</p>`);
  const list = $('#sheet .chip-list'), form = $('#sheet .chip-new'), input = form.querySelector('input');
  const act = (name, i, off) => `<button type="button" class="icon" data-chip="${name}" data-i="${i}" aria-label="${esc(t('chip' + name[0].toUpperCase() + name.slice(1)))}"${off ? ' disabled' : ''}>${icon(name === 'drop' ? 'close' : name, 20)}</button>`;
  const draw = () => {
    list.innerHTML = chips.map((text, i) => `<li><span>${esc(text)}</span>${act('up', i, i === 0)}${act('down', i, i === chips.length - 1)}${act('drop', i)}</li>`).join('');
    list.hidden = !chips.length; $('#sheet .chip-empty').hidden = chips.length > 0; $('#sheet .chip-off').hidden = !chipsUnkept;
    input.disabled = form.querySelector('button').disabled = chips.length >= CHIP_MAX;
  };
  const changed = () => { saveChips(); renderChips(); draw(); };
  list.onclick = e => {
    const b = e.target.closest('[data-chip]'); if (!b || b.disabled) return;
    const i = +b.dataset.i, kind = b.dataset.chip, to = kind === 'up' ? i - 1 : i + 1;
    if (kind === 'drop') chips.splice(i, 1); else [chips[i], chips[to]] = [chips[to], chips[i]];
    changed();
    // focus follows the text that moved; after a delete it goes to the neighbour, or to the box when none is left
    const row = list.children[kind === 'drop' ? Math.min(i, chips.length - 1) : to];
    (row ? [...row.querySelectorAll(`[data-chip=${kind}],[data-chip]`)].find(x => !x.disabled) : input).focus({preventScroll: true});
  };
  form.onsubmit = e => {
    e.preventDefault();
    const text = clipChip(input.value); if (!text || chips.length >= CHIP_MAX) return;
    chips.push(text); input.value = ''; changed(); list.scrollTop = list.scrollHeight;
    if (!input.disabled) input.focus({preventScroll: true});
  };
  draw();
}
renderChips();
// attach: upload the file to the server, then put its path in the draft (agents read files by path)
$('#attachBtn').onclick = () => $('#fileIn').click();
// iPhone photos (HEIC) carry where and with what they were taken, and the server cannot rewrite that format.
// Drawing the picture again leaves only the picture; a JPEG goes as it is and the server cleans it without touching the image.
async function redrawAsJpeg(file) {
  const url = URL.createObjectURL(file);
  try {
    const img = new Image(); img.src = url; await img.decode();
    const scale = Math.min(1, 4096 / Math.max(img.naturalWidth, img.naturalHeight));  // iOS refuses a canvas much larger than this
    const c = document.createElement('canvas'); c.width = Math.round(img.naturalWidth * scale); c.height = Math.round(img.naturalHeight * scale);
    c.getContext('2d').drawImage(img, 0, 0, c.width, c.height);
    return await new Promise(done => c.toBlob(done, 'image/jpeg', 0.9));
  } catch { return null; } finally { URL.revokeObjectURL(url); }
}
$('#fileIn').onchange = async e => {
  const files = [...e.target.files]; e.target.value = ''; if (!files.length) return;
  const btn = $('#attachBtn');
  const paths = [];
  let kept = false;
  const r = await act(btn, async () => {
    for (const f of files) {  // one upload per file, sequential; paths are collected as they succeed
      const body = /^image\/hei[cf]$/.test(f.type) && await redrawAsJpeg(f) || f;
      let res;
      try { res = await fetch(`/api/sessions/${encodeURIComponent(state.current)}/upload`, {method: 'POST', headers: {'Content-Type': body.type || 'application/octet-stream'}, body}); } catch { throw new Error(t('offline')); }
      if (!res.ok) { let m = `${res.status}`; try { m = (await res.json()).detail || m; } catch {} throw new Error(errText(m)); }
      const out = await res.json();
      paths.push(out.path); kept = kept || out.meta === 'kept';
    }
    toast(t(kept ? 'attachedKept' : 'attached'), kept ? '' : 'ok');
    return true;
  });
  if (paths.length) { ta.value = (ta.value ? ta.value.replace(/\s*$/, ' ') : '') + paths.join(' ') + ' '; store.set('draft:' + state.current, ta.value); autosize(); ta.focus(); }
};
// keep the composer above the iOS keyboard
if (/iPhone|iPad|iPod/.test(navigator.userAgent)) document.documentElement.classList.add('ios');
if (window.visualViewport) {
  let baseH = 0;
  const sa = navigator.standalone === true || matchMedia('(display-mode: standalone)').matches;
  // A home-screen app that draws under the status bar (black-translucent, top inset > 0) has been measured to end that much above the screen bottom. .under asks for the missing height; .sa stays only while the webview is in fact still short, because then the bottom inset lies outside the page.
  // Measured 27.11.2026 in the home-screen app: at load the top inset still read 0 and came out as 47px later, so it is read again on every fit, not once.
  const probe = document.createElement('div'); probe.style.cssText = 'position:fixed;visibility:hidden;pointer-events:none;padding-top:env(safe-area-inset-top)'; document.body.append(probe);
  let under = false;
  if (sa) document.documentElement.classList.add('installed');
  const fit = () => {
    baseH = Math.max(baseH, visualViewport.height);  // tallest viewport seen = keyboard closed (innerHeight also shrinks in a home-screen app)
    const kb = baseH - visualViewport.height > 120;
    if (sa && !under && parseFloat(getComputedStyle(probe).paddingTop) > 0) { under = true; document.documentElement.classList.add('under'); }
    if (under) document.documentElement.classList.toggle('sa', screen.height - baseH > 20 && innerWidth < innerHeight + 200);
    document.documentElement.classList.toggle('kb', kb);
    // Measured on iPhone (?debug=1, Safari tab): visualViewport already ends above the URL pill and ^ v done bar, so no reserve is needed
    document.documentElement.style.setProperty('--vvh', visualViewport.height + 'px'); window.scrollTo(0, 0); const b = $('#messages'); if (!$('#session').hidden && document.activeElement === ta) b.scrollTop = b.scrollHeight; };
  let debugOn = false;
  const errs = [];  // the last script errors, for the report the strip can send
  const noteErr = text => { errs.push(`${new Date().toISOString().slice(11, 19)} ${String(text).slice(0, 300)}`); if (errs.length > 20) errs.shift(); };
  addEventListener('error', e => noteErr(e.message || 'error')); addEventListener('unhandledrejection', e => noteErr(`rejected: ${e.reason && e.reason.message || e.reason}`));
  const startDebug = () => { if (debugOn) return; debugOn = true;  // on-screen metrics for diagnosing keyboard gaps
    const d = document.createElement('pre');
    d.style.cssText = 'position:fixed;top:0;left:0;right:0;z-index:99999;margin:0;padding:4px 6px;font:11px/1.3 monospace;background:#000c;color:#0f0;pointer-events:none;white-space:pre-wrap';
    document.body.appendChild(d);
    const probe = document.createElement('div');  // resolves env(safe-area-inset-*) to pixels
    probe.style.cssText = 'position:fixed;visibility:hidden;pointer-events:none;top:0;left:0;padding:env(safe-area-inset-top) env(safe-area-inset-right) env(safe-area-inset-bottom) env(safe-area-inset-left)';
    document.body.appendChild(probe);
    const fx = document.createElement('div'); fx.style.cssText = 'position:fixed;visibility:hidden;pointer-events:none;left:0;bottom:0;width:1px;height:1px'; document.body.appendChild(fx);  // where the browser thinks the bottom is
    const dbg = () => { const c = $('#composer'), r = c && c.getBoundingClientRect(), a = $('#app'), ar = a && a.getBoundingClientRect(), cs = getComputedStyle(probe), de = document.documentElement, dk = $('.dock'); d.textContent =
      `vv.h=${visualViewport.height.toFixed(0)} vv.top=${visualViewport.offsetTop.toFixed(0)} inner=${innerHeight} outer=${outerHeight} screen=${screen.height} base=${baseH.toFixed(0)}\n` +
      `html.clientH=${de.clientHeight} body.clientH=${document.body.clientHeight} app.bottom=${ar ? ar.bottom.toFixed(0) : '-'} composer.bottom=${r ? r.bottom.toFixed(0) : '-'} --vvh=${de.style.getPropertyValue('--vvh')}\n` +
      `safe t/r/b/l=${cs.paddingTop}/${cs.paddingRight}/${cs.paddingBottom}/${cs.paddingLeft} standalone=${navigator.standalone === true || matchMedia('(display-mode: standalone)').matches} scrollY=${scrollY.toFixed(0)} cls=${de.className}\n` +
      `docH=${de.scrollHeight} html.h=${de.getBoundingClientRect().height.toFixed(0)} fixedBottom=${fx.getBoundingClientRect().bottom.toFixed(0)} dock.bottom=${dk ? dk.getBoundingClientRect().bottom.toFixed(0) : '-'}`; };
    visualViewport.addEventListener('resize', dbg); visualViewport.addEventListener('scroll', dbg); document.addEventListener('focusin', () => setTimeout(dbg, 300)); setInterval(dbg, 500); dbg();
    // A home-screen app cannot be looked at from the machine, so the strip can send what it measured there: sizes, boxes and
    // element names, never the text on the screen. It goes the way an attached file goes and lands in the upload folder.
    const send = document.createElement('button'); send.id = 'debugSend'; send.type = 'button'; send.textContent = 'send report';
    send.style.cssText = 'position:fixed;right:4px;top:calc(env(safe-area-inset-top) + 64px);z-index:99999;padding:6px 10px;font:12px/1.3 monospace;background:#000c;color:#0f0;border:1px solid #0f0;border-radius:6px';
    document.body.appendChild(send);
    const name = el => el.tagName.toLowerCase() + (el.id ? '#' + el.id : '') + [...el.classList].map(c => '.' + c).join('');
    const box = el => { const r = el.getBoundingClientRect(); return `${r.left.toFixed(0)},${r.top.toFixed(0)} ${r.width.toFixed(0)}x${r.height.toFixed(0)}`; };
    const report = () => { dbg(); const de = document.documentElement, out = [d.textContent,
        `ua=${navigator.userAgent}`,
        `view=${$('#session').hidden ? 'home' : 'session'} dpr=${devicePixelRatio} inner=${innerWidth}x${innerHeight} screen=${screen.width}x${screen.height} scheme=${matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light'} theme=${de.dataset.theme || '-'} lang=${de.lang} worker=${navigator.serviceWorker && navigator.serviceWorker.controller ? 'yes' : 'no'} online=${navigator.onLine} focus=${document.activeElement ? name(document.activeElement) : '-'}`,
        '', 'boxes (left,top widthxheight):'];
      for (const sel of ['#home .top', '#home .dock', '#list', '#session .top', '#messages', '#receipt', '#promptZone', '#keybar', '#chips', '#composer', '#message', '#sheet', '#toasts']) { const el = $(sel); out.push(`${sel} ${el && el.getClientRects().length ? box(el) : 'not drawn'}`); }
      const drawn = [...document.body.querySelectorAll('*')].filter(el => el !== d && el !== send && el.getClientRects().length);
      const wide = drawn.filter(el => { const r = el.getBoundingClientRect(); return r.right > innerWidth + 1 || r.left < -1; });
      const cut = drawn.filter(el => el.scrollWidth > el.clientWidth + 1 && getComputedStyle(el).overflowX !== 'visible');
      out.push('', `past the side edges (${wide.length}):`, ...wide.slice(0, 30).map(el => `${name(el)} ${box(el)}`));
      out.push('', `wider inside than outside (${cut.length}):`, ...cut.slice(0, 30).map(el => `${name(el)} ${box(el)} inner=${el.scrollWidth}`));
      out.push('', `script errors (${errs.length}):`, ...errs);
      return out.join('\n') + '\n'; };
    send.onclick = async () => { const pane = state.current || [...state.sessions.keys()][0];  // the upload path wants a living session
      if (!pane) { send.textContent = 'no session to send through'; return; }
      const body = new Blob([report()], {type: 'text/plain'}); send.textContent = 'sending';
      try { const res = await fetch(`/api/sessions/${encodeURIComponent(pane)}/upload`, {method: 'POST', headers: {'Content-Type': 'text/plain'}, body}); const got = await res.json().catch(() => ({}));
        send.textContent = res.ok ? `sent: ${String(got.path).split('/').pop()}` : `refused: ${res.status}`; } catch { send.textContent = 'offline'; } };
  };
  if (/[?&]debug=1/.test(location.search)) startDebug();
  { let taps = []; document.addEventListener('pointerdown', () => { const n = Date.now(); taps = taps.filter(x => n - x < 2000); taps.push(n); if (taps.length >= 6) { taps = []; startDebug(); } }); }  // 6 quick taps enable the strip (home-screen app has no URL bar)
  visualViewport.addEventListener('resize', fit); visualViewport.addEventListener('scroll', () => window.scrollTo(0, 0)); fit(); if (sa) { requestAnimationFrame(fit); setTimeout(fit, 300); setTimeout(fit, 1500); }
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

let quickLabel = '', approvalTimeout = 0, handoffGrace = 600;
function setQuickLabel() { $('#toQuick').textContent = quickLabel ? t('quickTo', {x: quickLabel}) : t('quickSend'); }
api('/api/config').then(c => { quickLabel = c.quick_label || ''; approvalTimeout = +c.approval_timeout_minutes || 0; handoffGrace = +c.auto_handoff?.grace_seconds || 600; $('#toQuick').hidden = !quickLabel; setQuickLabel(); }).catch(() => {});
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
  showSheet(`<h3>${esc(s.project || t('pane'))}</h3><p>${esc(s.cwd || '')}</p><div class="acts" style="flex-direction:column"><button data-s>${esc(t('agentScreen'))}</button><button data-d>${esc(t('changes'))}</button>${s.agent === 'claude' ? `<button data-p>${esc(t('conversations'))}</button>` : ''}<button data-r>${esc(t('refreshSession'))}</button><button class="danger" data-c>${esc(t('closeSession'))}</button><button class="ghost" data-x>${esc(t('cancel'))}</button></div>`);
  $('#sheet [data-x]').onclick = hideSheet;
  $('#sheet [data-r]').onclick = () => { hideSheet(); state.chatHash = ''; loadChat(true); };
  $('#sheet [data-s]').onclick = screenSheet;
  $('#sheet [data-d]').onclick = changesSheet;
  $('#sheet [data-p]')?.addEventListener('click', conversationsSheet);
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
// 'none' = this browser cannot push, 'off' = it can and is not set up, 'on' = done,
// 'stopped' = the push service last refused this device, 'denied' = switched off in the device's own settings. The settings button carries a dot for the three that need the owner.
let bellState = 'none', bellHealth = null, bellEndpoint = null;
const bellFrom = (permission, subscribed, health) => permission === 'denied' ? 'denied'
  : !(subscribed && permission === 'granted' && health?.known) ? 'off'
  : health.fail_at && health.fail_at > (health.ok_at || 0) ? 'stopped' : 'on';
async function pushState() {
  if (!('serviceWorker' in navigator) || !('PushManager' in window) || !swReg) { setBell('none'); return; }
  const sub = await swReg.pushManager.getSubscription();
  bellEndpoint = sub?.endpoint || null;
  bellHealth = null;
  if (sub && Notification.permission === 'granted') { try { bellHealth = await api('/api/push/known', {endpoint: sub.endpoint}); } catch { bellHealth = {known: true}; } }
  setBell(bellFrom(Notification.permission, !!sub, bellHealth));
}
function setBell(v) { bellState = v; $('#settings').classList.toggle('dot', ['off', 'stopped', 'denied'].includes(v)); if ($('#sheet [data-settings]')) settingsSheet(); }
const enableBell = btn => act(btn, async () => {
  if (Notification.permission !== 'granted' && await Notification.requestPermission() !== 'granted') throw new Error(t('noPermission'));
  const {key} = await api('/api/push/public'); if (!key) throw new Error(t('noPushKey'));
  const pad = key.replace(/-/g, '+').replace(/_/g, '/').padEnd(Math.ceil(key.length / 4) * 4, '=');
  let sub = await swReg.pushManager.getSubscription();
  if (sub && bellState === 'stopped') { await sub.unsubscribe().catch(() => {}); sub = null; }  // the old address is the one that stopped working
  sub = sub || await swReg.pushManager.subscribe({userVisibleOnly: true, applicationServerKey: Uint8Array.from(atob(pad), c => c.charCodeAt(0))});
  await api('/api/push/subscribe', {...sub.toJSON(), lang: LANG}); await pushState();
}, t('bellOn'));
const testBell = btn => act(btn, async () => {
  const r = await api('/api/push/test', {endpoint: bellEndpoint});
  if (r.ok) { bellHealth = {...bellHealth, known: true, ok_at: serverNow()}; return toast(t('bellTestSent'), 'ok'); }
  bellHealth = {...bellHealth, known: true, fail_at: serverNow(), fail_code: r.code};
  setBell(r.code === 404 || r.code === 410 ? 'off' : 'stopped');
  throw new Error(t('bellTestFailed', {n: r.code ?? '?'}));
});
if ('serviceWorker' in navigator) {
  navigator.serviceWorker.register('/sw.js').then(r => { swReg = r; pushState(); }).catch(() => {});
  navigator.serviceWorker.addEventListener('message', e => { if (e.data?.type === 'open' && e.data.pane) { history.pushState({pane: e.data.pane}, '', `#/s/${e.data.pane}`); route(); }
    // A push that reached the phone while the app was open: the worker hands it over instead of showing it.
    if (e.data?.type === 'pushed' && e.data.pane && !$('#toasts').querySelector(`.call[data-pane="${CSS.escape(e.data.pane)}"]`)) { const s = state.sessions.get(e.data.pane); if (s?.bucket === 'blocked') callOut(s); }
  });
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
  const outcome = a => a.decision === 'auto-handoff' ? `Ulak · ${I18N.en['hoA' + a.outcome] ? t('hoA' + a.outcome) : a.outcome}${a.reason ? `: ${a.reason}` : ''}` : a.outcome === 'refused' ? `${t('outRefused')}: ${errText(a.reason)}` : a.outcome === 'confirm_requested' ? t('outConfirm') : a.outcome === 'timeout' ? `${t('outTimeout', {n: (/[\d.]+/.exec(a.reason || '') || [''])[0]})} · ${arrival(a)}` : `${t(DECIDED[a.decision] || 'sent')} · ${arrival(a)}`;
  const decisions = audit.items.length ? audit.items.map(a => `<div class="audit ${a.outcome === 'refused' ? 'no' : ''}"><b>${esc(a.project || a.pane)}</b><span>${esc(outcome(a))}</span>${a.command ? `<pre>${esc(a.command)}</pre>` : ''}<small>${esc(a.login || (a.outcome === 'timeout' ? 'Ulak' : ''))} · ${esc(new Date(a.ts * 1000).toLocaleString(LANG === 'tr' ? 'tr-TR' : 'en-GB', {dateStyle: 'medium', timeStyle: 'short'}))}</small></div>`).join('') : `<div class="empty">${t('noDecisions')}</div>`;
  showSheet(`<h3 class="sr-only">${esc(t('history'))}</h3><div class="tabs" role="tablist"><button role="tab" data-tab="notifications" aria-selected="true">${esc(t('notifications'))}</button><button role="tab" data-tab="decisions" aria-selected="false">${esc(t('decisions'))}</button></div>
    <div class="scroll" style="max-height:65vh" data-panel="notifications">${rows}</div><div class="scroll" style="max-height:65vh" data-panel="decisions" hidden>${decisions}</div>`);
  $('#sheet').querySelectorAll('[data-tab]').forEach(tab => tab.onclick = () => {
    $('#sheet').querySelectorAll('[data-tab]').forEach(x => x.setAttribute('aria-selected', String(x === tab)));
    $('#sheet').querySelectorAll('[data-panel]').forEach(x => { x.hidden = x.dataset.panel !== tab.dataset.tab; });
  });
  $('#sheet').querySelectorAll('[data-pane]').forEach(b => b.onclick = () => { const p = b.dataset.pane; hideSheet(); if (p && state.sessions.has(p)) go(p); });
});

const handoffKind = n => { const kind = String(n.status || '').startsWith('handoff-') ? n.status.slice(8) : ''; return I18N.en['hoN' + kind] ? kind : ''; };
function histTitle(n) { return handoffKind(n) ? t('hoTitle', {p: n.project || n.pane || ''}) : n.project ? t(n.status === 'blocked' ? 'needsYouTitle' : 'doneTitle', {p: n.project}) : (n.title || ''); }
function histBody(n) { const b = n.body || ''; if (handoffKind(n)) return t('hoN' + handoffKind(n), {n: (/\d+(\.\d+)?/.exec(b) || [+(handoffGrace / 60).toFixed(1)])[0], reason: n.raw || ''}); return n.project ? (b || t(n.status === 'blocked' ? 'askBody' : 'finishedBody')) : b; }

// ---------- language ----------
function refreshLang() {
  applyStaticI18n();
  updateConnection();
  setTitle();
  setQuickLabel();
  state.sessions.size && renderHome();
  renderLimits();
  if (state.current) { updateSessionHeader(); renderMeters(); $('#promptZone').dataset.sig = ''; state.chatHash = ''; loadChat(); }
}
async function toggleLang() {
  LANG = LANG === 'tr' ? 'en' : 'tr';
  store.set('lang', LANG);
  hideSheet(); refreshLang();
  try { const sub = await swReg?.pushManager.getSubscription(); if (sub) await api('/api/push/lang', {endpoint: sub.endpoint, lang: LANG}); } catch {}
}
function settingsSheet() {
  const reached = bellState === 'on' && bellHealth?.ok_at ? `<small>${esc(t('bellReached', {t: fmtIn(Math.max(1, serverNow() - bellHealth.ok_at))}))}</small>` : '';
  const bell = bellState === 'off' || bellState === 'stopped' ? `<button id="bell" class="row">${icon('bell', 20)}<span>${esc(t(bellState === 'off' ? 'enableBell' : 'bellStopped'))}</span></button>`
    : `<div class="row quiet">${icon('bell', 20)}<span>${esc(t(bellState === 'on' ? 'bellOn' : bellState === 'denied' ? 'bellDenied' : 'bellNone'))}${reached}</span></div>${bellState === 'on' ? `<button id="bellTest" class="row">${icon('bell', 20)}<span>${esc(t('bellTest'))}</span></button>` : ''}`;
  showSheet(`<h3 data-settings>${esc(t('settings'))}</h3><div class="rows">${bell}
    <button id="lang" class="row"><b class="lang">${LANG.toUpperCase()}</b><span>${esc(t('language'))}</span><small>${LANG === 'tr' ? 'English' : 'Türkçe'}</small></button>
    <button class="row" data-theme-toggle><svg class="moon" width="20" height="20" viewBox="0 0 24 24" aria-hidden="true"><defs><mask id="hilal2"><rect x="-4" y="-4" width="32" height="32" fill="#fff"/><circle class="bite" cx="16.4" cy="8.6" r="7.2" fill="#000"/></mask></defs><circle class="halo" cx="12" cy="12" r="9.5"/><circle class="disc" cx="12" cy="12" r="8.2" fill="currentColor" mask="url(#hilal2)"/></svg><span>${esc(t('appearance'))}</span><small><i class="when-dark">${esc(t('themeDark'))}</i><i class="when-light">${esc(t('themeLight'))}</i></small></button>
    <button id="chipsRow" class="row">${icon('chips', 20)}<span>${esc(t('chips'))}</span>${chips.length ? `<small>${chips.length}</small>` : ''}</button>
    <button id="refresh" class="row">${icon('refresh', 20)}<span>${esc(t('refresh'))}</span></button>
    ${approvalTimeout ? `<div class="row quiet timeout">${icon('timer', 20)}<span>${esc(t('timeoutOn', {n: approvalTimeout}))}</span></div>` : ''}</div>`);
  $('#sheet #lang').onclick = toggleLang;
  $('#sheet #chipsRow').onclick = chipsSheet;
  $('#sheet #refresh').onclick = e => act(e.currentTarget, loadList, t('refreshed'));
  const b = $('#sheet #bell'); if (b) b.onclick = e => enableBell(e.currentTarget);
  const bt = $('#sheet #bellTest'); if (bt) bt.onclick = e => testBell(e.currentTarget);
}
$('#settings').onclick = settingsSheet;
setTimeout(() => $('#home .mark').classList.remove('draw'), 1300);  // drawn once per opening, not each time the home screen comes back
refreshLang();
loadLimits();
