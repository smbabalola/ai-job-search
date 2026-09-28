// Bundle 6D-B popup states (spec §16.3). There is deliberately no release
// control: the only exit from a quarantined tab is closing it.
import type { ControllerView } from "../fill/run-controller";

export const FILLED_TEXT = "Filled. This page is quarantined and cannot send anything. "
  + "Submission is a later, separate step.";
const CLOSE_AND_RESTART = new Set(["WRITE_OUTCOME_UNKNOWN", "EXECUTOR_LOST", "QUARANTINE_LOST"]);

function escapeHtml(text: string): string {
  return text.replace(/[&<>"']/g, (c) => `&#${c.charCodeAt(0)};`);
}

export function renderFillView(view: ControllerView | null, permissionsGranted: boolean): string {
  if (!permissionsGranted) {
    return '<p>Safe FILL needs permission to check for other tabs of the employer site.</p>'
      + '<button id="enable-fill">Enable safe FILL</button>';
  }
  if (view === null) return '<button id="start-fill">Fill this page (quarantined)</button>';
  const reason = escapeHtml(view.reason ?? "unknown");
  const abort = '<button id="abort-fill">Abort: close this tab</button>';
  switch (view.phase) {
    case "OBSERVING":
      return "<p>Observing…</p>" + abort;
    case "NEEDS_REVIEW":
      return '<p>Needs your review in <a href="http://127.0.0.1:8420/" target="_blank">JobSearch</a>.</p>' + abort;
    case "UNSUPPORTED":
      return `<p>Unsupported (${escapeHtml(String((view.detail.causes as string[] | undefined)?.join(", ") ?? view.reason ?? "unknown"))}).</p>` + abort;
    case "PREPARING":
      return "<p>Quarantined · preparing</p>" + abort;
    case "FILLING":
      return `<p>Quarantined · filling ${view.progress?.done ?? 0}/${view.progress?.total ?? 0}</p>` + abort;
    case "FILLED":
      return `<p id="filled">${FILLED_TEXT}</p>` + abort;
    case "STOPPED": {
      const next = CLOSE_AND_RESTART.has(view.reason ?? "")
        ? "Close this tab and start fresh."
        : "Resolve the cause in JobSearch, then open the job page fresh and start again.";
      return `<p>Stopped (${reason}). ${next}</p>` + abort;
    }
  }
}
