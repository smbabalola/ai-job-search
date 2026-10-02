// 6E-A: submitting shows a visible challenge; completing it (a person's click)
// sends the application by XHR and swaps in the confirmation.
window.addEventListener("DOMContentLoaded", function () {
  var box = document.createElement("div");
  box.id = "challenge";
  box.setAttribute("data-submit-challenge", "");
  box.style.cssText = "display:none;width:240px;height:60px";
  box.innerHTML = '<p>Verify you are human</p><button type="button" id="challenge_done">I am human</button>';
  document.body.appendChild(box);
  document.getElementById("challenge_done").addEventListener("click", function () {
    box.style.display = "none";
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
    x.onerror = function () { window.__challengeSendFailed = true; };
    x.send("submitted");
  });
  document.addEventListener("submit", function (event) {
    event.preventDefault();
    box.style.display = "block";
  });
});
