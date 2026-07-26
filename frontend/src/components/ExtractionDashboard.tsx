"use client";

import { AnimatePresence, motion } from "motion/react";
import {
  AlertTriangle,
  CheckCircle2,
  FileWarning,
  Info,
  ScanLine,
} from "lucide-react";

import type { FinancialDocument, ValidationState } from "@/lib/api";
import { cn, formatAmount, formatPercent } from "@/lib/utils";

interface ExtractionDashboardProps {
  document: FinancialDocument | null;
}

const BANNER: Record<
  ValidationState,
  { icon: typeof CheckCircle2; tone: string; title: string }
> = {
  validated: {
    icon: CheckCircle2,
    tone: "border-state-validated bg-state-validated/10 text-state-validated",
    title: "Validated",
  },
  mismatch: {
    icon: AlertTriangle,
    tone: "border-state-warning bg-state-warning/10 text-state-warning",
    title: "Mismatch",
  },
  incomplete: {
    icon: FileWarning,
    tone: "border-state-warning bg-state-warning/10 text-state-warning",
    title: "Incomplete",
  },
};

/** Confidence is fill AND colour, never colour alone (design.md §7). */
function ConfidenceDot({ band, title }: { band: string; title: string }) {
  return (
    <span
      title={title}
      className={cn(
        "inline-block size-2 rounded-full",
        band === "high" && "bg-state-validated",
        band === "medium" && "border-[1.5px] border-state-warning",
        band === "low" && "border-[1.5px] border-state-error"
      )}
    />
  );
}

function bannerBody(document: FinancialDocument): string {
  const { currency } = document;
  if (document.validation_state === "validated") {
    return `Line items + tax = ${formatAmount(document.total_amount)} ${currency}, matching the stated total.`;
  }
  if (document.validation_state === "mismatch") {
    const stated = Number(document.total_amount ?? 0);
    const computed = Number(document.computed_total);
    const difference = Math.abs(stated - computed).toFixed(2);
    return `Line items + tax = ${formatAmount(document.computed_total)}, but the document states ${formatAmount(document.total_amount)} (difference ${formatAmount(difference)}). Review the rows below.`;
  }
  return "The total could not be read from this document. Nothing has been assumed in its place.";
}

