// A required custom widget (no native control) inside the application root.
document.addEventListener("DOMContentLoaded", function () {
  var d = document.createElement("div");
  d.innerHTML = '<div role="combobox" aria-label="Country of residence" aria-required="true" tabindex="0">Choose</div>';
  document.getElementById("application_form").insertBefore(d, document.getElementById("submit_app"));
});
