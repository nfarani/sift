// The user's edits to recommendations, before they become a plan. Kept per browser only
// as a convenience; the plan built from it on the server is the source of truth.
//
// Excluded senders are email addresses, so the stored selection is personal data. It is only
// persisted for normal mode. In demo mode the selection lives in memory only (it holds masked
// addresses that mean nothing after a backend restart), and the real one is never loaded.
export interface RecChoice {
  selected: boolean;
  action: "archive" | "trash";
  older_than_days: number;
  excluded_senders: string[];
}
export type Selection = Record<string, RecChoice>;

const KEY = "sift.selection.v1";

export function loadSelection(): Selection {
  try {
    return JSON.parse(localStorage.getItem(KEY) || "{}");
  } catch {
    return {};
  }
}

export function saveSelection(s: Selection) {
  try {
    localStorage.setItem(KEY, JSON.stringify(s));
  } catch { /* storage unavailable: selection just won't survive reload */ }
}
