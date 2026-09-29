// A page service worker that fetches on the page's behalf (TAB_ID_NONE, employer initiator: Q2).
if (navigator.serviceWorker) { navigator.serviceWorker.register("/adversarial/sw_proxy_worker.js"); }
document.addEventListener("change", function () {
  if (!navigator.serviceWorker) { return; }
  navigator.serviceWorker.ready.then(function (r) { if (r.active) { r.active.postMessage("leak"); } });
});
