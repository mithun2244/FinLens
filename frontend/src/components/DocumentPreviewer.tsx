"use client";

import { AnimatePresence, motion } from "motion/react";
import { useState } from "react";

import type { Citation, FinancialDocument } from "@/lib/api";
import { pageImageUrl } from "@/lib/api";

interface DocumentPreviewerProps {
  document: FinancialDocument | null;
  citation: Citation | null;
  onClearCitation: () => void;
}

/**
 * The document is the hero: it is the evidence behind every number, so it gets the
 * largest region of the screen and every citation lands on it.
 *
 * Highlighting needs no PDF rendering library. The backend supplies normalized top-left
 * bounding boxes, so an absolutely-positioned box over the page image is the whole
 * mechanism (decision D-16).
 */
export function DocumentPreviewer({
  document,
  citation,
  onClearCitation,
}: DocumentPreviewerProps) {
  const [page, setPage] = useState(1);

  // Adjusted during render, not in an effect: an effect renders once with the stale
  // page and again with the right one, which is a visible flash as well as a lint error.
  const [seenCitation, setSeenCitation] = useState(citation);
  if (citation !== seenCitation) {
    setSeenCitation(citation);
    if (citation && citation.page !== page) setPage(citation.page);
  }

  const [loadedKey, setLoadedKey] = useState<string | null>(null);

  return (
    <section
      id="viewer"
      className="flex min-h-0 flex-col bg-[hsl(0_0%_7%_/_0.55)] backdrop-blur-lg"
    >
      <div className="flex flex-wrap items-center justify-between gap-x-4 gap-y-2 border-b border-edge-subtle bg-[hsl(0_0%_10%_/_0.8)] px-5 py-[11px] backdrop-blur-md">
        <div className="flex min-w-0 items-center gap-2.5">
          <span className="truncate text-[12px] font-medium">
            {document?.filename ?? "No document loaded"}
          </span>
          {document && (
            <span
              className="whitespace-nowrap rounded-full border px-[7px] py-[3px] font-mono text-[9px] tracking-[0.1em]"
              style={
                document.used_ocr
                  ? {
                      color: "hsl(38 92% 60%)",
                      borderColor: "hsl(38 92% 60% / 0.4)",
                      background: "hsl(38 92% 60% / 0.08)",
                    }
                  : {
                      color: "hsl(119 99% 46%)",
                      borderColor: "hsl(119 99% 46% / 0.4)",
                      background: "hsl(119 99% 46% / 0.08)",
                    }
              }
            >
              {document.used_ocr ? "OCR" : "TEXT LAYER"}
            </span>
          )}
        </div>

        {document && (
          <div className="flex items-center gap-2">
            <button
              type="button"
              onClick={() => setPage((p) => Math.max(1, p - 1))}
              disabled={page <= 1}
              aria-label="Previous page"
              className="rounded px-1.5 font-mono text-[12px] text-ink-muted transition-colors hover:text-ink-primary disabled:opacity-30"
            >
              ‹
            </button>
            <span className="font-mono text-[9.5px] text-ink-muted">
              PAGE {page} / {document.page_count}
            </span>
            <button
              type="button"
              onClick={() => setPage((p) => Math.min(document.page_count, p + 1))}
              disabled={page >= document.page_count}
              aria-label="Next page"
              className="rounded px-1.5 font-mono text-[12px] text-ink-muted transition-colors hover:text-ink-primary disabled:opacity-30"
            >
              ›
            </button>
            <div className="h-3.5 w-px bg-edge-default" />
            <span className="font-mono text-[9.5px] text-ink-muted">
              {document.parse_seconds.toFixed(1)}s
            </span>
            {citation && (
              <button
                type="button"
                onClick={onClearCitation}
                className="ml-1.5 cursor-pointer rounded-md border border-edge-default bg-surface-lifted px-[11px] py-1.5 font-mono text-[9px] tracking-[0.12em] text-ink-secondary transition-all hover:bg-surface-hover hover:text-ink-primary"
              >
                CLEAR HIGHLIGHT
              </button>
            )}
          </div>
        )}
      </div>

      <div
        className="flex min-h-0 flex-1 justify-center overflow-y-auto p-[26px]"
        style={{
          backgroundImage: "radial-gradient(hsl(0 0% 16%) 1px, transparent 1px)",
          backgroundSize: "22px 22px",
        }}
      >
        {!document ? (
          <div className="self-center text-center">
            <p className="font-mono text-[10px] tracking-[0.2em] text-ink-faint">
              AWAITING DOCUMENT
            </p>
            <p className="mt-2 text-[12px] font-light text-ink-muted">
              The rendered page appears here, with every citation anchored to it.
            </p>
          </div>
        ) : (
          (() => {
            const imageKey = `${document.document_id}-${page}`;
            const loaded = loadedKey === imageKey;
            const highlight = citation && citation.page === page ? citation.bbox : null;
            const citedThisPage = citation?.page === page;
            return (
              <motion.div
                initial={{ opacity: 0, y: 14 }}
                animate={{ opacity: 1, y: 0 }}
                transition={{ duration: 0.7, ease: [0.16, 1, 0.3, 1] }}
                className="relative h-fit w-full max-w-[620px] overflow-hidden rounded"
                style={{
                  boxShadow:
                    "0 30px 60px -20px rgba(0,0,0,0.8), 0 0 0 1px hsl(0 0% 24%)",
                }}
              >
                {!loaded && (
                  <div className="absolute inset-0 grid place-items-center bg-surface-raised">
                    <span className="font-mono text-[9px] tracking-[0.2em] text-ink-faint">
                      RENDERING
                    </span>
                  </div>
                )}
                {/* eslint-disable-next-line @next/next/no-img-element -- a same-session
                    API stream already sized correctly; next/image would add a loader
                    round-trip for no benefit. */}
                <img
                  key={imageKey}
                  src={pageImageUrl(document.document_id, page)}
                  alt={`${document.filename}, page ${page}`}
                  onLoad={() => setLoadedKey(imageKey)}
                  className="block w-full bg-white"
                />

                <AnimatePresence>
                  {highlight && (
                    <motion.div
                      key={`${citation?.label}-${page}`}
                      initial={{ opacity: 0, scale: 0.96 }}
                      animate={{ opacity: 1, scale: 1 }}
                      exit={{ opacity: 0 }}
                      transition={{ duration: 0.35, ease: [0.16, 1, 0.3, 1] }}
                      style={{
                        left: `${highlight.left * 100}%`,
                        top: `${highlight.top * 100}%`,
                        width: `${(highlight.right - highlight.left) * 100}%`,
                        height: `${(highlight.bottom - highlight.top) * 100}%`,
                        border: "1.5px solid hsl(119 99% 40%)",
                        background: "hsl(119 99% 46% / 0.16)",
                        boxShadow:
                          "0 0 0 4px hsl(119 99% 46% / 0.14), 0 0 28px 2px hsl(119 99% 46% / 0.45), inset 0 0 18px hsl(119 99% 46% / 0.18)",
                      }}
                      className="pointer-events-none absolute rounded-[5px]"
                    >
                      <span className="absolute -top-[11px] -left-px whitespace-nowrap rounded-[3px] bg-[hsl(119_99%_40%)] px-[7px] py-0.5 font-mono text-[8.5px] font-medium tracking-[0.14em] text-[hsl(0_0%_6%)]">
                        {citation?.label}
                      </span>
                    </motion.div>
                  )}
                </AnimatePresence>

                {/* No bounding box — the record gives a page, not a region. Marking the
                    page is honest; drawing a box would claim precision we lack. */}
                {citedThisPage && !highlight && (
                  <div className="pointer-events-none absolute inset-0 rounded border-2 border-accent/60">
                    <span className="absolute left-2 top-2 rounded-[3px] bg-[hsl(119_99%_40%)] px-[7px] py-0.5 font-mono text-[8.5px] font-medium tracking-[0.14em] text-[hsl(0_0%_6%)]">
                      {citation?.label} · PAGE LEVEL
                    </span>
                  </div>
                )}
              </motion.div>
            );
          })()
        )}
      </div>
    </section>
  );
}
