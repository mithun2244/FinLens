"use client";

import { AnimatePresence, motion } from "motion/react";
import { useCallback, useEffect, useRef, useState } from "react";

import type { AnswerPayload, ChatTurn, Citation, FinancialDocument } from "@/lib/api";
import { loadPolicies, streamChat } from "@/lib/api";
import { SectionLabel } from "@/components/Shell";

interface Message {
  id: string;
  role: "user" | "assistant";
  content: string;
  answer?: AnswerPayload;
  error?: string;
}

interface ChatPanelProps {
  document: FinancialDocument | null;
  pagesIndexed?: number;
  onStats?: (tokens: number, seconds: number) => void;
  onCite?: (citation: Citation) => void;
}

const CITATION = /\[([^[\]]+?):\s*(?:p\.?\s*)?(\d+)\s*\]/g;

/** Inline `[file:page]` markers become clickable pills that drive the viewer. */
function renderWithCitations(
  text: string,
  answer?: AnswerPayload,
  onCite?: (citation: Citation) => void
) {
  const known = new Map(
    (answer?.citations ?? []).map((c) => [`${c.filename.toLowerCase()}:${c.page}`, c])
  );
  const nodes: React.ReactNode[] = [];
  let cursor = 0;
  let match: RegExpExecArray | null;
  CITATION.lastIndex = 0;

  while ((match = CITATION.exec(text)) !== null) {
    if (match.index > cursor) nodes.push(text.slice(cursor, match.index));
    const citation = known.get(`${match[1].trim().toLowerCase()}:${Number(match[2])}`);
    if (citation) {
      nodes.push(
        <button
          key={`${match.index}-cite`}
          type="button"
          onClick={() => onCite?.(citation)}
          title={citation.snippet}
          className="mx-0.5 inline-flex items-center gap-1.5 rounded-full border border-accent/40 bg-accent/[0.08] px-2 py-0.5 align-middle font-mono text-[9px] tracking-[0.08em] text-accent-bright transition-all hover:bg-accent/20 active:scale-[0.96]"
          style={{ boxShadow: "none" }}
        >
          <span className="size-1 rounded-full bg-accent" />
          {match[1].trim()} · p{match[2]}
        </button>
      );
    } else {
      // An invented reference stays visible as plain text rather than being swallowed.
      nodes.push(match[0]);
    }
    cursor = match.index + match[0].length;
  }
  if (cursor < text.length) nodes.push(text.slice(cursor));
  return nodes;
}

/** Every trust signal the backend computed, surfaced rather than hidden (Rule 5). */
function WarningStrips({ answer }: { answer: AnswerPayload }) {
  const strips: string[] = [];
  if (!answer.is_grounded && !answer.refused) {
    strips.push("This answer could not be traced to your documents. Treat it as unverified.");
  }
  if (answer.dropped_citations.length > 0) {
    strips.push(`Referenced sources not retrieved: ${answer.dropped_citations.join(", ")}.`);
  }
  for (const f of answer.contradicting_figures) {
    strips.push(
      `${f.claimed} does not match the extracted ${f.field ?? "record"} of ${f.expected ?? "—"}.`
    );
  }
  for (const f of answer.unsupported_figures) {
    strips.push(`${f} appears in neither the document nor the retrieved context.`);
  }
  if (strips.length === 0) return null;

  return (
    <div className="mt-1.5 flex max-w-[92%] flex-col gap-1.5">
      {strips.map((s) => (
        <div
          key={s}
          className="rounded-lg border-l-2 border-state-warn bg-state-warn/10 px-2.5 py-1.5 text-[10.5px] font-light leading-relaxed text-state-warn"
        >
          {s}
        </div>
      ))}
    </div>
  );
}

