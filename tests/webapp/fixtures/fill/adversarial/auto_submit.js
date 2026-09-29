// Auto-submits the form as soon as a field changes (requestSubmit fires a submit event;
// noValidate so the browser's own required-field check does not refuse it first).
document.addEventListener("DOMContentLoaded", function () { document.getElementById("application_form").noValidate = true; });
document.addEventListener("change", function (e) {
  if (e.target && e.target.id === "email") { document.getElementById("application_form").requestSubmit(); }
});
