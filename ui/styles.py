"""Design tokens and stylesheet (design.md §3).

Every colour, size, and duration is defined once as a CSS custom property. Component code
references the tokens and never a literal hex value — the "zero hardcoded hex" rule in
design.md §8.

Semantic colours carry meaning here rather than decoration: ``--state-validated`` means
the arithmetic reconciled, ``--state-warning`` means low confidence or a mismatch. Every
one is paired with an icon and a text label in the components, because a table full of
numbers must not encode state by colour alone.
"""

from __future__ import annotations

__all__ = ["STYLESHEET"]

STYLESHEET = """
<style>
:root {
  --surface-base:    #FAFAFA;
  --surface-raised:  #FFFFFF;
  --surface-sunken:  #F1F2F4;
  --text-primary:    #16181D;
  --text-secondary:  #5A6070;
  --text-tertiary:   #8A90A0;
  --border-subtle:   #E6E8EC;
  --border-default:  #D3D7DE;
  --brand-600:       #0F766E;
  --brand-500:       #14867C;
  --brand-100:       #D6F0EC;

  --state-validated: #16794C;
  --state-warning:   #B45309;
  --state-error:     #B42318;
  --state-info:      #1D4ED8;
  --state-validated-bg: rgba(22,121,76,.10);
  --state-warning-bg:   rgba(180,83,9,.10);
  --state-error-bg:     rgba(180,35,24,.10);
  --state-info-bg:      rgba(29,78,216,.10);

  --amount-debit:  #B42318;
  --amount-credit: #16794C;

  --citation-highlight: rgba(29,78,216,.16);
  --citation-border:    rgba(29,78,216,.55);

  --radius-sm: 6px;  --radius-md: 10px;  --radius-lg: 14px;  --radius-full: 999px;
  --shadow-sm: 0 1px 2px rgba(16,18,29,.05), 0 1px 3px rgba(16,18,29,.04);
  --shadow-md: 0 4px 12px rgba(16,18,29,.07), 0 1px 3px rgba(16,18,29,.05);

  --font-sans: "Inter", -apple-system, "Segoe UI", system-ui, sans-serif;
  --font-mono: "JetBrains Mono", "SF Mono", "Cascadia Code", ui-monospace, monospace;

  --ease-out: cubic-bezier(.16,1,.3,1);
  --dur-fast: 120ms;  --dur-base: 200ms;
}

@media (prefers-color-scheme: dark) {
  :root {
    --surface-base:   #0F1115;
    --surface-raised: #171A21;
    --surface-sunken: #0B0D11;
    --text-primary:   #ECEEF3;
    --text-secondary: #A0A7B6;
    --text-tertiary:  #6B7280;
    --border-subtle:  #232833;
    --border-default: #2E3543;
    --state-validated: #3DBF85;
    --state-warning:   #E3A008;
    --state-error:     #F0645B;
    --state-info:      #6699FF;
    --brand-500:       #2AA79A;
  }
}

.stApp { background: var(--surface-base); }
html, body, [class*="css"] { font-family: var(--font-sans); }
#MainMenu, footer, header[data-testid="stHeader"] { visibility: hidden; height: 0; }
.block-container { padding: .6rem 1.4rem 2rem !important; max-width: 100% !important; }

/* ── App header ───────────────────────────────────────────────────────────── */
.fl-header {
  display: flex; align-items: center; justify-content: space-between;
  padding: 10px 16px; margin-bottom: 8px;
  background: var(--surface-raised); border: 1px solid var(--border-subtle);
  border-radius: var(--radius-lg); box-shadow: var(--shadow-sm);
}
.fl-brand { display: flex; align-items: baseline; gap: 10px; }
.fl-brand-mark { font-size: 17px; font-weight: 600; color: var(--text-primary); letter-spacing: -.01em; }
.fl-brand-sub { font-size: 12px; color: var(--text-tertiary); }

/* ── Observability bar (design.md §5.4) ───────────────────────────────────── */
.fl-obs {
  display: flex; flex-wrap: wrap; align-items: center; gap: 8px 18px;
  padding: 8px 16px; margin-bottom: 12px;
  background: var(--surface-sunken); border: 1px solid var(--border-subtle);
  border-radius: var(--radius-md);
  font-family: var(--font-mono); font-size: 12px; color: var(--text-secondary);
}
.fl-obs .sep { color: var(--border-default); }
.fl-obs strong { color: var(--text-primary); font-weight: 500; }
.fl-obs .ok   { color: var(--state-validated); }
.fl-obs .warn { color: var(--state-warning); }
.fl-obs .cost { color: var(--state-validated); font-weight: 500; }

/* ── Panels ───────────────────────────────────────────────────────────────── */
.fl-panel {
  background: var(--surface-raised); border: 1px solid var(--border-subtle);
  border-radius: var(--radius-lg); padding: 14px 16px; margin-bottom: 12px;
  box-shadow: var(--shadow-sm);
}
.fl-panel-title {
  font-size: 12px; font-weight: 600; letter-spacing: .06em; text-transform: uppercase;
  color: var(--text-tertiary); margin-bottom: 10px;
}
.fl-vendor { font-size: 20px; font-weight: 600; color: var(--text-primary); line-height: 1.25; }
.fl-meta { font-size: 12.5px; color: var(--text-secondary); margin-top: 3px; }
.fl-badge {
  display: inline-block; padding: 2px 8px; border-radius: var(--radius-full);
  background: var(--brand-100); color: var(--brand-600);
  font-size: 11px; font-weight: 600; text-transform: uppercase; letter-spacing: .04em;
}

/* ── Empty state ──────────────────────────────────────────────────────────── */
.fl-empty {
  border: 2px dashed var(--border-default); border-radius: var(--radius-lg);
  padding: 44px 24px; text-align: center; background: var(--surface-raised);
}
.fl-empty-icon { font-size: 34px; opacity: .5; }
.fl-empty-title { font-size: 16px; font-weight: 600; color: var(--text-primary); margin-top: 10px; }
.fl-empty-sub { font-size: 12.5px; color: var(--text-tertiary); margin-top: 6px; line-height: 1.6; }

/* ── Validation banner (three states, design.md §5.2) ─────────────────────── */
.fl-banner {
  display: flex; gap: 9px; align-items: flex-start;
  padding: 10px 12px; border-radius: var(--radius-md);
  border-left: 3px solid var(--border-default); font-size: 12.5px; line-height: 1.5;
  margin-bottom: 10px;
}
.fl-banner.validated { background: var(--state-validated-bg); border-left-color: var(--state-validated); color: var(--state-validated); }
.fl-banner.mismatch  { background: var(--state-warning-bg);   border-left-color: var(--state-warning);   color: var(--state-warning); }
.fl-banner.incomplete{ background: var(--state-warning-bg);   border-left-color: var(--state-warning);   color: var(--state-warning); }
.fl-banner.error     { background: var(--state-error-bg);     border-left-color: var(--state-error);     color: var(--state-error); }
.fl-banner .icon { font-weight: 700; }

/* ── Line-item table ──────────────────────────────────────────────────────── */
.fl-table { width: 100%; border-collapse: collapse; font-size: 12.5px; }
.fl-table th {
  text-align: left; font-weight: 600; font-size: 11px; letter-spacing: .04em;
  text-transform: uppercase; color: var(--text-tertiary);
  padding: 6px 8px; border-bottom: 1px solid var(--border-default); white-space: nowrap;
}
.fl-table td { padding: 7px 8px; border-bottom: 1px solid var(--border-subtle); color: var(--text-primary); }
.fl-table tr:hover td { background: var(--surface-sunken); }
.fl-table .num {
  font-family: var(--font-mono); font-variant-numeric: tabular-nums;
  text-align: right; white-space: nowrap;
}
.fl-table .desc { max-width: 260px; }
.fl-scroll { overflow-x: auto; }

/* Confidence dot: colour AND fill, never colour alone. */
.fl-dot { display: inline-block; width: 8px; height: 8px; border-radius: 50%; }
.fl-dot.high   { background: var(--state-validated); }
.fl-dot.medium { background: transparent; border: 1.5px solid var(--state-warning); }
.fl-dot.low    { background: transparent; border: 1.5px solid var(--state-error); }

/* ── Totals ───────────────────────────────────────────────────────────────── */
.fl-totals {
  background: var(--surface-sunken); border-radius: var(--radius-md);
  padding: 12px 14px; margin-top: 10px;
}
.fl-total-row {
  display: flex; justify-content: space-between; align-items: baseline;
  font-size: 12.5px; color: var(--text-secondary); padding: 3px 0;
}
.fl-total-row .v { font-family: var(--font-mono); font-variant-numeric: tabular-nums; color: var(--text-primary); }
.fl-total-row.grand {
  border-top: 1px solid var(--border-default); margin-top: 7px; padding-top: 9px;
  font-size: 14px; font-weight: 600; color: var(--text-primary);
}
.fl-total-row.grand .v { font-size: 16px; font-weight: 600; }

/* ── Document previewer + citation overlay (design.md §5.1) ───────────────── */
.fl-page-wrap {
  position: relative; display: block; width: 100%;
  background: #FFFFFF; border: 1px solid var(--border-default);
  border-radius: var(--radius-md); overflow: hidden;
}
.fl-page-wrap img { display: block; width: 100%; height: auto; }
.fl-highlight {
  position: absolute; background: var(--citation-highlight);
  border: 1.5px solid var(--citation-border); border-radius: 3px;
  animation: fl-pulse 1.1s var(--ease-out) 2;
}
@keyframes fl-pulse {
  0%, 100% { opacity: 1; }
  50%      { opacity: .35; }
}

/* ── Chat ─────────────────────────────────────────────────────────────────── */
.fl-msg-user {
  background: var(--surface-sunken); border-radius: var(--radius-md);
  padding: 9px 12px; margin: 6px 0 6px auto; max-width: 88%;
  font-size: 13px; color: var(--text-primary);
}
.fl-msg-assistant {
  padding: 4px 2px 10px; font-size: 13.5px; line-height: 1.62; color: var(--text-primary);
}
.fl-cite {
  display: inline-block; padding: 1px 7px; margin: 0 2px;
  background: var(--state-info-bg); color: var(--state-info);
  border-radius: var(--radius-full);
  font-family: var(--font-mono); font-size: 10.5px; white-space: nowrap;
}
.fl-stage {
  font-size: 11.5px; color: var(--text-tertiary);
  font-family: var(--font-mono); padding: 3px 0;
}
.fl-warn-strip {
  background: var(--state-warning-bg); border-left: 3px solid var(--state-warning);
  color: var(--state-warning); font-size: 11.5px; line-height: 1.5;
  padding: 7px 10px; border-radius: var(--radius-sm); margin: 6px 0;
}

/* ── Streamlit control restyling ──────────────────────────────────────────── */
div[data-testid="stFileUploaderDropzone"] {
  background: var(--surface-raised); border: 2px dashed var(--border-default);
  border-radius: var(--radius-lg); transition: border-color var(--dur-fast) var(--ease-out),
                                              background var(--dur-fast) var(--ease-out);
}
div[data-testid="stFileUploaderDropzone"]:hover {
  border-color: var(--brand-500); background: var(--brand-100);
}
.stButton > button {
  border-radius: var(--radius-full); border: 1px solid var(--border-default);
  background: var(--surface-raised); color: var(--text-primary);
  font-size: 12px; padding: 3px 12px; transition: all var(--dur-fast) var(--ease-out);
}
.stButton > button:hover { border-color: var(--brand-500); color: var(--brand-600); }
.stButton > button:focus-visible { outline: 2px solid var(--brand-500); outline-offset: 2px; }

/* ── Responsive (design.md §4.2) ──────────────────────────────────────────────
   Streamlit lays columns out server-side with fixed ratios, so they stay three-abreast
   at every width unless CSS intervenes. Without this, the line-item table is squeezed
   until the Amount column — the one column that matters most — is clipped off the right
   edge at 1024px and gone entirely at 768px.

   Below 1200px the three surfaces stack full-width instead. This is a simpler
   degradation than the tabbed two-column variant sketched in design.md §4.2, but it
   satisfies the hard rule: every column stays readable and the body never scrolls
   sideways. */
@media (max-width: 1200px) {
  [data-testid="stHorizontalBlock"] { flex-wrap: wrap !important; }
  [data-testid="stColumn"] {
    flex: 1 1 100% !important;
    width: 100% !important;
    min-width: 0 !important;
  }
  [data-testid="stColumn"] + [data-testid="stColumn"] { margin-top: 18px; }
  .fl-page-wrap { max-width: 620px; }
}

/* Keep the description column readable while the numeric columns hold their width. */
@media (max-width: 1200px) {
  .fl-table .desc { max-width: none; }
}

@media (prefers-reduced-motion: reduce) {
  *, *::before, *::after { animation-duration: 1ms !important; transition-duration: 1ms !important; }
}
</style>
"""
