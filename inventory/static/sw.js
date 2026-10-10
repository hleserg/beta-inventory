// Makes the site an installable app. No data cache: the inventory lives on the server.
// Away from the home network a page can't load; show why instead of the browser's error.
self.addEventListener("install", e => { self.skipWaiting(); e.waitUntil(caches.open("v1").then(c => c.add("/offline"))); });
// «Взять/вернуть» without a link: the form's body waits in IndexedDB and goes out on the next page that loads (or Background Sync).
// op: one key per tap, the server drops a repeat — a post that did arrive but whose answer was lost isn't taken twice.
const store = mode => new Promise((ok, no) => { const r = indexedDB.open("q", 1);
  r.onupgradeneeded = () => r.result.createObjectStore("stock", { autoIncrement: true });
  r.onsuccess = () => ok(r.result.transaction("stock", mode).objectStore("stock")); r.onerror = () => no(r.error); });
const req = r => new Promise((ok, no) => { r.onsuccess = () => ok(r.result); r.onerror = () => no(r.error); });
async function flush() {
  const keys = await req((await store("readonly")).getAllKeys());
  for (const k of keys) {
    const body = await req((await store("readonly")).get(k));
    if (!await fetch("/stock", { method: "POST", body: new URLSearchParams(body), redirect: "manual" }).catch(() => null)) return;  // still offline
    await req((await store("readwrite")).delete(k));
  }
}
self.addEventListener("sync", e => { if (e.tag === "stock") e.waitUntil(flush()); });
self.addEventListener("fetch", e => {
  // Form posts go straight to the network: a failed photo upload then gets the browser's «resend», not a page that drops it.
  // A stalled Wi-Fi link: without the timeout an installed app shows nothing at all for minutes, the tap looks dead. 10 s, then «Нет связи» with «Обновить».
  if (e.request.mode === "navigate" && e.request.method === "GET") e.respondWith(fetch(e.request, { signal: AbortSignal.timeout(10000) })
    .then(r => { e.waitUntil(flush().catch(() => {})); return r; }, () => caches.match("/offline")));
  else if (e.request.method === "POST" && new URL(e.request.url).pathname === "/stock") e.respondWith((async () => {
    const body = new URLSearchParams(await e.request.clone().text()); body.set("op", crypto.randomUUID());
    try { return await fetch("/stock", { method: "POST", body, redirect: "manual", signal: AbortSignal.timeout(10000) }); }
    catch { await req((await store("readwrite")).add(body.toString())); self.registration.sync?.register("stock").catch(() => {});
      return Response.redirect("/offline?q=1", 303); }
  })());
});
