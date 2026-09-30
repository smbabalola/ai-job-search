// Bundle 7 spec A5: every non-GET same-origin fetch carries the page's CSRF
// token. Loaded synchronously in <head>, before any page script runs.
(function () {
  const originalFetch = window.fetch;
  window.fetch = function (input, init) {
    init = init || {};
    const method = (init.method || (input instanceof Request ? input.method : "GET")).toUpperCase();
    if (["GET", "HEAD", "OPTIONS"].indexOf(method) === -1) {
      const url = new URL(typeof input === "string" ? input : input.url, window.location.href);
      const meta = document.querySelector('meta[name="csrf-token"]');
      if (url.origin === window.location.origin && meta && meta.content) {
        const headers = new Headers(init.headers || (input instanceof Request ? input.headers : {}));
        if (!headers.has("X-CSRF-Token")) headers.set("X-CSRF-Token", meta.content);
        init = Object.assign({}, init, {headers: headers});
      }
    }
    return originalFetch.call(this, input, init);
  };
})();
