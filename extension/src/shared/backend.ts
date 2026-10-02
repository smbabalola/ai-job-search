// Bundle 7 spec X1/X2: the one JobSearch backend this build talks to, and the
// bearer headers every backend call carries.
//
// __JOBSEARCH_ORIGIN__ is a build-time constant (scripts/build.mjs --origin);
// modules imported by unit tests read it defensively, so the dev default
// applies when no build define exists.
declare const __JOBSEARCH_ORIGIN__: string | undefined;

export const DEV_BACKEND_ORIGIN = "http://127.0.0.1:8420";
export const BACKEND_ORIGIN: string =
  typeof __JOBSEARCH_ORIGIN__ === "string" ? __JOBSEARCH_ORIGIN__ : DEV_BACKEND_ORIGIN;

export type AuthHeaderProvider = () => Promise<Record<string, string>>;

let provider: AuthHeaderProvider = async () => ({});

// The background worker installs the TokenClient here once at start-up;
// every backend client merges these headers into each request.
export function setAuthHeaderProvider(next: AuthHeaderProvider): void {
  provider = next;
}

export function authHeaders(): Promise<Record<string, string>> {
  return provider();
}
