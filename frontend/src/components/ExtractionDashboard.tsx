"use client";

import { AnimatePresence, motion } from "motion/react";

import type { Citation, FinancialDocument, ValidationState } from "@/lib/api";
import { cn, formatAmount, formatPercent } from "@/lib/utils";
import { SectionLabel } from "@/components/Shell";

interface ExtractionDashboardProps {
  document: FinancialDocument | null;
  loading?: boolean;
  /** Clicking a field focuses its source page in the viewer. */
  onFocus?: (citation: Citation) => void;
}

const BANNER: Record<ValidationState, { tone: string; mark: string; title: string }> = {
  validated: {
    tone: "border-accent bg-accent/[0.07] text-accent",
    mark: "✓",
    title: "Validated",
  },
  mismatch: {
    tone: "border-state-warn bg-state-warn/10 text-state-warn",
    mark: "!",
    title: "Mismatch",
  },
  incomplete: {
    tone: "border-state-warn bg-state-warn/10 text-state-warn",
    mark: "?",
    title: "Incomplete",
  },
};

/** The design's confidence bands: >=90 accent, >=80 amber, <80 red. */
function bandStyle(band: string) {
  if (band === "high")
    return {
      fg: "hsl(119 99% 56%)",
      bg: "hsl(119 99% 46% / 0.1)",
      border: "hsl(119 99% 46% / 0.35)",
    };
  if (band === "medium")
    return {
      fg: "hsl(38 92% 62%)",
      bg: "hsl(38 92% 60% / 0.1)",
      border: "hsl(38 92% 60% / 0.35)",
    };
  return {
    fg: "hsl(0 84% 66%)",
    bg: "hsl(0 84% 60% / 0.12)",
    border: "hsl(0 84% 60% / 0.4)",
  };
}

interface FieldRow {
  label: string;
  value: string;
  page: number;
  /** Only set where the backend actually computed a confidence. */
  band?: string;
  confidence?: number;
  low?: boolean;
}

function buildFields(document: FinancialDocument): FieldRow[] {
  const rows: FieldRow[] = [
    { label: "VENDOR NAME", value: document.vendor_name, page: 1 },
    {
      label: "DOCUMENT DATE",
      value:
        document.billing_period_start && document.billing_period_end
          ? `${document.billing_period_start} → ${document.billing_period_end}`
          : (document.billing_date ?? "—"),
      page: 1,
    },
  ];

  if (document.invoice_number) {
    rows.push({ label: "REFERENCE", value: document.invoice_number, page: 1 });
  }

  // Line items are the only extracted values carrying a real confidence score, so the
  // weakest row sets the badge. Inventing a percentage for vendor or dates would be
  // precision this system never computed.
  if (document.line_items.length > 0) {
    const weakest = document.line_items.reduce((worst, item) =>
      item.confidence < worst.confidence ? item : worst
    );
    rows.push({
      label: "LINE ITEMS",
      value: `${document.line_items.length} rows · ${formatAmount(document.subtotal)} ${document.currency}`,
      page: weakest.source_page,
      band: weakest.confidence_band,
      confidence: weakest.confidence,
      low: weakest.confidence_band === "low",
    });
  }

  for (const tax of document.tax_lines) {
    const rate = formatPercent(tax.rate);
    rows.push({
      label: `TAX · ${tax.label.toUpperCase()}${rate ? ` ${rate}%` : ""}`,
      value: `${formatAmount(tax.amount)} ${document.currency}`,
      page: 1,
    });
  }

  rows.push({
    label: "TOTAL AMOUNT",
    value: `${formatAmount(document.total_amount)} ${document.currency}`,
    page: document.page_count,
  });

  return rows;
}

function focusFor(document: FinancialDocument, row: FieldRow): Citation {
  return {
    document_id: document.document_id,
    filename: document.filename,
    page: row.page,
    label: `${row.label} · p${row.page}`,
    snippet: `${row.label}: ${row.value}`,
    score: 1,
    chunk_type: "field",
    // No bounding box: these come from the structured record, not from retrieval.
    // The viewer marks the page rather than claiming a region it does not know.
    bbox: null,
  };
}

