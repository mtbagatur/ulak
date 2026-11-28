// Ulak service worker: push only (no offline cache, the app is useless without the PC anyway).
self.addEventListener('install', () => self.skipWaiting());
self.addEventListener('activate', e => e.waitUntil(self.clients.claim()));
self.addEventListener('push', event => {
  const d = event.data ? event.data.json() : {};
  // App open and visible: the list already updates live, so no system notification (WebKit allows this when a client is visible).
  // A test the owner asked for is always shown: seeing it arrive is the point.
  event.waitUntil(self.clients.matchAll({ type: 'window', includeUncontrolled: true }).then(list => {
    if (!d.test && list.some(c => c.visibilityState === 'visible')) { list.forEach(c => c.postMessage({ type: 'pushed', pane: d.pane })); return; }
    return self.registration.showNotification(d.title || 'Ulak', {
    body: d.body || (d.status === 'blocked' ? 'Session needs you' : 'Session finished'),
    tag: d.tag || `ulak-${d.pane}`, renotify: true, icon: '/static/icon-192.png', badge: '/static/icon-192.png',
    data: { pane: d.pane, url: d.url || '/' },
  });
  }));
});
self.addEventListener('notificationclick', event => {
  event.notification.close();
  const { pane, url } = event.notification.data || {};
  // The window in front gets the tap. If no window can be brought forward, a new one opens on the session itself,
  // so the tap never ends on whatever the app happened to show.
  event.waitUntil(self.clients.matchAll({ type: 'window', includeUncontrolled: true }).then(async list => {
    for (const c of [...list].sort((a, b) => (b.visibilityState === 'visible') - (a.visibilityState === 'visible'))) {
      if (!('focus' in c)) continue;
      try { await c.focus(); } catch { continue; }
      c.postMessage({ type: 'open', pane });
      return;
    }
    return self.clients.openWindow(url || '/');
  }));
});
