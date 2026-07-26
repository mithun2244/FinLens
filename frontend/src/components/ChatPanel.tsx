"use client";

import { AnimatePresence, motion } from "motion/react";
import { AlertTriangle, ArrowUp, BookOpen, Quote, Square } from "lucide-react";
import { useCallback, useEffect, useRef, useState } from "react";

import type { AnswerPayload, ChatTurn, FinancialDocument } from "@/lib/api";
import { loadPolicies, streamChat } from "@/lib/api";

interface Message {
  id: string;
  role: "user" | "assistant";
  content: string;
  answer?: AnswerPayload;
  error?: string;
}

interface ChatPanelProps {
  document: FinancialDocument | null;
  onStats?: (tokens: number, seconds: number) => void;
}

const CITATION = /\[([^[\]]+?):\s*(?:p\.?\s*)?(\d+)\s*\]/g;

/**
 * Render inline `[file:page]` markers as chips.
 *
 * A marker that matched no retrieved chunk is left as plain text rather than removed —
 * an invented reference must stay visible, not be silently swallowed (Rule 5).
 */
function renderWithCitations(text: string, answer?: AnswerPayload) {
  const known = new Set(
    (answer?.citations ?? []).map((c) => `${c.filename.toLowerCase()}:${c.page}`)
  );
  const nodes: React.ReactNode[] = [];
  let cursor = 0;
  let match: RegExpExecArray | null;
  CITATION.lastIndex = 0;

  while ((match = CITATION.exec(text)) !== null) {
    if (match.index > cursor) nodes.push(text.slice(cursor, match.index));
    const key = `${match[1].trim().toLowerCase()}:${Number(match[2])}`;
    if (known.has(key)) {
      nodes.push(
        <span
          key={`${match.index}-cite`}
          className="mx-0.5 inline-flex items-center gap-1 rounded-full bg-state-info/15 px-2 py-0.5 align-middle font-mono text-[10px] text-state-info"
        >
          <Quote className="size-2.5" />
          {match[1].trim()} · p.{match[2]}
        </span>
      );
    } else {
      nodes.push(match[0]);
    }
    cursor = match.index + match[0].length;
  }
  if (cursor < text.length) nodes.push(text.slice(cursor));
  return nodes;
}

/** Every trust signal the backend computed, surfaced rather than hidden. */
function WarningStrips({ answer }: { answer: AnswerPayload }) {
  const strips: string[] = [];
  if (!answer.is_grounded && !answer.refused) {
    strips.push("This answer could not be traced to your documents. Treat it as unverified.");
  }
  if (answer.dropped_citations.length > 0) {
    strips.push(
      `Referenced sources that were not retrieved: ${answer.dropped_citations.join(", ")}.`
    );
  }
  for (const figure of answer.contradicting_figures) {
    strips.push(
      `The figure ${figure.claimed} does not match the extracted ${figure.field ?? "record"} of ${figure.expected ?? "—"}.`
    );
  }
  for (const figure of answer.unsupported_figures) {
    strips.push(
      `The figure ${figure} does not appear in the document or the retrieved context.`
    );
  }
  if (strips.length === 0) return null;

  return (
    <div className="mt-2 flex flex-col gap-1.5">
      {strips.map((strip) => (
        <motion.div
          key={strip}
          initial={{ opacity: 0, x: -4 }}
          animate={{ opacity: 1, x: 0 }}
          className="flex items-start gap-1.5 rounded-lg border-l-2 border-state-warning bg-state-warning/10 px-2.5 py-2"
        >
          <AlertTriangle className="mt-px size-3 shrink-0 text-state-warning" />
          <p className="text-[11px] leading-relaxed text-state-warning">{strip}</p>
        </motion.div>
      ))}
    </div>
  );
}

