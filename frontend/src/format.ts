export const n = (x: number | null | undefined) => (x ?? 0).toLocaleString("en-GB");

export const pct = (x: number) => `${x.toLocaleString("en-GB", { maximumFractionDigits: 1 })}%`;

export function dur(s: number) {
  if (s < 60) return `${Math.round(s)}s`;
  if (s < 3600) return `${Math.floor(s / 60)}m ${Math.round(s % 60)}s`;
  return `${Math.floor(s / 3600)}h ${Math.round((s % 3600) / 60)}m`;
}

export function seconds(from: string, to?: string | null) {
  return ((to ? Date.parse(to) : Date.now()) - Date.parse(from)) / 1000;
}

export const day = (ms: number) =>
  new Date(ms).toLocaleDateString("en-GB", { day: "numeric", month: "short", year: "numeric" });

export const plural = (k: number, one: string, many = `${one}s`) => `${n(k)} ${k === 1 ? one : many}`;

export const AGE_OPTIONS = [0, 7, 14, 30, 90, 180, 365, 730];
export const ageLabel = (d: number) =>
  d === 0 ? "Any age" : d < 30 ? `Older than ${d} days` : d < 365 ? `Older than ${Math.round(d / 30)} month${d >= 60 ? "s" : ""}` : `Older than ${d / 365} year${d >= 730 ? "s" : ""}`;
