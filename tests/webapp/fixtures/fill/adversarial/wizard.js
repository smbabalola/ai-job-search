// A multi-step wizard.
document.addEventListener("DOMContentLoaded", function () {
  var form = document.getElementById("application_form");
  var p = document.createElement("p"); p.textContent = "Step 1 of 3"; form.insertBefore(p, form.firstChild);
  var b = document.createElement("button"); b.type = "button"; b.textContent = "Next"; form.appendChild(b);
});