export function ChatPanel({ document, onStats }: ChatPanelProps) {
  const [messages, setMessages] = useState<Message[]>([]);
  const [draft, setDraft] = useState("");
  const [streaming, setStreaming] = useState(false);
  const [stage, setStage] = useState<string | null>(null);
  const [partial, setPartial] = useState("");
  const [policiesLoaded, setPoliciesLoaded] = useState(false);

  const abortRef = useRef<AbortController | null>(null);
  const bottomRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth", block: "end" });
  }, [messages, partial, stage]);

  // Resetting on a new document is handled by remounting: page.tsx keys this component
  // on document_id. That is React's own answer to "reset state when a prop changes" —
  // clearing state inside an effect triggers a cascading render, which the
  // react-hooks/set-state-in-effect rule flags.
  //
  // The reset matters: carrying history across documents would let the query rewriter
  // resolve "this charge" against the previous invoice.

  const ask = useCallback(
    async (question: string) => {
      if (!question.trim() || streaming) return;

      const history: ChatTurn[] = messages.map((m) => ({
        role: m.role,
        content: m.content,
      }));

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
          if (event.type === "stage") {
            setStage(event.stage);
          } else if (event.type === "token") {
            text += event.token;
            setPartial(text);
          } else if (event.type === "answer") {
            answer = event.answer;
            text = event.answer.text;
            onStats?.(event.stats.total_tokens, event.stats.generate_seconds);
          } else if (event.type === "error") {
            failure = event.message;
          }
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
    <section className="flex min-h-0 flex-col gap-3">
      <div className="flex items-baseline justify-between">
        <h2 className="text-[11px] font-semibold uppercase tracking-[0.14em] text-ink-tertiary">
          Ask about this document
        </h2>
        {!policiesLoaded && (
          <button
            type="button"
            onClick={async () => {
              try {
                await loadPolicies();
                setPoliciesLoaded(true);
              } catch {
                /* the button simply stays available */
              }
            }}
            className="flex items-center gap-1 text-[10px] text-ink-tertiary transition-colors hover:text-brand-500"
          >
            <BookOpen className="size-3" />
            Load policies
          </button>
        )}
      </div>

      <div className="flex min-h-0 flex-1 flex-col gap-3 overflow-y-auto pr-1">
        {messages.length === 0 && !streaming && (
          <p className="text-xs leading-relaxed text-ink-tertiary">
            {document
              ? "Ask anything about this document. Every answer cites the exact page it came from."
              : "Upload a document to start asking questions."}
          </p>
        )}

        <AnimatePresence initial={false}>
          {messages.map((message) =>
            message.role === "user" ? (
              <motion.div
                key={message.id}
                initial={{ opacity: 0, y: 8, scale: 0.98 }}
                animate={{ opacity: 1, y: 0, scale: 1 }}
                transition={{ duration: 0.2, ease: [0.16, 1, 0.3, 1] }}
                className="ml-auto max-w-[88%] rounded-2xl rounded-br-sm bg-surface-hover px-3.5 py-2.5 text-xs leading-relaxed text-ink-primary"
              >
                {message.content}
              </motion.div>
            ) : (
              <motion.div
                key={message.id}
                initial={{ opacity: 0, y: 8 }}
                animate={{ opacity: 1, y: 0 }}
                transition={{ duration: 0.24, ease: [0.16, 1, 0.3, 1] }}
                className="pb-1"
              >
                {message.error ? (
                  <div className="flex items-start gap-2 rounded-lg border-l-2 border-state-error bg-state-error/10 px-3 py-2.5">
                    <AlertTriangle className="mt-0.5 size-3.5 shrink-0 text-state-error" />
                    <p className="text-[11px] leading-relaxed text-state-error">
                      {message.error}
                    </p>
                  </div>
                ) : (
                  <>
                    <p className="whitespace-pre-wrap text-[13px] leading-relaxed text-ink-primary">
                      {renderWithCitations(message.content, message.answer)}
                    </p>
                    {message.answer && <WarningStrips answer={message.answer} />}
                    {message.answer && (
                      <p className="tabular mt-2 text-[10px] text-ink-tertiary">
                        {message.answer.latency_seconds.toFixed(2)}s ·{" "}
                        {message.answer.prompt_tokens + message.answer.completion_tokens}{" "}
                        tokens · $0.00
                      </p>
                    )}
                  </>
                )}
              </motion.div>
            )
          )}
        </AnimatePresence>

        {stage && (
          <motion.p
            key={stage}
            initial={{ opacity: 0 }}
            animate={{ opacity: 1 }}
            className="tabular text-[11px] text-ink-tertiary"
          >
            {stage}
          </motion.p>
        )}

        {partial && (
          <p className="whitespace-pre-wrap text-[13px] leading-relaxed text-ink-primary">
            {partial}
            <motion.span
              animate={{ opacity: [1, 0.2, 1] }}
              transition={{ duration: 1, repeat: Infinity }}
              className="ml-0.5 inline-block h-3.5 w-[2px] translate-y-0.5 bg-brand-500"
            />
          </p>
        )}

        <div ref={bottomRef} />
      </div>

      {/* Chips come from the extracted record, not a hardcoded list — that is what makes
          them useful rather than decorative (design.md §5.3). */}
      {chips.length > 0 && messages.length === 0 && (
        <div className="flex flex-wrap gap-1.5">
          {chips.slice(0, 5).map((chip, index) => (
            <motion.button
              key={chip}
              type="button"
              disabled={streaming}
              initial={{ opacity: 0, scale: 0.94 }}
              animate={{ opacity: 1, scale: 1 }}
              transition={{ delay: 0.05 * index }}
              whileHover={{ y: -1 }}
              whileTap={{ scale: 0.97 }}
              onClick={() => void ask(chip)}
              className="rounded-full border border-edge-subtle bg-surface-raised px-3 py-1.5 text-[11px] text-ink-secondary transition-colors hover:border-brand-500/60 hover:text-ink-primary disabled:opacity-40"
            >
              {chip}
            </motion.button>
          ))}
        </div>
      )}

      <form
        onSubmit={(event) => {
          event.preventDefault();
          void ask(draft);
        }}
        className="flex items-end gap-2 rounded-2xl border border-edge-subtle bg-surface-raised p-2"
      >
        <textarea
          value={draft}
          onChange={(event) => setDraft(event.target.value)}
          onKeyDown={(event) => {
            if (event.key === "Enter" && !event.shiftKey) {
              event.preventDefault();
              void ask(draft);
            }
          }}
          rows={1}
          disabled={!document || streaming}
          placeholder={
            document ? "Ask why a charge was deducted…" : "Upload a document first"
          }
          className="max-h-28 min-h-[34px] flex-1 resize-none bg-transparent px-2 py-1.5 text-xs text-ink-primary placeholder:text-ink-tertiary focus:outline-none disabled:cursor-not-allowed"
        />
        {streaming ? (
          <motion.button
            type="button"
            whileTap={{ scale: 0.94 }}
            onClick={() => abortRef.current?.abort()}
            title="Stop generating"
            className="grid size-8 shrink-0 place-items-center rounded-full bg-surface-hover text-ink-secondary transition-colors hover:text-state-error"
          >
            <Square className="size-3.5 fill-current" />
          </motion.button>
        ) : (
          <motion.button
            type="submit"
            whileTap={{ scale: 0.94 }}
            disabled={!document || !draft.trim()}
            className="grid size-8 shrink-0 place-items-center rounded-full bg-brand-600 text-white transition-opacity disabled:opacity-30"
          >
            <ArrowUp className="size-4" />
          </motion.button>
        )}
      </form>
    </section>
  );
}
