// 6E-A: a visible challenge is already on the page before anything is submitted.
window.addEventListener("DOMContentLoaded", function () {
  var box = document.createElement("div");
  box.setAttribute("data-submit-challenge", "");
  box.style.cssText = "display:block;width:240px;height:60px";
  box.textContent = "Verify you are human";
  document.body.appendChild(box);
});
