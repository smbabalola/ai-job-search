"""Certification aid for spec §7.4 / §10.8: find channels that the
communications reset (reload) and Q1/Q2 cannot contain. A page with any of
these is refused certification (network_model must be
NO_UNCONTAINED_PERSISTENT_CHANNELS).

Flagged:
- WEBRTC: RTCPeerConnection use (DNR never sees WebRTC traffic);
- SHARED_WORKER: a SharedWorker (it can outlive the document);
- SW_PERSISTENT_CHANNEL: a service worker whose script opens WebSocket,
  WebTransport, EventSource or RTCPeerConnection (the worker survives the
  reload, so a channel it opened before the run survives too).

Document-owned WebSocket/WebTransport are NOT flagged: the reload closes
them (proved in the S2 suite). This is an aid used during certification on
an adapter version's fixtures, not a runtime guarantee."""
from __future__ import annotations

import re
from urllib.parse import urljoin

INSTRUMENT = """
(() => {
  const seen = (window.__fillCertify = { webrtc: 0, sharedWorker: 0, swScripts: [] });
  const wrap = (name, key) => {
    const Original = window[name];
    if (!Original) return;
    window[name] = new Proxy(Original, { construct(target, args) { seen[key] += 1; return new target(...args); } });
  };
  wrap("RTCPeerConnection", "webrtc");
  wrap("webkitRTCPeerConnection", "webrtc");
  wrap("SharedWorker", "sharedWorker");
  if (navigator.serviceWorker) {
    const register = navigator.serviceWorker.register.bind(navigator.serviceWorker);
    navigator.serviceWorker.register = (url, opts) => { seen.swScripts.push(String(url)); return register(url, opts); };
  }
})();
"""

_PERSISTENT = re.compile(r"\b(WebSocket|WebTransport|EventSource|RTCPeerConnection)\b")


def uncontained_channels(context, url: str, settle_ms: int = 800) -> list[str]:
    page = context.new_page()
    try:
        page.add_init_script(INSTRUMENT)
        page.goto(url)
        page.wait_for_timeout(settle_ms)
        seen = page.evaluate("() => window.__fillCertify")
        findings: list[str] = []
        if seen["webrtc"]:
            findings.append("WEBRTC")
        if seen["sharedWorker"]:
            findings.append("SHARED_WORKER")
        for script in seen["swScripts"]:
            source = page.request.get(urljoin(url, script)).text()
            if _PERSISTENT.search(source):
                findings.append("SW_PERSISTENT_CHANNEL")
        return sorted(set(findings))
    finally:
        page.close()
