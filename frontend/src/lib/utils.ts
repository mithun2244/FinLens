import { clsx, type ClassValue } from "clsx";
import { twMerge } from "tailwind-merge";

/** Merge Tailwind classes so a later class wins over an earlier conflicting one. */
export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs));
}

/**
 * Format a decimal string for display with thousands separators.
 *
 * Takes and returns strings. The value arrives from the backend as an exact decimal
 * string and is only ever formatted, never summed — JavaScript numbers cannot represent
 * currency exactly, which is why the API sends strings in the first place.
 */
export function formatAmount(value: string | null | undefined): string {
  if (value === null || value === undefined || value === "") return "—";
  const negative = value.trim().startsWith("-");
  const [whole, fraction = ""] = value.replace("-", "").split(".");
  const grouped = whole.replace(/\B(?=(\d{3})+(?!\d))/g, ",");
  const decimals = fraction.padEnd(2, "0").slice(0, Math.max(2, fraction.length));
  return `${negative ? "-" : ""}${grouped}.${decimals}`;
}

/** `"0.085"` → `"8.5"`, without the trailing zeros a naive multiply would leave. */
export function formatPercent(rate: string | null | undefined): string | null {
  if (!rate) return null;
  const parsed = Number(rate) * 100;
  if (!Number.isFinite(parsed)) return null;
  return String(Number(parsed.toFixed(4)));
}

export function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(0)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

export function fileExtension(name: string): string {
  const dot = name.lastIndexOf(".");
  return dot === -1 ? "FILE" : name.slice(dot + 1).toUpperCase();
}
