// Navigates the history on the first change.
document.addEventListener("change", function () { history.pushState({}, "", "?s=push_state&step=2"); });
