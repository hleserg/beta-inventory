// Makes the site an installable app. No data cache: the inventory lives on the server.
// Away from the home network a page can't load; show why instead of the browser's error.
self.addEventListener("install", e => { self.skipWaiting(); e.waitUntil(caches.open("v1").then(c => c.add("/offline"))); });
self.addEventListener("fetch", e => {
  // Form posts go straight to the network: a failed photo upload then gets the browser's «resend», not a page that drops it.
  // A stalled Wi-Fi link: without the timeout an installed app shows nothing at all for minutes, the tap looks dead. 10 s, then «Нет связи» with «Обновить».
  if (e.request.mode === "navigate" && e.request.method === "GET") e.respondWith(fetch(e.request, { signal: AbortSignal.timeout(10000) }).catch(() => caches.match("/offline")));
});
