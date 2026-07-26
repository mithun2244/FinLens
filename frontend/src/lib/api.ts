/**
 * Typed client for the FinLens FastAPI backend.
 *
 * Every monetary value is a `string`, never a `number`. The backend sends Decimal values
 * as strings on purpose: `Decimal("412.90")` would become the float `412.9` in JavaScript,
 * and float arithmetic on currency is what decision D-6 exists to prevent. The UI formats
 * these strings; it never does arithmetic on them.
 */

export const API_BASE =
  process.env.NEXT_PUBLIC_API_BASE ?? "http://localhost:8000";

export type ConfidenceBand = "high" | "medium" | "low";
export type ValidationState = "validated" | "mismatch" | "incomplete";

export interface LineItem {
  description: string;
  quantity: string | null;
  unit_price: string | null;
  amount: string;
  source_page: number;
  confidence: number;
  confidence_band: ConfidenceBand;
}

export interface TaxLine {
  label: string;
  rate: string | null;
  amount: string;
}

export interface PageInfo {
  page_number: number;
  width_points: number;
  height_points: number;
  has_image: boolean;
}

export interface FinancialDocument {
  document_id: string;
  filename: string;
  vendor_name: string;
  vendor_address: string | null;
  document_type: string;
  invoice_number: string | null;
  billing_date: string | null;
  billing_period_start: string | null;
  billing_period_end: string | null;
  currency: string;
  line_items: LineItem[];
  subtotal: string | null;
  tax_lines: TaxLine[];
  total_amount: string | null;
  computed_total: string;
  validation_state: ValidationState;
  is_validated: boolean;
  extraction_warnings: string[];
  page_count: number;
  pages: PageInfo[];
  used_ocr: boolean;
  parse_seconds: number;
  suggested_prompts: string[];
}

/**
 * A region on a page, normalized 0-1 with a top-left origin.
 *
 * The backend converts Docling's bottom-left absolute points once, at the boundary, so
 * the client can position an overlay as CSS percentages without knowing page dimensions
 * or dealing with coordinate systems.
 */
export interface BoundingBox {
  left: number;
  top: number;
  right: number;
  bottom: number;
}

export interface Citation {
  document_id: string;
  filename: string;
  page: number;
  label: string;
  snippet: string;
  score: number;
  chunk_type: string;
  /** Null when Docling gave no provenance; the UI falls back to page-level highlight. */
  bbox: BoundingBox | null;
}

/** URL of a rendered page image. Immutable per document, so it caches indefinitely. */
export function pageImageUrl(documentId: string, page: number): string {
  return `${API_BASE}/api/documents/${documentId}/pages/${page}`;
}

export interface ContradictingFigure {
  claimed: string;
  field: string | null;
  expected: string | null;
}

export interface AnswerPayload {
  text: string;
  refused: boolean;
  is_grounded: boolean;
  is_trustworthy: boolean;
  model: string;
  latency_seconds: number;
  prompt_tokens: number;
  completion_tokens: number;
  citations: Citation[];
  dropped_citations: string[];
  unsupported_figures: string[];
  contradicting_figures: ContradictingFigure[];
}

export interface RunStatsPayload {
  retrieve_seconds: number;
  generate_seconds: number;
  chunks_retrieved: number;
  total_tokens: number;
  tokens_estimated: boolean;
}

export type StreamEvent =
  | { type: "stage"; stage: string }
  | { type: "token"; token: string }
  | { type: "error"; message: string; retry_after_seconds: number | null }
  | { type: "answer"; answer: AnswerPayload; stats: RunStatsPayload };

export interface ChatTurn {
  role: "user" | "assistant";
  content: string;
}

export interface HealthPayload {
  status: string;
  llm_configured: boolean;
  reasoning_model: string;
  documents_loaded: number;
  collections: Record<string, number>;
}

export interface SampleDocument {
  label: string;
  filename: string;
}

/** Surface the backend's own message, which is written to be shown to a user. */
async function unwrap<T>(response: Response): Promise<T> {
  if (!response.ok) {
    let detail = `Request failed (${response.status})`;
    try {
      const body = await response.json();
      if (typeof body?.detail === "string") detail = body.detail;
    } catch {
      /* non-JSON error body; keep the status message */
    }
    throw new Error(detail);
  }
  return (await response.json()) as T;
}

export async function fetchHealth(): Promise<HealthPayload> {
  return unwrap<HealthPayload>(await fetch(`${API_BASE}/api/health`));
}

export async function fetchSamples(): Promise<SampleDocument[]> {
  return unwrap<SampleDocument[]>(await fetch(`${API_BASE}/api/samples`));
}

export async function uploadDocument(file: File): Promise<FinancialDocument> {
  const body = new FormData();
  body.append("file", file);
  return unwrap<FinancialDocument>(
    await fetch(`${API_BASE}/api/upload`, { method: "POST", body })
  );
}

export async function loadSample(filename: string): Promise<FinancialDocument> {
  return unwrap<FinancialDocument>(
    await fetch(`${API_BASE}/api/samples/${encodeURIComponent(filename)}`, {
      method: "POST",
    })
  );
}

export async function loadPolicies(): Promise<{ indexed: number; files: number }> {
  return unwrap(await fetch(`${API_BASE}/api/policies`, { method: "POST" }));
}

/**
 * Stream a grounded answer, yielding each NDJSON line as a typed event.
 *
 * The response is newline-delimited JSON rather than SSE, so a partial line at the end of
 * a chunk must be carried into the next read — splitting on "\n" and parsing every piece
 * would throw on the tail of a split object.
 */
export async function* streamChat(
  question: string,
  documentId: string | null,
  history: ChatTurn[],
  signal?: AbortSignal
): AsyncGenerator<StreamEvent> {
  const response = await fetch(`${API_BASE}/api/chat`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ question, document_id: documentId, history }),
    signal,
  });

  if (!response.ok || !response.body) {
    let detail = `Chat failed (${response.status})`;
    try {
      const body = await response.json();
      if (typeof body?.detail === "string") detail = body.detail;
    } catch {
      /* keep the status message */
    }
    yield { type: "error", message: detail, retry_after_seconds: null };
    return;
  }

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";

  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });

    const lines = buffer.split("\n");
    buffer = lines.pop() ?? ""; // keep the incomplete tail
    for (const line of lines) {
      if (!line.trim()) continue;
      try {
        yield JSON.parse(line) as StreamEvent;
      } catch {
        /* ignore a malformed line rather than killing the stream */
      }
    }
  }

  if (buffer.trim()) {
    try {
      yield JSON.parse(buffer) as StreamEvent;
    } catch {
      /* trailing partial line */
    }
  }
}