export function ExtractionDashboard({
  document,
  loading,
  onFocus,
}: ExtractionDashboardProps) {
  return (
    <div
      id="extract"
      style={{ animation: "fadeUp 0.7s cubic-bezier(0.16,1,0.3,1) 0.2s backwards" }}
      className="flex flex-none flex-col gap-3.5 border-b border-edge-subtle px-[18px] pb-4 pt-[18px]"
    >
      <SectionLabel
        index="02"
        title="Extracted fields"
        right={
          <span
            className="font-mono text-[9.5px]"
            style={{ color: loading ? "hsl(0 0% 50%)" : "hsl(119 99% 46%)" }}
          >
            {loading
              ? "PARSING…"
              : document
                ? `${document.line_items.length} ROWS · ${document.used_ocr ? "OCR" : "TEXT LAYER"}`
                : "IDLE"}
          </span>
        }
      />

      <AnimatePresence mode="wait">
        {loading ? (
          <motion.div
            key="skeleton"
            initial={{ opacity: 0 }}
            animate={{ opacity: 1 }}
            exit={{ opacity: 0 }}
            className="flex flex-col gap-[9px]"
          >
            {[100, 100, 88, 94, 76].map((w, i) => (
              <div
                key={i}
                style={{
                  height: 40,
                  width: `${w}%`,
                  background:
                    "linear-gradient(90deg, hsl(0 0% 15%) 25%, hsl(0 0% 22%) 50%, hsl(0 0% 15%) 75%)",
                  backgroundSize: "220% 100%",
                  animation: "shimmer 1.4s linear infinite",
                }}
                className="rounded-md"
              />
            ))}
          </motion.div>
        ) : !document ? (
          <motion.p
            key="empty"
            initial={{ opacity: 0 }}
            animate={{ opacity: 1 }}
            exit={{ opacity: 0 }}
            className="text-[11.5px] font-light leading-relaxed text-ink-faint"
          >
            Drop a document to see its vendor, line items and totals extracted here.
          </motion.p>
        ) : (
          <motion.div
            key={document.document_id}
            initial={{ opacity: 0, y: 8 }}
            animate={{ opacity: 1, y: 0 }}
            exit={{ opacity: 0 }}
            className="flex flex-col gap-2.5"
          >
            {/* Arithmetic only. An advisory note must never turn a document whose maths
                is correct into a red mismatch (decision D-19). */}
            {(() => {
              const banner = BANNER[document.validation_state];
              const difference =
                document.validation_state === "mismatch"
                  ? Math.abs(
                      Number(document.total_amount ?? 0) - Number(document.computed_total)
                    ).toFixed(2)
                  : null;
              return (
                <div
                  className={cn(
                    "flex items-start gap-2.5 rounded-[10px] border-l-2 px-3 py-2.5",
                    banner.tone
                  )}
                >
                  <span className="font-mono text-[11px] font-semibold">{banner.mark}</span>
                  <p className="text-[11px] font-light leading-relaxed">
                    <span className="font-medium">{banner.title}.</span>{" "}
                    {document.validation_state === "validated"
                      ? `Line items + tax = ${formatAmount(document.total_amount)} ${document.currency}, matching the stated total.`
                      : document.validation_state === "mismatch"
                        ? `Line items + tax = ${formatAmount(document.computed_total)}, but the document states ${formatAmount(document.total_amount)} (difference ${formatAmount(difference)}).`
                        : "The total could not be read. Nothing has been assumed in its place."}
                  </p>
                </div>
              );
            })()}

            {document.extraction_warnings.length > 0 && (
              <div className="rounded-[10px] border-l-2 border-state-info bg-state-info/10 px-3 py-2.5">
                <p className="font-mono text-[8.5px] tracking-[0.16em] text-state-info">
                  NOTES
                </p>
                <ul className="mt-1 list-disc space-y-1 pl-4 text-[11px] font-light leading-relaxed text-state-info">
                  {document.extraction_warnings.map((w) => (
                    <li key={w}>{w}</li>
                  ))}
                </ul>
              </div>
            )}

            <div className="flex flex-col gap-1.5">
              {buildFields(document).map((row, i) => {
                const style = row.band ? bandStyle(row.band) : null;
                return (
                  <motion.button
                    key={`${row.label}-${i}`}
                    type="button"
                    initial={{ opacity: 0, x: -5 }}
                    animate={{ opacity: 1, x: 0 }}
                    transition={{ delay: 0.03 * i }}
                    onClick={() => onFocus?.(focusFor(document, row))}
                    style={{
                      borderColor: row.low ? "hsl(0 84% 60% / 0.3)" : "hsl(0 0% 18%)",
                      background: row.low ? "hsl(0 84% 60% / 0.05)" : "hsl(0 0% 12%)",
                    }}
                    className="flex cursor-pointer items-center gap-2.5 rounded-[10px] border px-[11px] py-[9px] text-left transition-all hover:border-accent/50"
                  >
                    <div className="min-w-0 flex-1">
                      <div className="font-mono text-[8.5px] tracking-[0.16em] text-ink-muted">
                        {row.label}
                      </div>
                      <div className="tabular mt-[3px] truncate text-[12px] font-medium">
                        {row.value}
                      </div>
                    </div>
                    <span
                      className="whitespace-nowrap rounded-full border px-[7px] py-[3px] font-mono text-[8.5px] tracking-[0.08em]"
                      style={
                        style
                          ? { color: style.fg, background: style.bg, borderColor: style.border }
                          : {
                              color: "hsl(0 0% 56%)",
                              background: "hsl(0 0% 16%)",
                              borderColor: "hsl(0 0% 24%)",
                            }
                      }
                    >
                      {/* A percentage only where one was actually computed. */}
                      {row.confidence !== undefined
                        ? row.low
                          ? `REVIEW ${Math.round(row.confidence * 100)}%`
                          : `${Math.round(row.confidence * 100)}%`
                        : "VIEW"}
                    </span>
                  </motion.button>
                );
              })}
            </div>
          </motion.div>
        )}
      </AnimatePresence>
    </div>
  );
}
