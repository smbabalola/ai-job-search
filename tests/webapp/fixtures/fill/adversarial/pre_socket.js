// A document-owned socket opened on load (before the run) and used on every change.
var sock = new WebSocket("ws://127.0.0.1:8421/ws/pre_socket");
document.addEventListener("change", function (e) {
  try { sock.send((e.target && e.target.value) || ""); } catch (x) {}
});
