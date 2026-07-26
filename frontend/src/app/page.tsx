"use client";

import { motion } from "motion/react";
import { Activity, Cpu, Zap } from "lucide-react";
import { useCallback, useEffect, useState } from "react";

import { ChatPanel } from "@/components/ChatPanel";
import { DocumentPreviewer } from "@/components/DocumentPreviewer";
import { ExtractionDashboard } from "@/components/ExtractionDashboard";
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

  return (
    <main className="mx-auto flex h-dvh max-w-[1800px] flex-col gap-3 p-4">
      <motion.header
        initial={{ opacity: 0, y: -8 }}
        animate={{ opacity: 1, y: 0 }}
        transition={{ duration: 0.3, ease: [0.16, 1, 0.3, 1] }}
        className="flex shrink-0 items-center justify-between rounded-2xl border border-edge-subtle bg-surface-raised px-5 py-3"
      >
        <div className="flex items-baseline gap-2.5">
          <span className="text-base font-semibold tracking-tight text-ink-primary">
            FinLens
          </span>
          <span className="text-[11px] text-ink-tertiary">
            multimodal financial assistant
          </span>
        </div>
        <span className="text-[11px] text-ink-tertiary">
          {document ? `${document.filename} loaded` : "no document loaded"}
          {health ? ` · ${health.collections.policy_corpus ?? 0} policy chunks` : ""}
        </span>
      </motion.header>

      {/* Observability bar. The permanent $0.00 is the project's zero-cost thesis
          rendered as UI, not a placeholder (design.md §5.4). */}
      <motion.div
        initial={{ opacity: 0, y: -6 }}
        animate={{ opacity: 1, y: 0 }}
        transition={{ duration: 0.3, delay: 0.05 }}
        className="tabular flex shrink-0 flex-wrap items-center gap-x-5 gap-y-1.5 rounded-xl border border-edge-subtle bg-surface-sunken px-5 py-2.5 text-[11px] text-ink-secondary"
      >
        <span className="flex items-center gap-1.5">
          <Zap className="size-3 text-brand-500" />
          <span className="text-ink-primary">
            {health?.reasoning_model ?? "llama-3.3-70b-versatile"}
          </span>
        </span>
        <span className="text-edge-default">│</span>
        <span className="flex items-center gap-1.5">
          <Cpu className="size-3" />
          parse {document ? document.parse_seconds.toFixed(1) : "0.0"}s · generate{" "}
          {generateSeconds.toFixed(2)}s
        </span>
        <span className="text-edge-default">│</span>
        <span>
          ▤ {tokens.toLocaleString()} tokens ·{" "}
          <span className="text-state-validated">$0.00</span>
        </span>
        <span className="text-edge-default">│</span>
        <span className="flex items-center gap-1.5">
          <Activity className="size-3" />
          {offline ? (
            <span className="text-state-error">● API unreachable on :8000</span>
          ) : health?.llm_configured ? (
            <span className="text-state-validated">● Groq free tier</span>
          ) : (
            <span className="text-state-warning">● no API key configured</span>
          )}
        </span>
      </motion.div>

      {offline && (
        <div className="shrink-0 rounded-xl border-l-2 border-state-error bg-state-error/10 px-4 py-3 text-xs leading-relaxed text-state-error">
          Cannot reach the FinLens API at{" "}
          <span className="font-mono">http://localhost:8000</span>. Start it with{" "}
          <span className="font-mono">uvicorn api:app --port 8000</span> from the project
          root.
        </div>
      )}

      {/* Three columns on wide screens; stacked below 1280px so the line-item table is
          never squeezed until the Amount column clips (decision D-31). */}
      <div className="grid min-h-0 flex-1 grid-cols-1 gap-4 xl:grid-cols-[minmax(340px,0.9fr)_minmax(0,1fr)_minmax(340px,0.85fr)]">
        <div className="flex min-h-0 flex-col gap-4 overflow-y-auto pr-1">
          <Uploader
            samples={samples}
            onLoaded={(loaded) => {
              setCitation(null);
              setDocument(loaded);
            }}
            disabled={offline}
          />
          {/* Keyed on the document so page position resets on a new upload —
              the same remount pattern used for ChatPanel. */}
          <DocumentPreviewer
            key={document?.document_id ?? "empty"}
            document={document}
            citation={citation}
            onClearCitation={() => setCitation(null)}
          />
        </div>
        <div className="min-h-0">
          <ExtractionDashboard document={document} />
        </div>
        <div className="flex min-h-0 flex-col">
          {/* Keyed on the document so a new upload remounts the panel with fresh state.
              Resetting via an effect would cascade renders (react-hooks rule). */}
          <ChatPanel
            key={document?.document_id ?? "empty"}
            document={document}
            onStats={handleStats}
            onCite={setCitation}
          />
        </div>
      </div>
    </main>
  );
}
