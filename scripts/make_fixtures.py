"""Generate synthetic financial-document fixtures (phases.md Phase 2).

**No real financial documents ever enter this repository** (rules.md Rule 4). Every
fixture is generated here from invented data, so the corpus is safe to commit and
reproducible on any machine.

    python scripts/make_fixtures.py

Fixtures produced in ``evals/fixtures/``:

  clean_invoice.pdf       Digital-text AWS-style invoice, ruled table, arithmetic balances
  unbalanced_invoice.pdf  Same shape, but line items DO NOT sum to the stated total
  multipage_statement.pdf Two-page credit-card statement
  scanned_receipt.png     Rasterized receipt — no text layer, forces the OCR path
  malformed.pdf           Truncated bytes; must raise ParsingError, not crash
"""

from __future__ import annotations

import sys
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import fitz  # PyMuPDF  # noqa: E402

from src.config import FIXTURES_DIR  # noqa: E402

PAGE_W, PAGE_H = 595.0, 842.0  # A4 points
MARGIN = 56.0
FONT = "helv"
FONT_BOLD = "hebo"

# Column x-offsets for the line-item table.
COL_DESC = MARGIN + 8
COL_QTY = MARGIN + 268
COL_UNIT = MARGIN + 336
COL_AMT = MARGIN + 470  # right edge for right-aligned amounts

AWS_ITEMS: list[tuple[str, str, str, str]] = [
    ("EC2 t3.medium instance-hours", "730", "0.0420", "30.66"),
    ("NAT Gateway data processing (GB)", "1", "412.9000", "412.90"),
    ("S3 Standard storage", "1", "18.4400", "18.44"),
]


def _text(page: fitz.Page, x: float, y: float, s: str, size: float = 9.5, bold: bool = False) -> None:
    page.insert_text((x, y), s, fontname=FONT_BOLD if bold else FONT, fontsize=size)


def _right(page: fitz.Page, x_right: float, y: float, s: str, size: float = 9.5, bold: bool = False) -> None:
    font = fitz.Font(fontname=FONT_BOLD if bold else FONT)
    page.insert_text(
        (x_right - font.text_length(s, fontsize=size), y),
        s,
        fontname=FONT_BOLD if bold else FONT,
        fontsize=size,
    )


def _line(page: fitz.Page, y: float, x0: float = MARGIN, x1: float = PAGE_W - MARGIN, width: float = 0.6) -> None:
    page.draw_line(fitz.Point(x0, y), fitz.Point(x1, y), width=width, color=(0.35, 0.35, 0.35))


def _header(page: fitz.Page, vendor: str, address: str, doc_title: str) -> float:
    _text(page, MARGIN, 70, vendor, size=17, bold=True)
    _text(page, MARGIN, 86, address, size=8.5)
    _right(page, PAGE_W - MARGIN, 70, doc_title, size=15, bold=True)
    return 110.0


def _meta_block(page: fitz.Page, y: float, rows: list[tuple[str, str]]) -> float:
    for label, value in rows:
        _text(page, MARGIN, y, label, size=9, bold=True)
        _text(page, MARGIN + 132, y, value, size=9)
        y += 14
    return y + 10


def _item_table(page: fitz.Page, y: float, items: list[tuple[str, str, str, str]]) -> float:
    _line(page, y - 11, width=0.9)
    _text(page, COL_DESC, y, "Description", size=9, bold=True)
    _right(page, COL_QTY + 34, y, "Qty", size=9, bold=True)
    _right(page, COL_UNIT + 90, y, "Unit Price", size=9, bold=True)
    _right(page, COL_AMT, y, "Amount", size=9, bold=True)
    y += 6
    _line(page, y, width=0.9)
    y += 16

    for description, qty, unit, amount in items:
        _text(page, COL_DESC, y, description)
        _right(page, COL_QTY + 34, y, qty)
        _right(page, COL_UNIT + 90, y, unit)
        _right(page, COL_AMT, y, amount)
        y += 6
        _line(page, y, width=0.3)
        y += 15
    return y


def _totals(page: fitz.Page, y: float, subtotal: str, tax_label: str, tax: str, total: str) -> None:
    y += 6
    _right(page, COL_UNIT + 90, y, "Subtotal", bold=True)
    _right(page, COL_AMT, y, subtotal)
    y += 15
    _right(page, COL_UNIT + 90, y, tax_label, bold=True)
    _right(page, COL_AMT, y, tax)
    y += 8
    _line(page, y, x0=COL_UNIT, width=0.9)
    y += 16
    _right(page, COL_UNIT + 90, y, "TOTAL DUE", size=11, bold=True)
    _right(page, COL_AMT, y, total, size=11, bold=True)


