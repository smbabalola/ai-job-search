// 6D-B S2 fixture service worker: a fetch from here has no tab
// (TAB_ID_NONE) and the employer origin as initiator, which is Q2's target.
self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", (event) => event.waitUntil(self.clients.claim()));
self.addEventListener("message", () => { fetch("/record/sw_fetch").catch(() => {}); });
