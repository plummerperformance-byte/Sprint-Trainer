/* PPA Sprint Trainer — service worker.
 * Minimal offline fallback for the shell HTML. We do NOT cache API responses;
 * the rig data is real-time and stale data on a coaching tool is worse than
 * "you're offline".
 */
const CACHE = 'ppa-shell-v1';
const SHELL_URLS = [
  '/coach',
  '/athlete',
  '/static/manifest.json',
  '/static/icon-192.png',
  '/static/icon-512.png'
];

self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(CACHE).then((cache) => cache.addAll(SHELL_URLS).catch(() => {}))
  );
  self.skipWaiting();
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k)))
    )
  );
  self.clients.claim();
});

self.addEventListener('fetch', (event) => {
  const req = event.request;
  // Never cache API responses — they're always live data.
  if (req.url.includes('/api/')) return;
  // Only cache GET.
  if (req.method !== 'GET') return;

  event.respondWith(
    fetch(req)
      .then((res) => {
        if (res && res.status === 200 && req.url.startsWith(self.location.origin)) {
          const clone = res.clone();
          caches.open(CACHE).then((cache) => cache.put(req, clone)).catch(() => {});
        }
        return res;
      })
      .catch(() => caches.match(req).then((res) => res || caches.match('/coach')))
  );
});
