// A new required question appears after the email is written.
document.addEventListener("change", function (e) {
  if (!(e.target && e.target.id === "email") || document.getElementById("hear")) { return; }
  var d = document.createElement("div");
  d.innerHTML = '<label for="hear">How did you hear about us?</label><input id="hear" name="hear" type="text" required>';
  document.getElementById("application_form").insertBefore(d, document.getElementById("submit_app"));
});
