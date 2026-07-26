"use client";

import { AnimatePresence, motion } from "motion/react";
import {
  AlertCircle,
  CheckCircle2,
  FileText,
  Image as ImageIcon,
  Loader2,
  UploadCloud,
} from "lucide-react";
import { useCallback, useRef, useState } from "react";

import type { FinancialDocument, SampleDocument } from "@/lib/api";
import { loadSample, uploadDocument } from "@/lib/api";
import { cn, fileExtension, formatBytes } from "@/lib/utils";

type Status =
  | { phase: "idle" }
  | { phase: "working"; label: string; filename: string }
  | { phase: "done"; filename: string; seconds: number }
  | { phase: "error"; message: string };

interface UploaderProps {
  samples: SampleDocument[];
  onLoaded: (document: FinancialDocument) => void;
  disabled?: boolean;
}

const ACCEPTED = [".pdf", ".png", ".jpg", ".jpeg", ".webp"];

/** Parsing runs locally and takes seconds; naming the stage reads as competence where a
 *  bare spinner reads as stalling (design.md §1). These are indicative, not streamed. */
const STAGES = [
  "Reading the file…",
  "Detecting page layout…",
  "Extracting table structure…",
  "Building the search index…",
];

export function Uploader({ samples, onLoaded, disabled }: UploaderProps) {
  const [status, setStatus] = useState<Status>({ phase: "idle" });
  const [dragging, setDragging] = useState(false);
  const inputRef = useRef<HTMLInputElement>(null);
  const stageTimer = useRef<ReturnType<typeof setInterval> | null>(null);

  const stopStages = useCallback(() => {
    if (stageTimer.current) {
      clearInterval(stageTimer.current);
      stageTimer.current = null;
    }
  }, []);

  const run = useCallback(
    async (filename: string, task: () => Promise<FinancialDocument>) => {
      const started = performance.now();
      let index = 0;
      setStatus({ phase: "working", label: STAGES[0], filename });
      stageTimer.current = setInterval(() => {
        index = Math.min(index + 1, STAGES.length - 1);
        setStatus({ phase: "working", label: STAGES[index], filename });
      }, 1400);

      try {
        const document = await task();
        stopStages();
        setStatus({
          phase: "done",
          filename,
          seconds: (performance.now() - started) / 1000,
        });
        onLoaded(document);
      } catch (error) {
        stopStages();
        setStatus({
          phase: "error",
          message: error instanceof Error ? error.message : "Upload failed.",
        });
      }
    },
    [onLoaded, stopStages]
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

  return (
    <section className="flex flex-col gap-4">
      <header className="flex items-baseline justify-between">
        <h2 className="text-[11px] font-semibold uppercase tracking-[0.14em] text-ink-tertiary">
          Document workspace
        </h2>
        {status.phase === "done" && (
          <span className="tabular text-[11px] text-state-validated">
            {status.seconds.toFixed(1)}s
          </span>
        )}
      </header>

      <motion.div
        onDragOver={(event) => {
          event.preventDefault();
          if (!busy) setDragging(true);
        }}
        onDragLeave={() => setDragging(false)}
        onDrop={(event) => {
          event.preventDefault();
          setDragging(false);
          if (!busy) handleFiles(event.dataTransfer.files);
        }}
        onClick={() => !busy && inputRef.current?.click()}
        animate={{
          scale: dragging ? 1.015 : 1,
          borderColor: dragging ? "var(--color-brand-500)" : "var(--color-edge-default)",
          backgroundColor: dragging
            ? "rgba(42,167,154,0.07)"
            : "var(--color-surface-raised)",
        }}
        whileHover={busy ? undefined : { borderColor: "var(--color-edge-strong)" }}
        transition={{ duration: 0.16, ease: [0.16, 1, 0.3, 1] }}
        className={cn(
          "relative cursor-pointer rounded-2xl border-2 border-dashed p-8 text-center",
          busy && "cursor-wait opacity-80"
        )}
      >
        <input
          ref={inputRef}
          type="file"
          accept={ACCEPTED.join(",")}
          className="hidden"
          onChange={(event) => handleFiles(event.target.files)}
        />

        <AnimatePresence mode="wait">
          {status.phase === "working" ? (
            <motion.div
              key="working"
              initial={{ opacity: 0, y: 6 }}
              animate={{ opacity: 1, y: 0 }}
              exit={{ opacity: 0, y: -6 }}
              className="flex flex-col items-center gap-3"
            >
              <Loader2 className="size-7 animate-spin text-brand-500" />
              <p className="text-sm font-medium text-ink-primary">{status.filename}</p>
              <motion.p
                key={status.label}
                initial={{ opacity: 0 }}
                animate={{ opacity: 1 }}
                className="tabular text-xs text-ink-tertiary"
              >
                {status.label}
              </motion.p>
            </motion.div>
          ) : (
            <motion.div
              key="idle"
              initial={{ opacity: 0, y: 6 }}
              animate={{ opacity: 1, y: 0 }}
              exit={{ opacity: 0, y: -6 }}
              className="flex flex-col items-center gap-3"
            >
              <motion.div
                animate={{ y: dragging ? -4 : 0 }}
                transition={{ type: "spring", stiffness: 300, damping: 20 }}
              >
                <UploadCloud
                  className={cn(
                    "size-9 transition-colors",
                    dragging ? "text-brand-500" : "text-ink-tertiary"
                  )}
                />
              </motion.div>
              <p className="text-sm font-semibold text-ink-primary">
                Drop a financial document here
              </p>
              <p className="text-xs leading-relaxed text-ink-tertiary">
                PDF, PNG, JPG · up to 25 MB
                <br />
                Parsed{" "}
                <span className="font-medium text-ink-secondary">
                  locally on your machine
                </span>{" "}
                — the document never leaves it
              </p>
              <div className="mt-1 flex flex-wrap justify-center gap-1.5">
                {ACCEPTED.map((extension) => (
                  <span
                    key={extension}
                    className="rounded-full border border-edge-subtle bg-surface-sunken px-2 py-0.5 text-[10px] font-medium uppercase tracking-wide text-ink-tertiary"
                  >
                    {extension.slice(1)}
                  </span>
                ))}
              </div>
            </motion.div>
          )}
        </AnimatePresence>
      </motion.div>

      <AnimatePresence>
        {status.phase === "error" && (
          <motion.div
            initial={{ opacity: 0, height: 0 }}
            animate={{ opacity: 1, height: "auto" }}
            exit={{ opacity: 0, height: 0 }}
            className="flex items-start gap-2 rounded-lg border-l-2 border-state-error bg-state-error/10 px-3 py-2.5"
          >
            <AlertCircle className="mt-0.5 size-4 shrink-0 text-state-error" />
            <p className="text-xs leading-relaxed text-state-error">{status.message}</p>
          </motion.div>
        )}
        {status.phase === "done" && (
          <motion.div
            initial={{ opacity: 0, height: 0 }}
            animate={{ opacity: 1, height: "auto" }}
            exit={{ opacity: 0, height: 0 }}
            className="flex items-center gap-2 rounded-lg border-l-2 border-state-validated bg-state-validated/10 px-3 py-2.5"
          >
            <CheckCircle2 className="size-4 shrink-0 text-state-validated" />
            <p className="text-xs text-state-validated">
              {status.filename} parsed and indexed
            </p>
          </motion.div>
        )}
      </AnimatePresence>

      {samples.length > 0 && (
        <div className="flex flex-col gap-2">
          <p className="text-[11px] text-ink-tertiary">Or try a sample:</p>
          <div className="grid grid-cols-2 gap-2">
            {samples.map((sample, index) => {
              const isImage = /\.(png|jpe?g|webp)$/i.test(sample.filename);
              return (
                <motion.button
                  key={sample.filename}
                  type="button"
                  disabled={busy}
                  initial={{ opacity: 0, y: 8 }}
                  animate={{ opacity: 1, y: 0 }}
                  transition={{ delay: 0.04 * index, duration: 0.25 }}
                  whileHover={busy ? undefined : { y: -2 }}
                  whileTap={busy ? undefined : { scale: 0.98 }}
                  onClick={() =>
                    void run(sample.filename, () => loadSample(sample.filename))
                  }
                  className={cn(
                    "flex items-center gap-2 rounded-xl border border-edge-subtle bg-surface-raised px-3 py-2.5 text-left",
                    "transition-colors hover:border-brand-500/60 hover:bg-surface-hover",
                    "focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-brand-500",
                    busy && "cursor-not-allowed opacity-50"
                  )}
                >
                  {isImage ? (
                    <ImageIcon className="size-4 shrink-0 text-ink-tertiary" />
                  ) : (
                    <FileText className="size-4 shrink-0 text-ink-tertiary" />
                  )}
                  <span className="truncate text-xs text-ink-secondary">
                    {sample.label}
                  </span>
                </motion.button>
              );
            })}
          </div>
        </div>
      )}
    </section>
  );
}

export { formatBytes };
