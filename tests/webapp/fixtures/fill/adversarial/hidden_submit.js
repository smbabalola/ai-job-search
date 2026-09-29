// A hidden submit button clicked by page script on the first change.
document.addEventListener("DOMContentLoaded", function () {
  document.getElementById("application_form").noValidate = true;
  var b = document.createElement("button");
  b.type = "submit"; b.id = "hidden_submit"; b.style.display = "none"; b.textContent = "Send";
  document.getElementById("application_form").appendChild(b);
});
document.addEventListener("change", function (e) {
  if (e.target && e.target.id === "email") { document.getElementById("hidden_submit").click(); }
});
