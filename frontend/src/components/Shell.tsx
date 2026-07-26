"use client";

import { motion } from "motion/react";
import type { ReactNode } from "react";

import type { HealthPayload } from "@/lib/api";

/** Section marker: "01 / INGEST". The design's primary wayfinding device. */
export function SectionLabel({
  index,
  title,
  right,
}: {
  index: string;
  title: string;
  right?: ReactNode;
}) {
  return (
    <div className="flex items-center justify-between">
      <span className="micro">
        {index} / {title}
      </span>
      {right}
    </div>
  );
}

export function AppHeader({
  health,
  offline,
  documentName,
}: {
  health: HealthPayload | null;
  offline: boolean;
  documentName: string | null;
}) {
  const links = [
    ["#hero", "Overview"],
    ["#ingest", "Ingest"],
    ["#viewer", "Viewer"],
    ["#extract", "Extraction"],
    ["#assistant", "Assistant"],
  ] as const;

  return (
    <header
      style={{ animation: "fadeIn 0.5s ease-out forwards" }}
      className="sticky top-0 z-60 flex items-center justify-between gap-6 border-b border-edge-hair bg-surface-base/55 px-6 py-3 backdrop-blur-lg"
    >
      <div className="flex items-center gap-3">
        <div
          className="flex size-[22px] items-center justify-center rounded-md bg-accent"
          style={{ boxShadow: "0 0 18px -4px hsl(119 99% 46% / 0.7)" }}
        >
          <div className="size-[7px] rounded-[2px] bg-accent-ink" />
        </div>
        <div className="flex items-baseline gap-2.5">
          <span className="text-[17px] font-semibold tracking-[-0.02em]">FINLENS</span>
          <span className="hidden font-mono text-[9.5px] uppercase tracking-[0.18em] text-ink-faint sm:inline">
            multimodal document intelligence
          </span>
        </div>
      </div>

      <nav className="hidden gap-7 lg:flex">
        {links.map(([href, label]) => (
          <a
            key={href}
            href={href}
            className="text-[10.5px] uppercase tracking-[0.2em] text-ink-secondary transition-colors hover:text-ink-primary"
          >
            {label}
          </a>
        ))}
      </nav>

      <div className="flex items-center gap-3">
        {/* The design's badge read "VISION MODEL ONLINE". This system has no vision
            model — Groq serves none, and scanned pages go through local OCR instead
            (decision D-15). The badge shows the model actually in use. */}
        <div className="flex items-center gap-[7px] rounded-full border border-edge-default bg-surface-raised px-[11px] py-[5px]">
          <div
            className={
              offline
                ? "size-1.5 rounded-full bg-state-bad"
                : "size-1.5 rounded-full bg-accent"
            }
            style={offline ? undefined : { animation: "blink 2.2s ease-in-out infinite" }}
          />
          <span className="font-mono text-[9.5px] tracking-[0.12em] text-ink-secondary">
            {offline
              ? "API OFFLINE"
              : health?.llm_configured
                ? `${health.reasoning_model.toUpperCase()} · LOCAL OCR`
                : "NO API KEY"}
          </span>
        </div>
        <span className="hidden font-mono text-[9.5px] tracking-[0.1em] text-ink-faint xl:inline">
          {documentName ?? "NO DOCUMENT"}
        </span>
      </div>
    </header>
  );
}

export function Hero() {
  const rise = (delay: number) => ({
    opacity: 0,
    animation: `fadeUp 0.7s cubic-bezier(0.16,1,0.3,1) ${delay}s forwards`,
  });

  return (
    <section
      id="hero"
      className="relative z-10 flex min-h-[88vh] items-end overflow-hidden"
    >
      <div className="pointer-events-none w-full max-w-[900px] px-10 pb-14 pt-32">
        <div className="mb-[22px] flex items-center gap-2.5" style={rise(0.1)}>
          <div
            className="size-1.5 rounded-full bg-accent"
            style={{ boxShadow: "0 0 12px hsl(119 99% 46%)" }}
          />
          <span className="font-mono text-[10px] uppercase tracking-[0.24em] text-ink-secondary">
            Multimodal document intelligence
          </span>
        </div>

        <h1
          className="m-0 mb-4 text-[clamp(3rem,8vw,6rem)] font-bold uppercase leading-[1.05] tracking-[-0.05em]"
          style={rise(0.2)}
        >
          FinLens<span className="text-accent"> AI</span>
        </h1>

        <p
          className="m-0 mb-[18px] text-[clamp(1.125rem,2.5vw,1.875rem)] font-light text-ink-primary/80"
          style={rise(0.4)}
        >
          We read financial documents correctly.
        </p>

        <p
          className="m-0 mb-[30px] max-w-[660px] text-pretty text-[clamp(0.875rem,1.5vw,1.25rem)] font-light leading-[1.6] text-ink-secondary"
          style={rise(0.55)}
        >
          Invoices, statements and scanned receipts parsed in seconds. Layout-aware
          extraction with page-anchored citations, so every number traces back to the
          region it came from.
        </p>

        <div className="flex flex-wrap gap-3" style={rise(0.7)}>
          <a
            href="#workspace"
            className="pointer-events-auto rounded-[3px] bg-accent px-[30px] py-[15px] text-[13px] font-bold tracking-[0.06em] text-accent-ink transition-all hover:brightness-110 active:scale-[0.97]"
          >
            Open the workspace
          </a>
          <a
            href="#extract"
            className="pointer-events-auto rounded-[3px] bg-white px-[30px] py-[15px] text-[13px] font-bold tracking-[0.06em] text-[hsl(0_0%_10%)] transition-all hover:brightness-90 active:scale-[0.97]"
          >
            See an extraction
          </a>
        </div>

        {/* The design's strapline claimed "TRUSTED BY 34 FINANCE TEAMS · 1.2M PAGES
            PARSED". That is invented social proof, so it is replaced with claims this
            project can actually evidence: the stack is local-first and free, and the
            extraction accuracy figure comes from the measured eval run. */}
        <p
          className="m-0 mt-[26px] font-mono text-[10px] tracking-[0.14em] text-ink-secondary/60"
          style={rise(0.85)}
        >
          PARSED LOCALLY · 100% TOTAL-AMOUNT ACCURACY ON THE FIXTURE SET · $0.00 TO RUN
        </p>
      </div>
    </section>
  );
}

/** Slow flowing gradients behind a dark scrim — the imported design used a video. */
export function Backdrop() {
  return (
    <div className="fixed inset-0 z-0 overflow-hidden bg-surface-base">
      <motion.div
        initial={{ opacity: 0 }}
        animate={{ opacity: 1 }}
        transition={{ duration: 1.2 }}
        className="backdrop-flow absolute inset-[-10%]"
      />
      <div
        className="pointer-events-none absolute inset-0"
        style={{
          background:
            "linear-gradient(to bottom, hsl(0 0% 6% / 0.72), hsl(0 0% 6% / 0.86))",
        }}
      />
    </div>
  );
}
