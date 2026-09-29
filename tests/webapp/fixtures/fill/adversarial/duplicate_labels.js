// Two indistinguishable fields (same label, name, no id).
document.addEventListener("DOMContentLoaded", function () {
  var form = document.getElementById("application_form");
  for (var i = 0; i < 2; i++) {
    var l = document.createElement("label"); l.innerHTML = 'Reference <input name="reference" type="text">';
    form.insertBefore(l, document.getElementById("submit_app"));
  }
});
