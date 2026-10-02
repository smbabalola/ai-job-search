// Bundle 7 spec X5: credential actions (pair, sign out, device status) are
// honoured only from this extension's own pages, never from a content script.
import { describe, expect, it } from "vitest";

import { isExtensionPageSender } from "../src/background/sender";

const EXTENSION_ID = "abcdefghijklmnopabcdefghijklmnop";
const ROOT = `chrome-extension://${EXTENSION_ID}/`;

describe("isExtensionPageSender", () => {
  it("accepts the toolbar popup (no tab)", () => {
    expect(isExtensionPageSender({ id: EXTENSION_ID, url: `${ROOT}popup.html` }, EXTENSION_ID, ROOT)).toBe(true);
  });

  it("accepts the popup page opened in a tab", () => {
    expect(isExtensionPageSender({ id: EXTENSION_ID, url: `${ROOT}popup.html`, tab: { id: 7 } as chrome.tabs.Tab },
                                 EXTENSION_ID, ROOT)).toBe(true);
  });

  it("refuses a content script running in a web page", () => {
    expect(isExtensionPageSender({ id: EXTENSION_ID, url: "https://evil.example/", tab: { id: 7 } as chrome.tabs.Tab },
                                 EXTENSION_ID, ROOT)).toBe(false);
  });

  it("refuses another extension and a sender without a url", () => {
    expect(isExtensionPageSender({ id: "otherextension", url: `${ROOT}popup.html` }, EXTENSION_ID, ROOT)).toBe(false);
    expect(isExtensionPageSender({ id: EXTENSION_ID }, EXTENSION_ID, ROOT)).toBe(false);
  });

  it("refuses a look-alike origin that only shares a prefix", () => {
    expect(isExtensionPageSender({ id: EXTENSION_ID, url: `chrome-extension://${EXTENSION_ID}x/popup.html` },
                                 EXTENSION_ID, ROOT)).toBe(false);
  });
});
