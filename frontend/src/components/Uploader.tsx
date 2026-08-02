"use client";

import { AnimatePresence, motion } from "motion/react";
import { useCallback, useRef, useState } from "react";

import type { FinancialDocument, SampleDocument } from "@/lib/api";
import { loadSample, uploadDocument } from "@/lib/api";
import { cn, fileExtension } from "@/lib/utils";
import { SectionLabel } from "@/components/Shell";

type Status =
  | { phase: "idle" }
  | { phase: "working"; filename: string }
  | { phase: "done"; filename: string; seconds: number }
  | { phase: "error"; message: string };

interface UploaderProps {
  samples: SampleDocument[];
  onLoaded: (document: FinancialDocument) => void;
  disabled?: boolean;
  /** Raised the moment work starts, so the shell can collapse the hero immediately
   *  rather than waiting for the document to finish parsing. */
  onBusyChange?: (busy: boolean) => void;
}

/** PDF only.
 *
 *  Images used to be accepted and OCR'd; that path went with Docling, and the backend
 *  now rejects them at upload. Listing them here produced a file picker that let you
 *  choose a .png and then failed server-side — the UI promising what the API refuses.
 */
const ACCEPTED = [".pdf"];

/** The triage pipeline, worded to match what the backend actually does. */
const STEPS = [
  "Checking text layer",
  "Reading tables & layout",
  "Extracting line items",
  "Linking numeric entities",
];

/** File-type badges, mapped to what each type means for the pipeline. */
const KINDS = [
  { label: "PDF", note: "native text", color: "text-state-bad" },
  { label: "MULTI", note: "tabular", color: "text-state-info" },
];

function TriageRing({ pct }: { pct: number }) {
  const circumference = 188.5;
  return (
    <div className="relative size-[62px] flex-none">
      <svg viewBox="0 0 72 72" className="size-[62px] -rotate-90">
        <circle cx="36" cy="36" r="30" fill="none" stroke="hsl(0 0% 20%)" strokeWidth="5" />
        <circle
          cx="36"
          cy="36"
          r="30"
          fill="none"
          stroke="hsl(119 99% 46%)"
          strokeWidth="5"
          strokeLinecap="round"
          strokeDasharray={circumference}
          strokeDashoffset={circumference - (circumference * pct) / 100}
          style={{
            transition: "stroke-dashoffset 0.25s linear",
            filter: "drop-shadow(0 0 5px hsl(119 99% 46% / 0.7))",
          }}
        />
      </svg>
      <div className="absolute inset-0 flex items-center justify-center font-mono text-[13px] font-medium">
        {pct}%
      </div>
    </div>
  );
}

