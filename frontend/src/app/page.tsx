"use client";

import { AnimatePresence } from "motion/react";
import { useCallback, useEffect, useState } from "react";

import { ChatPanel } from "@/components/ChatPanel";
import { DocumentPreviewer } from "@/components/DocumentPreviewer";
import { ExtractionDashboard } from "@/components/ExtractionDashboard";
import { AppHeader, Backdrop, Hero } from "@/components/Shell";
import { Uploader } from "@/components/Uploader";
import type {
  Citation,
  FinancialDocument,
  HealthPayload,
  SampleDocument,
} from "@/lib/api";
import { fetchHealth, fetchSamples } from "@/lib/api";

export default function Home() {
  const [document, setDocument] = useState<FinancialDocument | null>(null);
  const [samples, setSamples] = useState<SampleDocument[]>([]);
  const [health, setHealth] = useState<HealthPayload | null>(null);
  const [offline, setOffline] = useState(false);
  const [tokens, setTokens] = useState(0);
  const [generateSeconds, setGenerateSeconds] = useState(0);
  const [citation, setCitation] = useState<Citation | null>(null);
  const [parsing, setParsing] = useState(false);

  // The hero is the landing state. The moment there is something to work on — a
  // document loaded, or one being parsed — it collapses so the workspace occupies
  // the viewport without the user having to scroll.
  const showHero = !document && !parsing;

  useEffect(() => {
    let cancelled = false;
    void (async () => {
      try {
        const [healthPayload, samplePayload] = await Promise.all([
          fetchHealth(),
          fetchSamples(),
        ]);
        if (cancelled) return;
        setHealth(healthPayload);
        setSamples(samplePayload);
        setOffline(false);
      } catch {
        if (!cancelled) setOffline(true);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  const handleStats = useCallback((total: number, seconds: number) => {
    setTokens((previous) => previous + total);
    setGenerateSeconds(seconds);
  }, []);

  const handleLoaded = useCallback((loaded: FinancialDocument) => {
    setCitation(null);
    setDocument(loaded);
  }, []);

  // Collapsing the hero removes content from above the fold, so anyone who had
  // scrolled would be left mid-page. Returning to the top keeps the workspace where
  // the animation puts it. A scroll is a DOM side effect, which is what effects are
  // for — unlike the setState-in-effect pattern the lint rule forbids.
  useEffect(() => {
    if (!showHero) window.scrollTo({ top: 0, behavior: "smooth" });
  }, [showHero]);

  return (
    <div className="relative flex min-h-screen flex-col overflow-x-auto">
      <Backdrop />

      <div className="relative z-10 flex min-h-screen flex-col">
        <AppHeader
          health={health}
          offline={offline}
          documentName={document?.filename ?? null}
        />

        <AnimatePresence initial={false}>{showHero && <Hero />}</AnimatePresence>

        {offline && (
          <div className="relative z-10 mx-10 mb-4 rounded-xl border-l-2 border-state-bad bg-state-bad/10 px-4 py-3 text-[12px] font-light leading-relaxed text-state-bad">
            Cannot reach the FinLens API at{" "}
            <span className="font-mono">http://localhost:8000</span>. Start it with{" "}
            <span className="font-mono">uvicorn api:app --port 8000</span> from the
            project root.
          </div>
        )}

        {/* The design fixes the workspace at 100vh with a 1180px minimum and lets the
            page scroll sideways. That is right for a three-panel tool: squeezing the
            columns is what clipped the Amount column off the line-item table before
            (decision D-31). Below 1280px the panels stack instead. */}
        <main
          id="workspace"
          className="relative z-10 grid min-h-[680px] grid-cols-1 xl:h-screen xl:min-w-[1180px] xl:grid-cols-[clamp(250px,19vw,320px)_minmax(430px,1fr)_clamp(330px,25vw,420px)]"
        >
          <Uploader
            samples={samples}
            onLoaded={handleLoaded}
            onBusyChange={setParsing}
            disabled={offline}
          />

          <DocumentPreviewer
            key={document?.document_id ?? "empty"}
            document={document}
            citation={citation}
            onClearCitation={() => setCitation(null)}
          />

          <section className="flex min-h-0 flex-col border-l border-edge-hair bg-surface-panel/[0.66] backdrop-blur-xl">
            <ExtractionDashboard document={document} onFocus={setCitation} />
            <ChatPanel
              key={document?.document_id ?? "empty"}
              document={document}
              pagesIndexed={document?.page_count}
              onStats={handleStats}
              onCite={setCitation}
            />
          </section>
        </main>

        {/* Observability strip. The permanent $0.00 is the project's zero-cost thesis
            rendered as UI, not a placeholder. */}
        <footer className="relative z-10 flex flex-wrap items-center gap-x-6 gap-y-2 border-t border-edge-hair bg-surface-base/70 px-6 py-3 font-mono text-[9.5px] tracking-[0.1em] text-ink-muted backdrop-blur-lg">
          <span className="text-ink-secondary">
            {health?.reasoning_model ?? "llama-3.3-70b-versatile"}
          </span>
          <span className="text-edge-default">│</span>
          <span>
            PARSE {document ? document.parse_seconds.toFixed(1) : "0.0"}S · GENERATE{" "}
            {generateSeconds.toFixed(2)}S
          </span>
          <span className="text-edge-default">│</span>
          <span>
            {tokens.toLocaleString()} TOKENS · <span className="text-accent">$0.00</span>
          </span>
          <span className="text-edge-default">│</span>
          <span>
            {health ? `${health.collections.policy_corpus ?? 0} POLICY CHUNKS` : "—"}
          </span>
        </footer>
      </div>
    </div>
  );
}
