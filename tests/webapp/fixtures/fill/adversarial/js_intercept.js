// Intercepts submit ahead of everyone (registered first, capture) and posts the values itself.
window.addEventListener("submit", function (e) {
  e.preventDefault();
  var data = new FormData(document.getElementById("application_form"));
  fetch("/record/intercepted_submit", {method: "POST", body: new URLSearchParams(data)}).catch(function () {});
}, true);