export function ExtractionDashboard({ document }: ExtractionDashboardProps) {
  return (
    <section className="flex min-h-0 flex-col gap-4">
      <h2 className="text-[11px] font-semibold uppercase tracking-[0.14em] text-ink-tertiary">
        Extraction dashboard
      </h2>

      <AnimatePresence mode="wait">
        {!document ? (
          <motion.div
            key="empty"
            initial={{ opacity: 0 }}
            animate={{ opacity: 1 }}
            exit={{ opacity: 0 }}
            className="rounded-2xl border border-edge-subtle bg-surface-raised p-6"
          >
            <p className="text-xs leading-relaxed text-ink-tertiary">
              Upload a document to see its extracted vendor, line items, and totals.
            </p>
          </motion.div>
        ) : (
          <motion.div
            key={document.document_id}
            initial={{ opacity: 0, y: 10 }}
            animate={{ opacity: 1, y: 0 }}
            exit={{ opacity: 0, y: -10 }}
            transition={{ duration: 0.3, ease: [0.16, 1, 0.3, 1] }}
            className="flex min-h-0 flex-col gap-3 overflow-y-auto pr-1"
          >
            {/* Vendor */}
            <motion.div
              initial={{ opacity: 0, y: 8 }}
              animate={{ opacity: 1, y: 0 }}
              transition={{ delay: 0.03 }}
              className="rounded-2xl border border-edge-subtle bg-surface-raised p-4"
            >
              <div className="flex items-start justify-between gap-3">
                <div className="min-w-0">
                  <h3 className="truncate text-lg font-semibold text-ink-primary">
                    {document.vendor_name}
                  </h3>
                  <p className="mt-1 text-xs text-ink-tertiary">
                    {[
                      document.invoice_number && `#${document.invoice_number}`,
                      document.billing_period_start && document.billing_period_end
                        ? `${document.billing_period_start} → ${document.billing_period_end}`
                        : document.billing_date,
                      `${document.page_count} page${document.page_count === 1 ? "" : "s"}`,
                    ]
                      .filter(Boolean)
                      .join(" · ")}
                  </p>
                </div>
                <span className="shrink-0 rounded-full bg-brand-600/20 px-2.5 py-1 text-[10px] font-semibold uppercase tracking-wide text-brand-500">
                  {document.document_type.replace("_", " ")}
                </span>
              </div>
              {document.used_ocr && (
                <div className="mt-3 flex items-center gap-1.5 text-[11px] text-ink-tertiary">
                  <ScanLine className="size-3.5" />
                  Read with local OCR — no text layer found
                </div>
              )}
            </motion.div>

            {/* Validation banner — arithmetic only. An advisory note must never turn a
                document whose maths is correct into a red mismatch (decision D-19). */}
            {(() => {
              const config = BANNER[document.validation_state];
              const Icon = config.icon;
              return (
                <motion.div
                  initial={{ opacity: 0, y: 8 }}
                  animate={{ opacity: 1, y: 0 }}
                  transition={{ delay: 0.06 }}
                  className={cn(
                    "flex items-start gap-2.5 rounded-xl border-l-2 px-3.5 py-3",
                    config.tone
                  )}
                >
                  <Icon className="mt-0.5 size-4 shrink-0" />
                  <p className="text-xs leading-relaxed">
                    <span className="font-semibold">{config.title}.</span>{" "}
                    {bannerBody(document)}
                  </p>
                </motion.div>
              );
            })()}

            {document.extraction_warnings.length > 0 && (
              <motion.div
                initial={{ opacity: 0, y: 8 }}
                animate={{ opacity: 1, y: 0 }}
                transition={{ delay: 0.09 }}
                className="flex items-start gap-2.5 rounded-xl border-l-2 border-state-info bg-state-info/10 px-3.5 py-3"
              >
                <Info className="mt-0.5 size-4 shrink-0 text-state-info" />
                <div className="text-xs leading-relaxed text-state-info">
                  <p className="font-semibold">Notes</p>
                  <ul className="mt-1 list-disc space-y-1 pl-4">
                    {document.extraction_warnings.map((warning) => (
                      <li key={warning}>{warning}</li>
                    ))}
                  </ul>
                </div>
              </motion.div>
            )}

            {/* Line items */}
            <motion.div
              initial={{ opacity: 0, y: 8 }}
              animate={{ opacity: 1, y: 0 }}
              transition={{ delay: 0.12 }}
              className="overflow-hidden rounded-2xl border border-edge-subtle bg-surface-raised"
            >
              {document.line_items.length === 0 ? (
                <p className="p-4 text-xs leading-relaxed text-ink-tertiary">
                  No line items detected. This may be a summary document — try asking a
                  question about it directly.
                </p>
              ) : (
                <div className="overflow-x-auto">
                  <table className="w-full text-xs">
                    <thead>
                      <tr className="border-b border-edge-default">
                        <th className="px-3 py-2.5 text-left text-[10px] font-semibold uppercase tracking-wide text-ink-tertiary">
                          Description
                        </th>
                        <th className="px-2 py-2.5 text-right text-[10px] font-semibold uppercase tracking-wide text-ink-tertiary">
                          Qty
                        </th>
                        <th className="px-2 py-2.5 text-right text-[10px] font-semibold uppercase tracking-wide text-ink-tertiary">
                          Unit
                        </th>
                        <th className="px-3 py-2.5 text-right text-[10px] font-semibold uppercase tracking-wide text-ink-tertiary">
                          Amount
                        </th>
                        <th className="px-2 py-2.5" />
                      </tr>
                    </thead>
                    <tbody>
                      {document.line_items.map((item, index) => (
                        <motion.tr
                          key={`${item.description}-${index}`}
                          initial={{ opacity: 0, x: -6 }}
                          animate={{ opacity: 1, x: 0 }}
                          transition={{ delay: 0.14 + index * 0.04 }}
                          className="border-b border-edge-subtle last:border-0 hover:bg-surface-hover"
                        >
                          <td className="max-w-[220px] px-3 py-2.5 text-ink-primary">
                            {item.description}
                          </td>
                          <td className="tabular px-2 py-2.5 text-right text-ink-secondary">
                            {item.quantity ?? "—"}
                          </td>
                          <td className="tabular px-2 py-2.5 text-right text-ink-secondary">
                            {item.unit_price ?? "—"}
                          </td>
                          <td className="tabular px-3 py-2.5 text-right font-medium text-ink-primary">
                            {formatAmount(item.amount)}
                          </td>
                          <td className="px-2 py-2.5 text-center">
                            <ConfidenceDot
                              band={item.confidence_band}
                              title={`confidence ${Math.round(item.confidence * 100)}%, page ${item.source_page}`}
                            />
                          </td>
                        </motion.tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
            </motion.div>

            {/* Totals */}
            <motion.div
              initial={{ opacity: 0, y: 8 }}
              animate={{ opacity: 1, y: 0 }}
              transition={{ delay: 0.18 }}
              className="rounded-2xl bg-surface-sunken p-4"
            >
              <div className="flex items-baseline justify-between py-1 text-xs text-ink-secondary">
                <span>Subtotal</span>
                <span className="tabular text-ink-primary">
                  {formatAmount(document.subtotal)}
                </span>
              </div>
              {document.tax_lines.map((tax) => {
                const rate = formatPercent(tax.rate);
                return (
                  <div
                    key={`${tax.label}-${tax.amount}`}
                    className="flex items-baseline justify-between py-1 text-xs text-ink-secondary"
                  >
                    <span>
                      {tax.label}
                      {rate ? ` (${rate}%)` : ""}
                    </span>
                    <span className="tabular text-ink-primary">
                      {formatAmount(tax.amount)}
                    </span>
                  </div>
                );
              })}
              <div className="mt-2 flex items-baseline justify-between border-t border-edge-default pt-3">
                <span className="text-sm font-semibold text-ink-primary">TOTAL</span>
                <motion.span
                  key={document.total_amount ?? "none"}
                  initial={{ opacity: 0, scale: 0.96 }}
                  animate={{ opacity: 1, scale: 1 }}
                  transition={{ delay: 0.22, type: "spring", stiffness: 260, damping: 20 }}
                  className="tabular text-base font-semibold text-ink-primary"
                >
                  {formatAmount(document.total_amount)}{" "}
                  <span className="text-xs text-ink-tertiary">{document.currency}</span>
                </motion.span>
              </div>
            </motion.div>
          </motion.div>
        )}
      </AnimatePresence>
    </section>
  );
}
