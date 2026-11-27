const CACHE = 'reoview-v3';
const SHELL = ['/', '/static/hls.min.js', '/static/manifest.webmanifest', '/static/icon-192.png'];
self.addEventListener('install', e => e.waitUntil(caches.open(CACHE).then(c => c.addAll(SHELL))));
self.addEventListener('activate', e => e.waitUntil(
  caches.keys().then(k => Promise.all(k.filter(n => n !== CACHE).map(n => caches.delete(n))))));
self.addEventListener('fetch', e => {
  const u = new URL(e.request.url);
  if (e.request.method !== 'GET' || u.pathname.startsWith('/api/') || u.pathname.startsWith('/hls/') || u.pathname.startsWith('/alerts/') || u.pathname.startsWith('/download/')) return;
  e.respondWith(fetch(e.request).catch(() => caches.match(e.request)));
});
self.addEventListener('push', e => {
  let d = {};
  try { d = e.data.json(); } catch {}
  e.waitUntil(self.registration.showNotification(d.title || 'Reoview', {
    body: d.body || 'Motion detected', icon: '/static/icon-192.png',
    badge: '/static/icon-192.png', tag: d.tag || 'reoview', renotify: true, data: { url: d.url || '/' } }));
});
self.addEventListener('notificationclick', e => {
  e.notification.close();
  const url = (e.notification.data && e.notification.data.url) || '/';
  e.waitUntil((async () => {
    const l = await clients.matchAll({ type: 'window', includeUncontrolled: true });
    if (l.length) { try { await l[0].navigate(url); } catch {} return l[0].focus(); }
    return clients.openWindow(url);
  })());
});
