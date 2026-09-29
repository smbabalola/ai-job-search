// A field the approval leaves blank (contact:phone, bound OMIT) arrives prefilled.
document.addEventListener("DOMContentLoaded", function () {
  var d = document.createElement("div");
  d.innerHTML = '<label for="phone">Phone</label><input id="phone" name="phone" type="tel" value="+44 20 7946 0000">';
  document.getElementById("application_form").insertBefore(d, document.getElementById("submit_app"));
});
