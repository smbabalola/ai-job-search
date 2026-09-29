// Part of the application lives in a frame of another origin.
document.addEventListener("DOMContentLoaded", function () {
  var f = document.createElement("iframe"); f.src = "http://127.0.0.1:8430/employer_plain.html"; f.title = "More questions";
  document.getElementById("application_form").insertBefore(f, document.getElementById("submit_app"));
});
