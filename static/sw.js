// coniMIX service worker: the app opens without a connection, and songs saved on the phone play from it.
const SHELL = 'conimix-shell-v1', SONGS = 'conimix-songs';   // SONGS is filled by the page ("Al mòbil")
const SHELL_FILES = ['/', '/static/manifest.webmanifest', '/static/icon-180.png', '/static/icon-192.png', '/static/icon-512.png', '/static/logo.jpg', '/static/avatar.jpg'];

self.addEventListener('install', e => {
  e.waitUntil(caches.open(SHELL).then(c => c.addAll(SHELL_FILES)).catch(() => {}).then(() => self.skipWaiting()));
});
self.addEventListener('activate', e => {
  e.waitUntil(caches.keys().then(ks => Promise.all(ks.filter(k => k.startsWith('conimix-shell') && k !== SHELL).map(k => caches.delete(k))))
    .then(() => self.clients.claim()));
});

// network first (so updates arrive by themselves); with no connection, or with the PC off (the tunnel answers 5xx), the saved copy
async function networkFirst(req, key) {
  const c = await caches.open(SHELL);
  try {
    const r = await fetch(req);
    if (r.ok) { c.put(key, r.clone()); return r; }
    if (r.status < 500) return r;
  } catch {}
  return (await c.match(key)) || new Response('Sense connexió', {status: 503, headers: {'Content-Type': 'text/plain; charset=utf-8'}});
}

// a song saved on the phone, honouring Range (iOS only plays audio that answers byte ranges)
async function fromSaved(req, hit) {
  const range = req.headers.get('Range'), m = range && /bytes=(\d*)-(\d*)/.exec(range);
  if (!m || (!m[1] && !m[2])) return hit;
  const blob = await hit.blob(), size = blob.size;
  let start, end;
  if (m[1]) { start = +m[1]; end = m[2] ? Math.min(+m[2], size - 1) : size - 1; }
  else { start = Math.max(0, size - +m[2]); end = size - 1; }
  if (start >= size) return new Response(null, {status: 416, headers: {'Content-Range': `bytes */${size}`}});
  return new Response(blob.slice(start, end + 1), {status: 206, headers: {
    'Content-Type': hit.headers.get('Content-Type') || 'audio/mpeg', 'Content-Length': String(end - start + 1),
    'Content-Range': `bytes ${start}-${end}/${size}`, 'Accept-Ranges': 'bytes'}});
}

self.addEventListener('fetch', e => {
  const req = e.request, url = new URL(req.url);
  if (req.method !== 'GET' || url.origin !== location.origin) return;
  if (req.mode === 'navigate') { if (url.pathname === '/') e.respondWith(networkFirst(req, '/')); return; }   // the jam (/jam) is online only
  if (url.pathname.startsWith('/media/')) {
    e.respondWith(caches.open(SONGS).then(c => c.match(url.pathname)).then(hit => hit ? fromSaved(req, hit) : fetch(req)));
    return;
  }
  if (url.pathname.startsWith('/static/')) e.respondWith(networkFirst(req, url.pathname));
});
