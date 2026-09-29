// Certification-refusal fixture: a service worker that holds a WebSocket.
// The worker survives the reset reload, so this channel is uncontained.
self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", () => { new WebSocket(`ws://${self.location.hostname}:8421/ws/sw_held`); });