def build_clean_invoice(path: Path) -> None:
    """Digital-text invoice whose arithmetic balances exactly. The happy-path fixture."""
    doc = fitz.open()
    page = doc.new_page(width=PAGE_W, height=PAGE_H)

    y = _header(page, "Amazon Web Services, Inc.", "410 Terry Avenue North, Seattle, WA 98109", "INVOICE")
    y = _meta_block(
        page,
        y,
        [
            ("Invoice Number", "INV-7741820"),
            ("Invoice Date", "2026-07-01"),
            ("Billing Period", "2026-06-01 to 2026-06-30"),
            ("Payment Due", "2026-07-31"),
            ("Account ID", "8842-1190-3374"),
            ("Currency", "USD"),
        ],
    )
    y = _item_table(page, y + 10, AWS_ITEMS)
    _totals(page, y, "462.00", "Sales Tax (8.5%)", "39.27", "501.27")
    _text(page, MARGIN, PAGE_H - 70, "Charges above the free tier are billed monthly in arrears.", size=8)
    doc.save(path)
    doc.close()


def build_unbalanced_invoice(path: Path) -> None:
    """Line items do NOT sum to the stated total — must trip FR-2.4 validation.

    Line items sum to 462.00 and tax is 39.27, so the honest total is 501.27. This
    document states 528.40 instead. ``FinancialRecord.validate_arithmetic`` must flag it
    and must NOT silently adjust any figure (decision D-12).
    """
    doc = fitz.open()
    page = doc.new_page(width=PAGE_W, height=PAGE_H)

    y = _header(page, "Northwind Logistics Ltd.", "22 Harbour Road, Bristol BS1 5UH", "INVOICE")
    y = _meta_block(
        page,
        y,
        [
            ("Invoice Number", "NW-2026-0442"),
            ("Invoice Date", "2026-06-18"),
            ("Payment Due", "2026-07-18"),
            ("Currency", "USD"),
        ],
    )
    y = _item_table(page, y + 10, AWS_ITEMS)
    _totals(page, y, "462.00", "Sales Tax (8.5%)", "39.27", "528.40")  # deliberately wrong
    doc.save(path)
    doc.close()


def build_multipage_statement(path: Path) -> None:
    """Two-page card statement — exercises page_count and per-page provenance."""
    doc = fitz.open()

    page = doc.new_page(width=PAGE_W, height=PAGE_H)
    y = _header(page, "Meridian Bank", "PO Box 4410, Wilmington, DE 19899", "CARD STATEMENT")
    y = _meta_block(
        page,
        y,
        [
            ("Account", "**** **** **** 4417"),
            ("Statement Date", "2026-07-05"),
            ("Billing Period", "2026-06-05 to 2026-07-04"),
            ("Currency", "USD"),
        ],
    )
    page_one = [
        ("AMAZON WEB SERVICES  AWS.AMAZON.CO", "1", "501.27", "501.27"),
        ("UNITED AIRLINES  SAN FRANCISCO CA", "1", "684.20", "684.20"),
        ("HILTON GARDEN INN  AUSTIN TX", "3", "212.00", "636.00"),
    ]
    _item_table(page, y + 10, page_one)
    _text(page, MARGIN, PAGE_H - 60, "Continued on page 2", size=8.5, bold=True)

    page2 = doc.new_page(width=PAGE_W, height=PAGE_H)
    _text(page2, MARGIN, 70, "Meridian Bank — Statement continued", size=13, bold=True)
    page_two = [
        ("GITHUB INC  HTTPSGITHUB.CO", "1", "21.00", "21.00"),
        ("UBER TRIP  HELP.UBER.COM", "4", "27.35", "109.40"),
    ]
    y2 = _item_table(page2, 120, page_two)
    _totals(page2, y2, "1951.87", "Interest Charged", "0.00", "1951.87")
    doc.save(path)
    doc.close()


