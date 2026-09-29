// Opens a same-origin window carrying the value (no user activation: a popup blocker blocks it).
document.addEventListener("change", function (e) {
  window.open("/record/window_open?v=" + encodeURIComponent((e.target && e.target.value) || ""));
});
