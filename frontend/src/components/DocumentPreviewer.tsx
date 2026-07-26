"use client";

import { AnimatePresence, motion } from "motion/react";
import { ChevronLeft, ChevronRight, FileSearch, X } from "lucide-react";
import { useState } from "react";

import type { Citation, FinancialDocument } from "@/lib/api";
import { pageImageUrl } from "@/lib/api";
import { cn } from "@/lib/utils";

interface DocumentPreviewerProps {
  document: FinancialDocument | null;
  citation: Citation | null;
  onClearCitation: () => void;
}

/**
 * The document is the hero (design.md §2). It is the evidence behind every number, so it
 * gets the highest-fidelity region of the screen and every citation lands on it.
 *
 * Highlighting needs no PDF rendering library: the backend supplies normalized top-left
 * bounding boxes, so an absolutely-positioned div over a page image is the whole
 * mechanism. That is what settled the Streamlit-versus-React question (D-29) — and it
 * ports to React unchanged.
 */
export function DocumentPreviewer({
  document,
  citation,
  onClearCitation,
}: DocumentPreviewerProps) {
  const [page, setPage] = useState(1);

  // A citation drives the previewer to its page — the moment the product proves itself,
  // so it takes precedence over wherever the user had navigated.
  //
  // Adjusted during render rather than in an effect. Clearing state inside an effect
  // renders once with the stale page and again with the right one, which is both a
  // visible flash and what react-hooks/set-state-in-effect flags. React documents this
  // pattern for "adjust state when a prop changes".
  const [seenCitation, setSeenCitation] = useState(citation);
  if (citation !== seenCitation) {
    setSeenCitation(citation);
    if (citation && citation.page !== page) setPage(citation.page);
  }

  // Which page image has finished loading, derived rather than reset by an effect.
  // Switching page or document changes the key, so the placeholder returns for free.
  const [loadedKey, setLoadedKey] = useState<string | null>(null);

  if (!document) return null;

  const imageKey = `${document.document_id}-${page}`;
  const loaded = loadedKey === imageKey;

  const pageCount = document.page_count;
  const highlight = citation && citation.page === page ? citation.bbox : null;
  const citedThisPage = citation?.page === page;

  return (
    <section className="flex flex-col gap-2">
      <div className="flex items-center justify-between">
        <h2 className="text-[11px] font-semibold uppercase tracking-[0.14em] text-ink-tertiary">
          Preview
        </h2>
        {pageCount > 1 && (
          <div className="flex items-center gap-1">
            <button
              type="button"
              onClick={() => setPage((p) => Math.max(1, p - 1))}
              disabled={page <= 1}
              aria-label="Previous page"
              className="grid size-6 place-items-center rounded-md text-ink-tertiary transition-colors hover:bg-surface-hover hover:text-ink-primary disabled:opacity-30 disabled:hover:bg-transparent"
            >
              <ChevronLeft className="size-3.5" />
            </button>
            <span className="tabular px-1 text-[11px] text-ink-tertiary">
              {page} / {pageCount}
            </span>
            <button
              type="button"
              onClick={() => setPage((p) => Math.min(pageCount, p + 1))}
              disabled={page >= pageCount}
              aria-label="Next page"
              className="grid size-6 place-items-center rounded-md text-ink-tertiary transition-colors hover:bg-surface-hover hover:text-ink-primary disabled:opacity-30 disabled:hover:bg-transparent"
            >
              <ChevronRight className="size-3.5" />
            </button>
          </div>
        )}
      </div>

      <motion.div
        initial={{ opacity: 0, y: 8 }}
        animate={{ opacity: 1, y: 0 }}
        transition={{ duration: 0.28, ease: [0.16, 1, 0.3, 1] }}
        // White backdrop in a dark UI, deliberately: you do not invert someone's
        // invoice (design.md §3.1).
        className="relative overflow-hidden rounded-xl border border-edge-default bg-white"
      >
        {!loaded && (
          <div className="absolute inset-0 grid place-items-center bg-surface-raised">
            <FileSearch className="size-5 animate-pulse text-ink-tertiary" />
          </div>
        )}

        {/* eslint-disable-next-line @next/next/no-img-element -- next/image optimizes
            remote URLs through a loader; this is a same-session API stream that is
            already sized correctly, so the plain element is the honest choice. */}
        <img
          key={imageKey}
          src={pageImageUrl(document.document_id, page)}
          alt={`${document.filename}, page ${page}`}
          onLoad={() => setLoadedKey(imageKey)}
          className="block w-full"
        />

        <AnimatePresence>
          {highlight && (
            <motion.div
              key={`${citation?.label}-${page}`}
              initial={{ opacity: 0 }}
              // Pulses twice, then settles. The one deliberate flourish in the product:
              // it is the moment a cited number is shown to be real (design.md §3.4).
              animate={{ opacity: [0, 1, 0.35, 1, 0.55, 1] }}
              exit={{ opacity: 0 }}
              transition={{ duration: 1.6, ease: "easeOut" }}
              style={{
                left: `${highlight.left * 100}%`,
                top: `${highlight.top * 100}%`,
                width: `${(highlight.right - highlight.left) * 100}%`,
                height: `${(highlight.bottom - highlight.top) * 100}%`,
              }}
              className="pointer-events-none absolute rounded-sm border-[1.5px] border-state-info bg-state-info/20"
            />
          )}
        </AnimatePresence>

        {/* A citation with no bbox still navigates to the right page — the page border
            marks it rather than pretending to a precision we do not have. */}
        {citedThisPage && !highlight && (
          <div className="pointer-events-none absolute inset-0 rounded-xl border-2 border-state-info/60" />
        )}
      </motion.div>

      <AnimatePresence>
        {citation && (
          <motion.div
            initial={{ opacity: 0, height: 0 }}
            animate={{ opacity: 1, height: "auto" }}
            exit={{ opacity: 0, height: 0 }}
            className="flex items-start gap-2 rounded-lg border border-state-info/30 bg-state-info/10 px-2.5 py-2"
          >
            <div className="min-w-0 flex-1">
              <p className="tabular text-[10px] text-state-info">
                {citation.label} · relevance {citation.score.toFixed(2)}
                {!citation.bbox && " · page-level"}
              </p>
              <p className="mt-0.5 truncate text-[11px] text-ink-secondary">
                {citation.snippet}
              </p>
            </div>
            <button
              type="button"
              onClick={onClearCitation}
              aria-label="Clear highlight"
              className={cn(
                "grid size-5 shrink-0 place-items-center rounded text-ink-tertiary",
                "transition-colors hover:bg-surface-hover hover:text-ink-primary"
              )}
            >
              <X className="size-3" />
            </button>
          </motion.div>
        )}
      </AnimatePresence>
    </section>
  );
}