def build_scanned_receipt(path: Path) -> None:
    """A receipt rasterized to PNG — no text layer at all, so OCR is the only route.

    This is the fixture that proves the OCR fallback fires (decision D-15) and, just as
    importantly, that it does NOT fire on the digital fixtures above.
    """
    doc = fitz.open()
    page = doc.new_page(width=300, height=430)

    lines = [
        ("CITY GROCERS", 13, True),
        ("1180 Mission St, San Francisco", 7.5, False),
        ("Receipt #  4471-02", 8, False),
        ("Date  2026-07-12  14:32", 8, False),
        ("", 8, False),
        ("Coffee beans 1kg          18.50", 8.5, False),
        ("Oat milk x3                9.75", 8.5, False),
        ("Sandwich                   7.25", 8.5, False),
        ("", 8, False),
        ("Subtotal                  35.50", 8.5, False),
        ("Sales Tax 8.5%             3.02", 8.5, False),
        ("TOTAL                     38.52", 10, True),
        ("", 8, False),
        ("VISA ****4417   APPROVED", 8, False),
    ]
    y = 40.0
    for text, size, bold in lines:
        if text:
            page.insert_text((24, y), text, fontname=FONT_BOLD if bold else FONT, fontsize=size)
        y += size + 6

    # Rasterize and discard the text layer entirely.
    pixmap = page.get_pixmap(dpi=170)
    pixmap.save(path)
    doc.close()


def build_malformed(path: Path) -> None:
    """A truncated PDF. Must raise ParsingError with an actionable message, not crash."""
    path.write_bytes(b"%PDF-1.7\n1 0 obj\n<< /Type /Catalog >>\nendobj\ntrailer\n<<TRUNCATED")


# ── Policy corpus (phases.md Phase 3) ────────────────────────────────────────
#
# The supporting context that turns "what does this say" into "why was I charged this".
# Written as markdown because policies are prose, not layout — they are ingested as text
# rather than pushed through Docling.

TRAVEL_POLICY = """# Corporate Travel & Expense Policy

Effective 2026-01-01. Applies to all employees and contractors.

## 1. Air Travel
Economy class is required for all flights under 6 hours. Premium economy is permitted for
flights of 6 hours or more with prior written approval from a line manager. Business class
requires VP approval and is not reimbursable without it.

## 2. Accommodation
Hotel spend is capped at **USD 200 per night** excluding taxes in standard-rate cities, and
USD 275 per night in designated high-cost cities (San Francisco, New York, London, Tokyo,
Zurich, Singapore). Nightly rates above the cap are reimbursed only up to the cap; the
remainder is the employee's responsibility.

## 3. Ground Transport
Ride-hailing and taxi fares are reimbursable for airport transfers and client travel. Daily
ground transport above USD 75 requires an itemised receipt and a business justification.

## 4. Meals
Meals are reimbursed to a daily limit of USD 75 domestic and USD 100 international. Alcohol
is not reimbursable except at approved client-entertainment events.

## 5. Receipts and Deadlines
Any single expense of USD 25 or more requires an itemised receipt. Claims must be submitted
within 30 days of the expense date. Claims submitted after 60 days will not be reimbursed.

## 6. Non-Reimbursable
Personal entertainment, in-flight wifi above USD 30 per flight, hotel minibar, traffic
fines, flight-change fees arising from personal preference, and travel insurance purchased
separately from the corporate policy.
"""

CLOUD_BILLING_POLICY = """# Cloud Infrastructure Billing & Overage Terms

Applies to the company's Amazon Web Services accounts. Last revised 2026-04-15.

## 1. Billing Cycle
Usage is metered continuously and invoiced monthly in arrears. The billing period runs from
00:00 UTC on the first day of the month to 23:59 UTC on the last day. Charges appear on the
invoice issued in the first week of the following month.

## 2. Free Tier
New accounts receive 12 months of limited free-tier usage. Free-tier allowances do not roll
over between months. Usage beyond the allowance is billed at standard on-demand rates with
no warning, which is the most common cause of an unexpected first invoice.

## 3. NAT Gateway Charges
NAT Gateways are billed on two separate dimensions: an hourly charge for each gateway that
exists, and a **data processing charge per GB** passed through it. The data processing
charge applies to all traffic, including traffic to and from services inside AWS. This is
the single most frequent source of unexplained cost increases, because moving data between
private subnets and S3 or ECR without a VPC endpoint routes through the NAT Gateway and is
billed per GB. **Internal budget alert threshold for NAT Gateway data processing is USD 200
per month.** Spend above this must be reviewed by the infrastructure lead.

## 4. EC2 Instance Hours
On-demand instances are billed per second with a 60-second minimum. Instances that are
stopped are not billed for compute, but attached EBS volumes continue to be billed.

## 5. S3 Storage
Standard storage is billed per GB-month, prorated. Requests, data transfer out, and
lifecycle transitions are billed separately from storage.

## 6. Tax
Sales tax is applied at the rate applicable to the billing address on file. For US accounts
this is currently 8.5%. Tax is calculated on the subtotal after any credits are applied.

## 7. Disputes
Billing disputes must be raised within 60 days of the invoice date. Include the invoice
number and the specific line item in question.
"""


