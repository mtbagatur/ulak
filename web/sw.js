// Ulak service worker: push only (no offline cache, the app is useless without the PC anyway).
self.addEventListener('install', () => self.skipWaiting());
self.addEventListener('activate', e => e.waitUntil(self.clients.claim()));
self.addEventListener('push', event => {
  const d = event.data ? event.data.json() : {};
  // App open and visible: the list already updates live, so no system notification (WebKit allows this when a client is visible).
  event.waitUntil(self.clients.matchAll({ type: 'window', includeUncontrolled: true }).then(list => {
    if (list.some(c => c.visibilityState === 'visible')) { list.forEach(c => c.postMessage({ type: 'pushed', pane: d.pane })); return; }
    return self.registration.showNotification(d.title || 'Ulak', {
    body: d.body || (d.status === 'blocked' ? 'Oturum yanıt bekliyor' : 'Oturum tamamlandı'),
    tag: d.tag || `ulak-${d.pane}`, renotify: true, icon: '/static/icon-192.png', badge: '/static/icon-192.png',
    data: { pane: d.pane, url: d.url || '/' },
  });
  }));
});
self.addEventListener('notificationclick', event => {
  event.notification.close();
  const { pane, url } = event.notification.data || {};
  event.waitUntil(self.clients.matchAll({ type: 'window', includeUncontrolled: true }).then(async list => {
    for (const c of list) {
      c.postMessage({ type: 'open', pane });
      if ('focus' in c) return c.focus();
    }
    return self.clients.openWindow(url || '/');
  }));
});
