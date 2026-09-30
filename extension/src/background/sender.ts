// True only for a message sent by one of this extension's own pages (the popup,
// whether opened from the toolbar or in a tab). A content script's sender url
// is the web page it runs in, so it can never pass (Bundle 7 spec X5).
export function isExtensionPageSender(
  sender: chrome.runtime.MessageSender,
  extensionId: string = chrome.runtime.id,
  extensionRoot: string = chrome.runtime.getURL(""),
): boolean {
  return sender.id === extensionId && typeof sender.url === "string" && sender.url.startsWith(extensionRoot);
}
