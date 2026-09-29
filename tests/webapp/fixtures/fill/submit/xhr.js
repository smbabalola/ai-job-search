// 6E-A: the page submits by XHR and swaps in the confirmation (the in-page success variant).
document.addEventListener("submit", function (event) {
  event.preventDefault();
  var x = new XMLHttpRequest();
  x.open("POST", "/acme/jobs/123");
  x.setRequestHeader("X-Requested-With", "XMLHttpRequest");
  x.onload = function () {
    if (x.status !== 200) return;
    var done = document.createElement("div");
    done.id = "application_confirmation";
    done.textContent = "Thank you for applying";
    document.getElementById("application_form").replaceWith(done);
  };
  x.send("submitted");
});
