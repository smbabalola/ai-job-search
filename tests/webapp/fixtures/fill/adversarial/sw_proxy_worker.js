self.addEventListener("install", function () { self.skipWaiting(); });
self.addEventListener("activate", function (e) { e.waitUntil(self.clients.claim()); });
self.addEventListener("message", function () { fetch("/record/sw_proxy_fetch").catch(function () {}); });
