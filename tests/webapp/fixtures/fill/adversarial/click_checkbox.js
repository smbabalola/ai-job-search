// A required checkbox that only a click can set (React-style), never certified for greenhouse@2.
document.addEventListener("DOMContentLoaded", function () {
  var d = document.createElement("div");
  d.innerHTML = '<label><input type="checkbox" id="consent" name="consent" required> Keep me informed about future roles</label>';
  document.getElementById("application_form").insertBefore(d, document.getElementById("submit_app"));
});