def build_prior_invoice(path: Path) -> None:
    """The previous month's AWS invoice, so 'compare to last month' has something to find.

    NAT Gateway is 41.20 here versus 412.90 in the current invoice — a 10x jump that is the
    intended discovery for the comparison flow.
    """
    doc = fitz.open()
    page = doc.new_page(width=PAGE_W, height=PAGE_H)

    y = _header(page, "Amazon Web Services, Inc.", "410 Terry Avenue North, Seattle, WA 98109", "INVOICE")
    y = _meta_block(
        page,
        y,
        [
            ("Invoice Number", "INV-7698233"),
            ("Invoice Date", "2026-06-01"),
            ("Billing Period", "2026-05-01 to 2026-05-31"),
            ("Payment Due", "2026-06-30"),
            ("Account ID", "8842-1190-3374"),
            ("Currency", "USD"),
        ],
    )
    prior_items = [
        ("EC2 t3.medium instance-hours", "744", "0.0420", "31.25"),
        ("NAT Gateway data processing (GB)", "1", "41.2000", "41.20"),
        ("S3 Standard storage", "1", "17.90", "17.90"),
    ]
    y = _item_table(page, y + 10, prior_items)
    _totals(page, y, "90.35", "Sales Tax (8.5%)", "7.68", "98.03")
    doc.save(path)
    doc.close()


def _assert_arithmetic() -> None:
    """Guard the fixtures themselves — a fixture with wrong maths teaches wrong lessons."""
    items = sum(Decimal(amount) for _, _, _, amount in AWS_ITEMS)
    assert items == Decimal("462.00"), f"AWS_ITEMS sum to {items}, expected 462.00"
    tax = (items * Decimal("0.085")).quantize(Decimal("0.01"))
    assert tax == Decimal("39.27"), f"Tax computes to {tax}, expected 39.27"
    assert items + tax == Decimal("501.27"), "clean_invoice total is inconsistent"

    prior = Decimal("31.25") + Decimal("41.20") + Decimal("17.90")
    assert prior == Decimal("90.35"), f"prior invoice items sum to {prior}, expected 90.35"
    prior_tax = (prior * Decimal("0.085")).quantize(Decimal("0.01"))
    assert prior_tax == Decimal("7.68"), f"prior tax computes to {prior_tax}, expected 7.68"
    assert prior + prior_tax == Decimal("98.03"), "prior_invoice total is inconsistent"


def main() -> int:
    _assert_arithmetic()
    FIXTURES_DIR.mkdir(parents=True, exist_ok=True)
    policy_dir = FIXTURES_DIR / "policies"
    policy_dir.mkdir(parents=True, exist_ok=True)

    builders = [
        ("clean_invoice.pdf", build_clean_invoice),
        ("unbalanced_invoice.pdf", build_unbalanced_invoice),
        ("multipage_statement.pdf", build_multipage_statement),
        ("scanned_receipt.png", build_scanned_receipt),
        ("malformed.pdf", build_malformed),
        ("prior_invoice.pdf", build_prior_invoice),
    ]
    for filename, builder in builders:
        target = FIXTURES_DIR / filename
        builder(target)
        print(f"  wrote {filename:<28} {target.stat().st_size:>8,} bytes")

    policies = [
        ("travel_policy.md", TRAVEL_POLICY),
        ("cloud_billing_policy.md", CLOUD_BILLING_POLICY),
    ]
    for filename, content in policies:
        target = policy_dir / filename
        target.write_text(content, encoding="utf-8")
        print(f"  wrote policies/{filename:<18} {target.stat().st_size:>8,} bytes")

    print(f"\n{len(builders) + len(policies)} synthetic fixtures written to {FIXTURES_DIR}")
    print("All data is invented. No real financial documents are in this repository (Rule 4).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
