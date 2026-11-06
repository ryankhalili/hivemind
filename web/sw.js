const SHELL='hivemind-shell-v3';
const ASSETS=['/static/style.css','/static/app.js','/static/icon.svg'];
self.addEventListener('install',event=>{event.waitUntil(caches.open(SHELL).then(cache=>cache.addAll(ASSETS)));self.skipWaiting()});
self.addEventListener('activate',event=>{event.waitUntil(caches.keys().then(keys=>Promise.all(keys.filter(key=>key.startsWith('hivemind-shell-')&&key!==SHELL).map(key=>caches.delete(key)))));self.clients.claim()});
self.addEventListener('fetch',event=>{const url=new URL(event.request.url);if(event.request.method!=='GET'||url.origin!==self.location.origin||!ASSETS.includes(url.pathname))return;event.respondWith(fetch(event.request).then(response=>{if(response.ok){const copy=response.clone();caches.open(SHELL).then(cache=>cache.put(event.request,copy))}return response}).catch(()=>caches.match(event.request)))});