export function Uploader({
  samples,
  onLoaded,
  disabled,
  onBusyChange,
}: UploaderProps) {
  const [status, setStatus] = useState<Status>({ phase: "idle" });
  const [pct, setPct] = useState(0);
  const [dragging, setDragging] = useState(false);
  const inputRef = useRef<HTMLInputElement>(null);
  const timers = useRef<ReturnType<typeof setInterval>[]>([]);

  const clearTimers = useCallback(() => {
    timers.current.forEach(clearInterval);
    timers.current = [];
  }, []);

  const run = useCallback(
    async (filename: string, task: () => Promise<FinancialDocument>) => {
      const started = performance.now();
      clearTimers();
      setPct(0);
      setStatus({ phase: "working", filename });
      onBusyChange?.(true);

      // The ring advances on a timer while the real work happens. It is a progress
      // *indication*, not a measurement — the backend does not stream parse progress,
      // and it is capped below 100 so it never claims completion the work has not
      // reached.
      //
      // The step is derived from `pct` at render rather than tracked alongside it. It
      // used to be its own state, advanced from inside this interval using `pct` from
      // the enclosing closure — which is fixed at the value `run` was called with, so
      // `Math.floor((0 + 2) / 25)` evaluated to 0 on every tick and the checklist sat
      // frozen on the first step for the whole upload while the ring filled beside it.
      timers.current.push(
        setInterval(() => setPct((p) => Math.min(94, p + 2)), 90)
      );

      try {
        const document = await task();
        clearTimers();
        setPct(100);
        setStatus({
          phase: "done",
          filename,
          seconds: (performance.now() - started) / 1000,
        });
        onLoaded(document);
        onBusyChange?.(false);
      } catch (error) {
        clearTimers();
        setPct(0);
        setStatus({
          phase: "error",
          message: error instanceof Error ? error.message : "Upload failed.",
        });
        // Released on failure too: a failed parse must not strand the hero collapsed
        // with nothing behind it.
        onBusyChange?.(false);
      }
    },
    [clearTimers, onBusyChange, onLoaded]
  );

  const handleFiles = useCallback(
    (files: FileList | null) => {
      const file = files?.[0];
      if (!file) return;
      const extension = `.${fileExtension(file.name).toLowerCase()}`;
      if (!ACCEPTED.includes(extension)) {
        setStatus({
          phase: "error",
          message: `Cannot read ${extension} files. Supported: ${ACCEPTED.join(", ")}.`,
        });
        return;
      }
      void run(file.name, () => uploadDocument(file));
    },
    [run]
  );

  const busy = status.phase === "working" || disabled;
  const running = status.phase === "working";
  const step = Math.min(STEPS.length - 1, Math.floor(pct / (100 / STEPS.length)));

  return (
    <section
      id="ingest"
      style={{ animation: "fadeUp 0.7s cubic-bezier(0.16,1,0.3,1) 0.1s backwards" }}
      className="flex min-h-0 flex-col gap-[18px] overflow-y-auto border-r border-edge-hair bg-surface-panel/[0.66] px-[18px] pb-7 pt-6 backdrop-blur-xl"
    >
      <SectionLabel
        index="01"
        title="Ingest"
        right={
          <span className="font-mono text-[9.5px] text-accent">
            {samples.length + (status.phase === "done" ? 1 : 0)} FILES
          </span>
        }
      />

      <div
        onDragOver={(e) => {
          e.preventDefault();
          if (!busy) setDragging(true);
        }}
        onDragLeave={() => setDragging(false)}
        onDrop={(e) => {
          e.preventDefault();
          setDragging(false);
          if (!busy) handleFiles(e.dataTransfer.files);
        }}
        onClick={() => !busy && inputRef.current?.click()}
        // Focusable and operable from the keyboard. It looks like a button and is the
        // primary action in the panel, but a bare div with onClick is reachable only by
        // mouse — the file input it drives is visually hidden, so there was no keyboard
        // path to uploading a document at all.
        role="button"
        tabIndex={busy ? -1 : 0}
        aria-label="Choose a PDF to ingest"
        aria-disabled={busy}
        onKeyDown={(e) => {
          if (busy) return;
          if (e.key === "Enter" || e.key === " ") {
            e.preventDefault();
            inputRef.current?.click();
          }
        }}
        style={{
          borderColor: dragging ? "hsl(119 99% 46%)" : "hsl(0 0% 22%)",
          background: dragging ? "hsl(119 99% 46% / 0.07)" : "hsl(0 0% 11%)",
          transform: `scale(${dragging ? 1.025 : 1})`,
          transition:
            "transform 0.35s cubic-bezier(0.16,1,0.3,1), background 0.3s, border-color 0.3s",
        }}
        className={cn(
          "flex cursor-pointer flex-col items-center gap-3 rounded-[14px] border-[1.5px] border-dashed px-4 py-[26px] text-center",
          busy && "cursor-wait opacity-80"
        )}
      >
        <input
          ref={inputRef}
          type="file"
          accept={ACCEPTED.join(",")}
          className="hidden"
          onChange={(e) => handleFiles(e.target.files)}
        />
        <div
          className="flex size-11 items-center justify-center rounded-xl border border-edge-default bg-surface-lifted"
          style={{ animation: dragging ? "pulseRing 1.1s ease-in-out infinite" : "none" }}
        >
          <div
            className="size-[15px] rotate-45 rounded border-2 border-accent"
            style={{ borderBottomColor: "transparent", borderRightColor: "transparent" }}
          />
        </div>
        <div>
          <div className="mb-1 text-[13px] font-medium">
            {dragging ? "Release to ingest" : "Drop financial documents"}
          </div>
          <div className="font-mono text-[9.5px] tracking-[0.1em] text-ink-muted">
            PDF · INVOICE · STATEMENT
          </div>
        </div>
        <span className="rounded bg-accent px-[15px] py-[7px] text-[11px] font-semibold uppercase tracking-[0.1em] text-accent-ink transition-[filter] hover:brightness-110">
          Browse files
        </span>
      </div>

      <div className="flex gap-[7px]">
        {KINDS.map((k) => (
          <div
            key={k.label}
            className="flex-1 rounded-lg border border-edge-subtle bg-surface-raised px-1.5 py-2 text-center"
          >
            <div className={cn("font-mono text-[10px] font-medium", k.color)}>{k.label}</div>
            <div className="mt-0.5 text-[9px] text-ink-faint">{k.note}</div>
          </div>
        ))}
      </div>

      <div className="flex flex-col gap-3.5 rounded-[14px] border border-edge-subtle bg-surface-raised/70 px-3.5 py-[15px] backdrop-blur-md">
        <div className="flex items-center gap-3.5">
          <TriageRing pct={pct} />
          <div className="min-w-0">
            <div className="mb-[5px] font-mono text-[9px] tracking-[0.2em] text-ink-faint">
              TRIAGE PIPELINE
            </div>
            <div
              className={cn(
                "text-[12px] font-medium leading-[1.35]",
                running ? "text-ink-primary" : "text-accent"
              )}
            >
              {running
                ? STEPS[step]
                : status.phase === "done"
                  ? `Triage complete · ${status.seconds.toFixed(1)}s`
                  : "Awaiting a document"}
            </div>
          </div>
        </div>

        <div className="flex flex-col gap-[9px]">
          {STEPS.map((label, i) => {
            const done = status.phase === "done" || (running && i < step);
            const active = running && i === step;
            return (
              <div key={label} className="flex items-center gap-[9px]">
                <div
                  className="flex size-3.5 flex-none items-center justify-center rounded border font-mono text-[8px] font-semibold text-accent-ink transition-all"
                  style={{
                    borderColor: done
                      ? "hsl(119 99% 46%)"
                      : active
                        ? "hsl(119 99% 46% / 0.6)"
                        : "hsl(0 0% 26%)",
                    background: done
                      ? "hsl(119 99% 46%)"
                      : active
                        ? "hsl(119 99% 46% / 0.22)"
                        : "transparent",
                  }}
                >
                  {done ? "✓" : ""}
                </div>
                <span
                  className="text-[11.5px] font-light transition-colors"
                  style={{
                    color: done
                      ? "hsl(0 0% 84%)"
                      : active
                        ? "hsl(0 0% 96%)"
                        : "hsl(0 0% 42%)",
                  }}
                >
                  {label}
                </span>
              </div>
            );
          })}
        </div>
      </div>

      <AnimatePresence>
        {status.phase === "error" && (
          <motion.div
            initial={{ opacity: 0, height: 0 }}
            animate={{ opacity: 1, height: "auto" }}
            exit={{ opacity: 0, height: 0 }}
            className="rounded-lg border-l-2 border-state-bad bg-state-bad/10 px-3 py-2.5 text-[11px] leading-relaxed text-state-bad"
          >
            {status.message}
          </motion.div>
        )}
      </AnimatePresence>

      <SectionLabel index="Q" title="Queue" />

      <div className="flex flex-col gap-2">
        {samples.map((sample, index) => {
          const active = status.phase === "done" && status.filename === sample.filename;
          const kind = sample.filename.includes("statement") ? "MULTI" : "PDF";
          const kindColor = kind === "MULTI" ? "hsl(199 80% 62%)" : "hsl(0 84% 66%)";
          return (
            <motion.button
              key={sample.filename}
              type="button"
              disabled={busy}
              initial={{ opacity: 0, y: 8 }}
              animate={{ opacity: 1, y: 0 }}
              transition={{ delay: 0.04 * index }}
              onClick={() => void run(sample.filename, () => loadSample(sample.filename))}
              style={{
                borderColor: active ? "hsl(119 99% 46% / 0.45)" : "hsl(0 0% 18%)",
                background: active ? "hsl(119 99% 46% / 0.05)" : "hsl(0 0% 12%)",
                boxShadow: active ? "0 0 22px -8px hsl(119 99% 46% / 0.7)" : "none",
              }}
              className="flex cursor-pointer items-center gap-[11px] rounded-[11px] border px-3 py-[11px] text-left transition-all disabled:cursor-not-allowed disabled:opacity-50"
            >
              <div
                className="flex h-[34px] w-[30px] flex-none items-end justify-center rounded border bg-surface-raised pb-1"
                style={{ borderColor: kindColor }}
              >
                <span
                  className="font-mono text-[7.5px] tracking-[0.06em]"
                  style={{ color: kindColor }}
                >
                  {kind}
                </span>
              </div>
              <div className="min-w-0 flex-1">
                <div className="truncate text-[11.5px] font-medium">{sample.label}</div>
                <div className="mt-[3px] truncate font-mono text-[9px] text-ink-faint">
                  {sample.filename}
                </div>
              </div>
              <span
                className="whitespace-nowrap font-mono text-[8.5px] tracking-[0.1em]"
                style={{ color: active ? "hsl(119 99% 46%)" : "hsl(0 0% 50%)" }}
              >
                {active ? "ACTIVE" : "READY"}
              </span>
            </motion.button>
          );
        })}
      </div>
    </section>
  );
}