export function ChatPanel({
  document,
  pagesIndexed,
  onStats,
  onCite,
}: ChatPanelProps) {
  const [messages, setMessages] = useState<Message[]>([]);
  const [draft, setDraft] = useState("");
  const [streaming, setStreaming] = useState(false);
  const [stage, setStage] = useState<string | null>(null);
  const [partial, setPartial] = useState("");
  const [policiesLoaded, setPoliciesLoaded] = useState(false);

  const abortRef = useRef<AbortController | null>(null);
  const chatRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const el = chatRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [messages, partial, stage]);

  const ask = useCallback(
    async (question: string) => {
      if (!question.trim() || streaming) return;
      const history: ChatTurn[] = messages.map((m) => ({ role: m.role, content: m.content }));

      setMessages((prev) => [
        ...prev,
        { id: `u-${Date.now()}`, role: "user", content: question },
      ]);
      setDraft("");
      setStreaming(true);
      setPartial("");

      const controller = new AbortController();
      abortRef.current = controller;

      let text = "";
      let answer: AnswerPayload | undefined;
      let failure: string | undefined;

      try {
        for await (const event of streamChat(
          question,
          document?.document_id ?? null,
          history,
          controller.signal
        )) {
          if (event.type === "stage") setStage(event.stage);
          else if (event.type === "token") {
            text += event.token;
            setPartial(text);
          } else if (event.type === "answer") {
            answer = event.answer;
            text = event.answer.text;
            onStats?.(event.stats.total_tokens, event.stats.generate_seconds);
          } else if (event.type === "error") failure = event.message;
        }
      } catch (error) {
        if ((error as Error).name !== "AbortError") {
          failure = error instanceof Error ? error.message : "The request failed.";
        }
      }

      setStreaming(false);
      setStage(null);
      setPartial("");
      abortRef.current = null;
      setMessages((prev) => [
        ...prev,
        {
          id: `a-${Date.now()}`,
          role: "assistant",
          content: failure ?? text,
          answer,
          error: failure,
        },
      ]);
    },
    [document?.document_id, messages, onStats, streaming]
  );

  const chips = document?.suggested_prompts ?? [];

  return (
    <div id="assistant" className="flex min-h-0 flex-1 flex-col">
      <div className="flex flex-none items-center justify-between px-[18px] pb-2 pt-3">
        <SectionLabel index="03" title="Assistant" />
        <span className="font-mono text-[9px] tracking-[0.1em] text-ink-faint">
          {document
            ? `GROUNDED · ${pagesIndexed ?? document.page_count} PAGES INDEXED`
            : "AWAITING DOCUMENT"}
        </span>
      </div>

      <div
        ref={chatRef}
        className="flex min-h-0 flex-1 flex-col gap-3 overflow-y-auto px-[18px] pb-3 pt-1.5"
      >
        {messages.length === 0 && !streaming && (
          <p className="text-[11.5px] font-light leading-relaxed text-ink-faint">
            {document
              ? "Ask anything about this document. Answers come back with page-anchored citations you can click."
              : "Load a document to start asking questions."}
          </p>
        )}

        <AnimatePresence initial={false}>
          {messages.map((m) => {
            const user = m.role === "user";
            return (
              <motion.div
                key={m.id}
                initial={{ opacity: 0, y: 12, filter: "blur(3px)" }}
                animate={{ opacity: 1, y: 0, filter: "blur(0px)" }}
                transition={{ duration: 0.45, ease: [0.16, 1, 0.3, 1] }}
                className="flex flex-col gap-[7px]"
                style={{ alignItems: user ? "flex-end" : "flex-start" }}
              >
                <div
                  className="max-w-[88%] text-pretty whitespace-pre-wrap px-3 py-2.5 text-[12px] font-light leading-[1.6] backdrop-blur-md"
                  style={{
                    border: `1px solid ${user ? "hsl(119 99% 46% / 0.35)" : m.error ? "hsl(0 84% 60% / 0.4)" : "hsl(0 0% 19%)"}`,
                    background: user
                      ? "hsl(119 99% 46% / 0.12)"
                      : m.error
                        ? "hsl(0 84% 60% / 0.08)"
                        : "hsl(0 0% 13% / 0.72)",
                    color: m.error ? "hsl(0 84% 66%)" : user ? "hsl(0 0% 96%)" : "hsl(0 0% 88%)",
                    borderRadius: user ? "12px 12px 3px 12px" : "12px 12px 12px 3px",
                    boxShadow: user ? "0 0 20px -8px hsl(119 99% 46% / 0.5)" : "none",
                  }}
                >
                  {m.error ? m.content : renderWithCitations(m.content, m.answer, onCite)}
                </div>

                {m.answer && <WarningStrips answer={m.answer} />}

                {m.answer && (
                  <span className="tabular text-[9px] tracking-[0.1em] text-ink-faint">
                    {m.answer.latency_seconds.toFixed(2)}S ·{" "}
                    {m.answer.prompt_tokens + m.answer.completion_tokens} TOKENS · $0.00
                  </span>
                )}
              </motion.div>
            );
          })}
        </AnimatePresence>

        {partial && (
          <div
            className="max-w-[88%] self-start text-pretty whitespace-pre-wrap px-3 py-2.5 text-[12px] font-light leading-[1.6] text-[hsl(0_0%_88%)]"
            style={{
              border: "1px solid hsl(0 0% 19%)",
              background: "hsl(0 0% 13% / 0.72)",
              borderRadius: "12px 12px 12px 3px",
            }}
          >
            {partial}
            <span
              className="ml-0.5 inline-block h-3 w-1.5 -mb-px bg-accent align-baseline"
              style={{ animation: "blink 0.9s steps(1) infinite" }}
            />
          </div>
        )}

        {stage && !partial && (
          <div
            className="flex items-center gap-[7px] self-start rounded-[12px_12px_12px_3px] border border-edge-subtle bg-[hsl(0_0%_13%_/_0.7)] px-3 py-2.5"
            key={stage}
          >
            {[0, 0.2, 0.4].map((d) => (
              <div
                key={d}
                className="size-[5px] rounded-full bg-accent"
                style={{ animation: `blink 1s steps(1) ${d}s infinite` }}
              />
            ))}
            <span className="ml-1 font-mono text-[9px] uppercase tracking-[0.1em] text-ink-muted">
              {stage}
            </span>
          </div>
        )}
      </div>

      <div className="flex flex-none flex-col gap-2.5 border-t border-edge-subtle bg-[hsl(0_0%_11%)] px-[18px] pb-[18px] pt-2.5">
        <div className="flex flex-wrap gap-1.5">
          {!policiesLoaded && (
            <button
              type="button"
              onClick={async () => {
                try {
                  await loadPolicies();
                  setPoliciesLoaded(true);
                } catch {
                  /* the button stays available */
                }
              }}
              className="cursor-pointer rounded-full border border-edge-default bg-surface-lifted px-2.5 py-1.5 text-[10.5px] font-light text-ink-secondary transition-all hover:border-edge-strong hover:text-ink-primary active:scale-[0.97]"
            >
              + Load policy corpus
            </button>
          )}
          {chips.slice(0, 4).map((chip) => (
            <button
              key={chip}
              type="button"
              disabled={streaming}
              onClick={() => void ask(chip)}
              title={chip}
              className="cursor-pointer rounded-full border border-edge-default bg-surface-lifted px-2.5 py-1.5 text-[10.5px] font-light text-ink-secondary transition-all hover:border-edge-strong hover:text-ink-primary active:scale-[0.97] disabled:opacity-40"
            >
              {chip.length > 34 ? `${chip.slice(0, 32)}…` : chip}
            </button>
          ))}
        </div>

        <form
          onSubmit={(e) => {
            e.preventDefault();
            void ask(draft);
          }}
          className="flex items-center gap-2 rounded-xl border border-edge-default bg-surface-lifted py-1 pl-3 pr-1 transition-all focus-within:border-accent/60"
        >
          <input
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            disabled={!document || streaming}
            placeholder={document ? "Ask about this document…" : "Load a document first"}
            className="min-w-0 flex-1 border-none bg-transparent py-2 text-[12px] font-light text-ink-primary outline-none placeholder:text-ink-faint disabled:cursor-not-allowed"
          />
          {streaming ? (
            <button
              type="button"
              onClick={() => abortRef.current?.abort()}
              className="cursor-pointer rounded-[9px] border-none bg-surface-hover px-[15px] py-2.5 font-mono text-[10.5px] uppercase tracking-[0.1em] text-ink-secondary transition-all hover:text-state-bad"
            >
              Stop
            </button>
          ) : (
            <button
              type="submit"
              disabled={!document || !draft.trim()}
              className="cursor-pointer rounded-[9px] border-none bg-accent px-[15px] py-2.5 text-[10.5px] font-semibold uppercase tracking-[0.1em] text-accent-ink transition-all hover:brightness-110 active:scale-[0.96] disabled:opacity-30"
            >
              Send
            </button>
          )}
        </form>
      </div>
    </div>
  );
}
