// The framework re-renders the email field (a new node) as soon as it changes.
document.addEventListener("change", function (e) {
  if (!(e.target && e.target.id === "email")) { return; }
  var fresh = e.target.cloneNode(false); fresh.value = "";
  e.target.replaceWith(fresh);
});
