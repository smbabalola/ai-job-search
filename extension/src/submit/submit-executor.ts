// Bundle 6E-A SUBMIT_CLICK (spec E13): the one click in the extension, in the
// ISOLATED world, on the one certified submit control the page bundle has
// just re-found by its bound fingerprint. The 6D-B executor still never clicks.
export function submitClick(el: Element): void { (el as HTMLElement).click(); }
