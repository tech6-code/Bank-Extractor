import re
import logging
from collections import defaultdict
import pdfplumber
from pathlib import Path

logger = logging.getLogger(__name__)

# ── Date Patterns ─────────────────────────────────────────────────────────────
# Full month names and common abbreviations
_MONTH = (
    r"(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?"
    r"|jul(?:y)?|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)"
)

DATE_PATTERNS = [
    # DD/MM/YYYY or MM/DD/YYYY  (4-digit year, /, -, . separator)
    re.compile(r"\b(\d{1,2}[/\-.]\d{1,2}[/\-.]\d{4})\b"),
    # YYYY/MM/DD or YYYY-MM-DD  (ISO-style)
    re.compile(r"\b(\d{4}[/\-.]\d{1,2}[/\-.]\d{1,2})\b"),
    # DD/MM/YY or MM/DD/YY      (2-digit year)
    re.compile(r"\b(\d{1,2}[/\-.]\d{1,2}[/\-.]\d{2})\b"),
    # DDMONYY / DMONYY / DDMONTHYYYY  e.g. "31DEC24", "2JAN25", "15JANUARY2025"
    re.compile(rf"\b(\d{{1,2}}{_MONTH}\d{{2,4}})\b", re.IGNORECASE),
    # D Mon YYYY / D Month YYYY  e.g. "1 Jan 2024", "15 January 2024"
    re.compile(rf"\b(\d{{1,2}}\s+{_MONTH}\s+\d{{4}})\b", re.IGNORECASE),
    # D-Mon-YYYY / D/Mon/YYYY    e.g. "4-Nov-2025", "4/Nov/2025"
    re.compile(rf"\b(\d{{1,2}}[/\-.]{_MONTH}[/\-.]\d{{4}})\b", re.IGNORECASE),
    # D-Mon-YY / D/Mon/YY        e.g. "01-MAY-25", "4/Nov/25"  (2-digit year)
    re.compile(rf"\b(\d{{1,2}}[/\-.]{_MONTH}[/\-.]\d{{2}})\b", re.IGNORECASE),
    # Mon DD, YYYY / Mon DD YYYY (US-style) e.g. "Jan 15, 2024"
    re.compile(rf"\b({_MONTH}\.?\s+\d{{1,2}},?\s+\d{{4}})\b", re.IGNORECASE),
    # Mon-DD-YYYY / Mon/DD/YYYY  (US-style with separator)
    re.compile(rf"\b({_MONTH}[/\-.]\d{{1,2}}[/\-.]\d{{4}})\b", re.IGNORECASE),
    # D Month YYYY               e.g. "15 January 2024" (also caught above, kept for clarity)
    re.compile(rf"\b(\d{{1,2}}\s+{_MONTH}\s+\d{{2,4}})\b", re.IGNORECASE),
]

DATE_START_RE = re.compile(
    rf"^\d{{1,2}}[/\-.](\d{{1,2}}|{_MONTH})[/\-.]\d{{2,4}}"
    rf"|^\d{{4}}[/\-.]\d{{1,2}}[/\-.]\d{{1,2}}"
    rf"|^\d{{1,2}}{_MONTH}\d{{2,4}}"
    rf"|^\d{{1,2}}\s+{_MONTH}\s+\d{{2,4}}"
    rf"|^{_MONTH}",
    re.IGNORECASE,
)

# Some PDFs split a 4-digit year across two text chunks, e.g. "01-01-20 25".
# Normalize that back to "01-01-2025" before date parsing so the trailing "25"
# does not bleed into the description.
_SPLIT_NUMERIC_YEAR_PREFIX_RE = re.compile(
    r"^(\d{1,2}[/\-.]\d{1,2}[/\-.]\d{2})\s+(\d{2})(?=\D|$)"
)


def _normalize_split_year_prefix(text: str) -> str:
    if not text:
        return text
    return _SPLIT_NUMERIC_YEAR_PREFIX_RE.sub(r"\1\2", text, count=1)

AMOUNT_RE = re.compile(r"-?[\d,]+\.\d{2}")
SIGNED_AMOUNT_RE = re.compile(r"[+\-]?[\d,]+\.\d{2}")
FLEX_AMOUNT_RE = re.compile(r"-?[\d,]+(?:\.\d{1,2})?$")
# For finding amounts in text lines (must be whitespace-bounded)
# Captures optional +/- prefix so Mashreq-style "+2.00" amounts are recognised.
TEXT_AMOUNT_RE = re.compile(r"(?:^|\s)([+\-]?[\d,]+(?:\.\d{1,2})?)(?=\s|$)")
CELL_AMOUNT_RE = re.compile(r"[+\-]?[\d,]+\.\d{1,2}")

# ── Universal Column Keyword Sets ─────────────────────────────────────────────
# Covers major banks: US, UK, India, Middle East, Southeast Asia, Africa, etc.

DATE_KEYWORDS = {
    "date", "dt",
    # Transaction date variants
    "transaction date", "txn date", "tran date", "trans date", "trans. date",
    "txn dt", "tran dt", "trans dt", "transaction dt",
    # Posting / value date variants
    "posting date", "post date", "posted date", "post dt",
    "value date", "val date", "val. date", "value dt",
    "entry date", "effective date", "effective dt",
    "trade date", "settlement date", "settlement dt",
    "process date", "processed date", "processing date",
    "book date", "booking date", "clearing date",
    # Short abbreviations used by specific banks
    "vd", "pd", "txdt", "trdt",
    # Chinese / bilingual layouts
    "日期", "过账日期", "交易日期", "记账日期", "起息日",
}

DESC_KEYWORDS = {
    "description", "desc",
    "particulars", "particular",
    "narration", "narrative",
    "details", "detail",
    "remarks", "remark",
    "memo", "note", "notes",
    # Compound names
    "transaction details", "transaction description",
    "txn description", "txn details",
    "payment details", "payment description",
    "cheque details", "check details", "chq details",
    # Payee / beneficiary
    "payee", "beneficiary", "beneficiary name",
    "merchant", "merchant name", "counterparty",
    # Other region-specific names
    "reference description", "ref description",
    "chalan description", "instrument description",
    # Transaction column used by some banks (Mashreq Standard) as the description column
    "transaction", "transactions",
    # Chinese / bilingual layouts
    "摘要", "附言", "交易附言", "交易说明",
}

DEBIT_KEYWORDS = {
    "debit", "dr", "dr.",
    "debit amount", "dr amount", "dr. amount", "debit amt", "dr amt",
    # Withdrawal variants
    "withdrawal", "withdrawals", "withdrawal amount",
    "withdraw", "wdl", "wdrawal",
    # Payment / outflow
    "paid out", "money out", "outflow",
    "payment", "payments",
    "charge", "charges",
    "spent", "deductions",
    "disbursement", "disbursements",
    # Chinese / bilingual layouts
    "汇出金额", "支出金额", "借方", "付款金额",
}

CREDIT_KEYWORDS = {
    "credit", "cr", "cr.",
    "credit amount", "cr amount", "cr. amount", "credit amt", "cr amt",
    # Deposit variants
    "deposit", "deposits", "deposit amount",
    # Receipt / inflow
    "paid in", "money in", "inflow",
    "receipt", "receipts",
    "received", "income",
    "collection", "collections",
    # Chinese / bilingual layouts
    "汇入金额", "收入金额", "贷方", "收款金额",
}

BALANCE_KEYWORDS = {
    "balance", "bal", "bal.",
    "closing balance", "opening balance",
    "running balance", "current balance",
    "available balance", "avail balance",
    "ledger balance", "book balance",
    "outstanding balance",
    "end balance", "ending balance",
    "net balance", "total balance",
    "o/b", "c/b",
    # Chinese / bilingual layouts
    "余额", "结余", "当前余额",
}

AMOUNT_KEYWORDS = {
    "amount", "amt",
    "transaction amount", "txn amount", "tran amount", "trans amount",
    "net amount", "net amt",
    "local amount", "foreign amount",
    # Combined Dr/Cr column (signed single amount column) — various separator styles
    "dr/cr", "cr/dr", "dr / cr", "cr / dr",
    "dr./cr.", "cr./dr.", "dr. / cr.", "cr. / dr.",
    "dr./cr", "cr./dr",
    "debit/credit", "credit/debit",
    "debit / credit", "credit / debit",
    "withdrawals/deposits", "deposits/withdrawals",
}

# Columns to skip — reference numbers, cheque nos, etc. are not transaction data
SKIP_COL_KEYWORDS = {
    "reference", "ref", "ref.", "ref no", "ref no.", "reference no",
    "reference no.", "reference number", "ref number",
    "cheque", "cheque no", "cheque no.", "cheque number",
    "check", "check no", "check no.", "check number",
    "chq", "chq no", "chq no.", "chq number",
    "serial", "serial no", "serial no.", "sr no", "sr. no", "s.no", "s/no", "s no", "s no.",
    "voucher", "voucher no", "voucher no.", "voucher number",
    "instrument no", "instrument number",
    "sequence", "seq", "seq no", "seq no.",
    "tran id", "transaction id", "txn id", "trans id",
    "no.", "no", "#",
    "branch", "branch code", "branch name",
    "channel", "type", "trans type", "transaction type",
    "currency", "ccy", "cur",
    "mode",
    # Chinese / bilingual layouts
    "银行参考号", "客户参考号", "参考号", "时间", "trn 类型", "trn类型", "类型",
}

# Rows containing these phrases (case-insensitive) are not real transactions.
# These are financial summary lines that should ALWAYS be skipped, even in
# header-contaminated rows — they are never real transaction descriptions.
SKIP_PHRASES = [
    "total records", "total debit", "total credit", "grand total",
    "opening balance", "closing balance", "balance brought", "balance carried",
    "statement summary", "end of statement",
    "balance b/f", "balance c/f", "brought forward", "carried forward",
    "summary of accounts", "summary of savings",
]

# Softer skip phrases — only applied when the row is NOT header-contaminated.
# These appear in page headers that pdfplumber may merge into data rows;
# skipping them on contaminated rows would kill valid first-row transactions.
_SOFT_SKIP_PHRASES = [
    "please review this account statement",
    "account statement from",
    "account type", "account holder",
    "if no issues are reported",
    "confirmation of the correctness", "correctness of the statement",
    "electronically generated statement", "does not require a signature",
    "does not require . a signature",
    "no notice of disagreement", "paid up capital",
    "registered details: emirates",
    "account summary",
    "your account summary",
    "your adib account",
    "kindly avoid sharing",
]

# Regex to detect IBAN numbers in description text (clear sign of a header dump)
_IBAN_RE = re.compile(r"\b[A-Z]{2}\d{2}[A-Z0-9]{12,}\b")

# Description starts with these → page header content was merged in by pdfplumber.
# We try to strip the header prefix and recover the real description; only skip the
# row entirely if no real description can be extracted after the header block.
_DESC_HEADER_RES = [
    re.compile(r"^account statement\b", re.IGNORECASE),
    re.compile(r"^your bank statement\b", re.IGNORECASE),
    re.compile(r"^statement of account\b", re.IGNORECASE),
    re.compile(r"^account summary\b", re.IGNORECASE),
    re.compile(r"^summary of account", re.IGNORECASE),
    re.compile(r"^transaction history\b", re.IGNORECASE),
    re.compile(r"^your\s+\w+\s+account\s+statement\b", re.IGNORECASE),  # "Your ADIB Account Statement"
]

# Matches a single page-header metadata field (keyword + value).
# Used to find where the page header block ends inside a contaminated description cell,
# e.g. "Transaction history Acme Corp Currency AED Branch Al Tawar Branch <real desc>"
#                                       ^^^^^^^^^^^^^ ^^^^^^^^^^^^^^^^^^^^^^^^^
# Taking the end position of the LAST match gives the start of the real description.
_PAGE_META_FIELD_RE = re.compile(
    r"(?:"
    r"(?:currency|cur)\s+[A-Z]{2,4}"                     # Currency AED / CUR AED
    r"|page\s+(?:number\s+)?\d+"                          # Page 1 / Page number 1
    r"|account\s+(?:type|number|no\.?|holder)\s+\S+"      # Account type / Account number
    r"|iban\s+[A-Z]{2}[\dA-Z]{10,}"                      # IBAN AE86041...
    r"|statement\s+period\s+\S+"                          # Statement period ...
    r"|branch(?:\s+\S+){1,3}"                            # Branch Al Tawar Branch
    r")",
    re.IGNORECASE,
)

# When these phrases appear INSIDE a description, everything from that point onward
# is footer/contact info that was merged into the description — truncate there.
_DESC_FOOTER_MARKERS = [
    "for assistance", "for help,", "for help ", "contact us",
    "customer service", "please call", "helpline", "toll free",
    "call center", "call centre", "if you have any", "should you have any",
    "for queries", "for complaints", "for any query",
    "this is a digital stamp",
    "does not require signature", "does not require . a signature",
    "wio bank pjsc",
    "confirmation of the correctness", "correctness of the statement",
    "electronically generated statement", "registered details: emirates",
    "no notice of disagreement", "paid up capital",
    "tax registration number",
    # Additional metadata phrases that appear mid-description when rows are merged
    "please review this account statement",
    "if no issues are reported",
    "this bank is regulated",
    "account statement from",
    "account opened",
    "account holder name",
    "interest rate",
    "current_account",
    "available balance as of",
    "licensed & regulated",
    "central bank of the uae",
    "statement date",
    "online banking - go to relationship summary",
    "select card>> block card",
    "phone banking - call",
    "unauthorized transaction",
    "wakala deposits is based on wakala contract",
    "savings account is based on mudaraba contract",
    "profit calculation, distribution and payment",
    "complaints management unit",
    "errors and omissions excepted",
    "banking / select card>>manage card >> block card",
    "main menu.",
    # ADIB-specific security warning / header phrases
    "kindly avoid sharing",
    "adib staff will never",
    "click here for more information",
    "not applicable",
    "quick approvals & finance",
    "adib personal finance",
    "sharia, as defined in the aaoifi",
    "sharia standards and the guidance of dib issc",
    "realization of profit from the underlying investments",
    "complaint within an estimated average of",
    # Mashreqbank-specific footer phrases
    "report any discrepancies",
    "all charges, terms and conditions",
    "please note that for foreign currency",
    "subject to change",
    "indicative only",
]
# These markers are only truncated when they appear far enough into the text
# (>= _FOOTER_MIN_POS chars in).  Short descriptions must not be destroyed.
_FOOTER_MIN_POS = 30

# Regex markers for page-header/account-metadata text that bleeds into the
# description column on banks like Wio Business (where each statement page
# repeats the account header above the transactions table). When any of these
# patterns appears inside a description, everything from the match onward is
# header contamination — truncate there. Patterns are highly specific to keep
# false positives away from legitimate transaction text.
#
# Each marker is prefixed with a "company-name run" pattern so any trailing
# all-caps tokens (e.g. "CLOUDFUSION CONSULTING - FZCO") preceding the marker
# get stripped along with the marker itself. The prefix is case-sensitive on
# purpose: it should NOT match real description text like "Subscription fee".
_HDR_COMPANY_PREFIX = r"(?:\s+(?:[A-Z][A-Z0-9_&]+|-|,))*"

_DESC_HEADER_FIELD_RES = [
    # Wio "[Company Name] [CCY] N% FROM dd/mm/yyyy TO dd/mm/yyyy" header signature.
    # Match the leading company-name run too so it's stripped along with the period.
    re.compile(
        r"(?:\s+[A-Z][A-Z0-9_-]*){0,6}\s+[A-Z]{3}\s+\d+%\s+FROM\s+"
        r"\d{1,2}[/\-.]\d{1,2}[/\-.]\d{2,4}\s+TO\s+"
        r"\d{1,2}[/\-.]\d{1,2}[/\-.]\d{2,4}",
        re.IGNORECASE,
    ),
    # Bare statement-period range "FROM dd/mm/yyyy TO dd/mm/yyyy"
    re.compile(
        r"\bFROM\s+\d{1,2}[/\-.]\d{1,2}[/\-.]\d{2,4}\s+TO\s+"
        r"\d{1,2}[/\-.]\d{1,2}[/\-.]\d{2,4}\b",
        re.IGNORECASE,
    ),
    # Account header field labels followed by their value.
    # Case-SENSITIVE on purpose: Wio-style page headers are uppercase
    # ("ACCOUNT NUMBER 12345..."), while legitimate transaction descriptions
    # use mixed case ("Account Number 80309100036511 To ..."). Matching
    # case-insensitively would chop real cheque/transfer descriptions.
    re.compile(r"\bACCOUNT\s+(?:NAME|NUMBER|TYPE|HOLDER|OPENED)\b"),
    # Balance header labels (uppercase only — same reasoning).
    re.compile(r"\b(?:OPENING|CLOSING)\s+BALANCE\b"),
    # Other Wio header literals (uppercase only).
    re.compile(r"\bINTEREST\s+RATE\b"),
    re.compile(r"\bCURRENT_ACCOUNT\b"),
    # Amount-column header text "(Incl. VAT)" / "(Incl VAT)" pulled into description
    re.compile(r"\(\s*Incl\.?\s+VAT\s*\)", re.IGNORECASE),
    # The following markers also swallow any preceding all-caps company-name run
    # so "CLOUDFUSION CONSULTING - FZCO Dubai Silicon Oasis" all gets stripped.
    # Standalone "ACCOUNT STATEMENT" literal mid-description (Wio header title)
    re.compile(_HDR_COMPANY_PREFIX + r"\s+ACCOUNT\s+STATEMENT\b"),
    # "Premises No" — address-line marker from registered-address blocks
    re.compile(_HDR_COMPANY_PREFIX + r"\s+(?:IFZA\s+Business\s+Park|Premises\s+No\.?\s*[:\-]?)"),
    re.compile(_HDR_COMPANY_PREFIX + r"\s+Dubai\s+Silicon\s+Oasis\b"),
    re.compile(_HDR_COMPANY_PREFIX + r"\s+United\s+Arab\s+Emirates\b"),
    # Long digit run (>=8 digits — IBAN tail / account number) followed by a date
    # and one or two amounts. Matches the Wio header tail:
    # "9548511337 28/07/2023 152,822.58 163,570.58"
    re.compile(
        _HDR_COMPANY_PREFIX +
        r"\s+\d{8,}\s+\d{1,2}[/\-.]\d{1,2}[/\-.]\d{2,4}"
        r"(?:\s+[\d,]+(?:\.\d{2})?){1,2}\b"
    ),
]
# These patterns are very specific so we use a lower threshold than _FOOTER_MIN_POS
# — even short descriptions like "Bill" should not retain Wio-style header bleed.
_DESC_HEADER_FIELD_MIN_POS = 5
# Mid-line matches (i.e. matches NOT at the start of a continuation line) are
# treated as suspicious — they may be legitimate transaction text. Require the
# match to sit this far into the description before considering a mid-line cut.
_DESC_HEADER_DEEP_POS = 60
# Safety guard: never truncate a description so aggressively that less than this
# fraction of its original length is kept (unless the match is line-anchored,
# which is a strong header-bleed signal).
_DESC_HEADER_MIN_KEEP_RATIO = 0.5

# Footer markers that legitimately appear inside transaction descriptions
# (e.g. "INTEREST RATE 5%" can be a real credit row).  These ones only count
# when they begin a line, never mid-sentence.
_LINE_ANCHORED_FOOTER_MARKERS = frozenset({
    "interest rate",
    "statement date",
    "subject to change",
    "indicative only",
    "current_account",
    "main menu.",
    "available balance as of",
    "account opened",
    "account holder name",
    "online banking - go to relationship summary",
    "select card>> block card",
    "phone banking - call",
    "banking / select card>>manage card >> block card",
})

# Regex: truncate at © symbol or start of Arabic/RTL block mid-description
_DESC_POISON_RE = re.compile(
    r"©|\(c\)\s*\d{4}"                  # copyright mark
    r"|[\u0600-\u06FF\u0750-\u077F]"    # Arabic / Farsi / Urdu characters
    r"|\bpo\s+box\b",                    # mailing address
    re.IGNORECASE,
)
# Strip trailing phone-number sequences from descriptions.
# Require either a + country prefix OR an actual space/dash separator inside
# the digit run — pure digit sequences are usually transaction reference
# numbers (e.g. "REF 1234567890") and must NOT be stripped.
_TRAILING_PHONE_RE = re.compile(
    r"\s+(?:"
    r"\+\d{1,4}[\s\-]?\d[\d\s\-]{4,}\d"           # +971 4 123 4567
    r"|\d{1,4}[\s\-]+\d{1,4}[\s\-]+\d[\d\s\-]*\d"  # 600 500 946 / 04-123-4567
    r")\s*$"
)

# Column roles that, when appearing consecutively inside a description, indicate
# that the next page's column header row was merged in.
_COL_HEADER_ROLES = frozenset({
    "date", "description", "debit", "credit", "balance", "amount", "skip",
})


def _normalize_multiline_text(value: str) -> str:
    """Normalize text while preserving meaningful line boundaries."""
    if not value:
        return ""

    lines = []
    for raw_line in str(value).replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        line = re.sub(r"\s+", " ", raw_line).strip()
        if line:
            lines.append(line)
    return "\n".join(lines)


def _append_description(base: str, extra: str) -> str:
    """Append description text without flattening line breaks."""
    base = _normalize_multiline_text(base)
    extra = _normalize_multiline_text(extra)
    if not base:
        return extra
    if not extra:
        return base
    return f"{base}\n{extra}"


_PAGE_HEADER_ROW_KEYWORDS = (
    "branch", "currency", "iban", "account number", "account no",
    "account type", "account holder", "statement period", "statement date",
    "page ", "page no", "page number", "transaction history",
    "statement of account", "your bank statement",
)


def _row_looks_like_page_header(row: list[str]) -> bool:
    """True when 2+ cells in a row carry page-header metadata keywords.

    Continuation-row merging would otherwise glue page-header text from the
    top of a new page onto the previous transaction's description.  A single
    keyword can appear inside legitimate transaction text (e.g. "Branch" in a
    payee name); requiring 2 distinct keyword hits across the row keeps that
    case safe while catching real header rows.
    """
    if not row:
        return False
    combined = " ".join(c.strip() for c in row if c and c.strip()).lower()
    if not combined:
        return False
    hits = sum(1 for kw in _PAGE_HEADER_ROW_KEYWORDS if kw in combined)
    return hits >= 2


def _is_standalone_amount_line(text: str) -> bool:
    """True when a text line is just a monetary value, not description text."""
    if not text:
        return False
    normalized = text.strip()
    return bool(re.fullmatch(r"[+\-]?[\d,]+(?:\.\d{1,2})?", normalized))


def _count_date_matches(text: str) -> int:
    """Count distinct date-like spans inside a cell."""
    spans: set[tuple[int, int]] = set()
    for pattern in DATE_PATTERNS:
        for match in pattern.finditer(text or ""):
            spans.add((match.start(), match.end()))
    return len(spans)


def _count_amount_matches(text: str) -> int:
    """Count amount-like substrings inside a cell."""
    return len(CELL_AMOUNT_RE.findall(text or ""))


def _clean_description(description: str) -> str:
    """Strip footer/metadata content merged into a transaction description.

    Handles four cases:
      1. Known text markers ("for assistance", "contact us", etc.)
      2. Arabic characters or © copyright symbol appearing mid-text
      3. Trailing phone-number sequences
      4. Next-page column headers merged in (e.g. "... Amount Balance Date Ref. Number")

    The function is deliberately conservative: it only truncates when the marker
    appears well past the start of the text (_FOOTER_MIN_POS chars in) so that
    short or medium-length descriptions are never accidentally destroyed.
    """
    if not description:
        return description

    description = _normalize_multiline_text(description)

    # 1. Truncate at the EARLIEST known footer text marker — only when it appears
    #    far enough into the description that there is meaningful content before it.
    #    For markers in _LINE_ANCHORED_FOOTER_MARKERS, only truncate when the
    #    marker begins a line (start-of-string or after a newline) — those phrases
    #    legitimately appear inside transaction descriptions and must not chop
    #    real content mid-sentence.
    desc_lower = description.lower()
    earliest_cut = len(description)
    for marker in _DESC_FOOTER_MARKERS:
        if marker in _LINE_ANCHORED_FOOTER_MARKERS:
            # Find marker only at start-of-string or right after a newline
            search_from = 0
            while True:
                idx = desc_lower.find(marker, search_from)
                if idx < 0:
                    break
                if idx == 0 or desc_lower[idx - 1] == "\n":
                    if idx >= _FOOTER_MIN_POS and idx < earliest_cut:
                        earliest_cut = idx
                    break
                search_from = idx + 1
        else:
            idx = desc_lower.find(marker)
            if idx >= _FOOTER_MIN_POS and idx < earliest_cut:
                earliest_cut = idx
    if earliest_cut < len(description):
        description = description[:earliest_cut].strip()
        desc_lower = description.lower()

    # 1b. Truncate at the earliest page-header field regex marker.
    #     These catch metadata bleed (statement period, ACCOUNT NAME, IBAN-style
    #     fields, column-header parentheticals) that follows the real description
    #     when banks like Wio repeat the account header above each page's table.
    #
    #     Two acceptance rules:
    #       (a) Line-anchored match — the match sits at the start of a continuation
    #           line (preceded only by whitespace after a newline). This is the
    #           strong header-bleed signal: real bleed almost always follows a
    #           line break in the cell. Accepted at the basic _DESC_HEADER_FIELD_MIN_POS.
    #       (b) Mid-line match — match sits inside a line of running text. This is
    #           riskier (could be legitimate "valid FROM dd/mm/yyyy TO dd/mm/yyyy"
    #           transaction text). Require both _DESC_HEADER_DEEP_POS depth AND
    #           that the cut keeps at least _DESC_HEADER_MIN_KEEP_RATIO of the
    #           original text.
    earliest_regex_cut = len(description)
    desc_len = len(description)

    def _accept_header_match(pos: int) -> bool:
        if pos < _DESC_HEADER_FIELD_MIN_POS:
            return False
        last_nl = description.rfind("\n", 0, pos)
        line_anchored = last_nl >= 0 and description[last_nl + 1:pos].strip() == ""
        if line_anchored:
            return True
        # Mid-line match: stricter guards
        if pos < _DESC_HEADER_DEEP_POS:
            return False
        if pos < desc_len * _DESC_HEADER_MIN_KEEP_RATIO:
            return False
        return True

    for pattern in _DESC_HEADER_FIELD_RES:
        m = pattern.search(description)
        if m and _accept_header_match(m.start()) and m.start() < earliest_regex_cut:
            earliest_regex_cut = m.start()
    # Bare IBAN inside description is also a page-header signal
    iban_m = _IBAN_RE.search(description)
    if iban_m and _accept_header_match(iban_m.start()) and iban_m.start() < earliest_regex_cut:
        earliest_regex_cut = iban_m.start()
    if earliest_regex_cut < len(description):
        description = description[:earliest_regex_cut].strip()
        desc_lower = description.lower()

    # 2. Truncate at © or first Arabic character (clearly non-transaction content)
    #    Only if there's enough real content before it.
    cid_match = re.search(r"(?:\(cid:\d+\)){2,}", description, re.IGNORECASE)
    m = cid_match or _DESC_POISON_RE.search(description)
    if m and m.start() >= _FOOTER_MIN_POS:
        description = description[:m.start()].strip()

    # 3. Strip trailing phone-number-like digit sequences
    description = _TRAILING_PHONE_RE.sub("", description).strip()

    # Drop standalone header lines like "Description" that can be merged into
    # the first data row on a page break.
    lines = description.split("\n")
    while lines and _is_header_only_line(lines[0]):
        lines.pop(0)

    # Drop trailing standalone amount lines that come from summary/footer balances
    while len(lines) > 1 and _is_standalone_amount_line(lines[-1]):
        lines.pop()

    # Filter out individual noise lines from multiline descriptions.
    # This catches footer/header lines that were merged as continuation rows.
    if len(lines) > 1:
        cleaned_lines = []
        for ln in lines:
            if _is_noise_line(ln):
                continue
            cleaned_lines.append(ln)
        lines = cleaned_lines if cleaned_lines else lines[:1]

    description = "\n".join(lines).strip()

    # 4. Detect consecutive column-header words merged into description.
    #    e.g. "Owais Arshad Khan Amount Balance Date Ref. Number Description..."
    #    Require at least 4 consecutive header words AND real content before them
    #    to avoid false positives on descriptions containing words like "Balance"
    #    or "Credit" in normal transaction text.
    words = description.split()
    run_start: int | None = None
    run_len = 0
    for i, word in enumerate(words):
        # Strip punctuation/brackets but keep dots (for "Ref.")
        clean_word = re.sub(r"[^A-Za-z0-9.]", "", word)
        role, conf = classify_column_header(clean_word)
        if conf >= 0.85 and role in _COL_HEADER_ROLES:
            if run_start is None:
                run_start = i
            run_len += 1
            # Require at least 4 consecutive header words AND real content before them
            if run_len >= 4 and run_start > 0:
                description = " ".join(words[:run_start]).strip()
                break
        else:
            run_start = None
            run_len = 0

    return description


def _is_header_only_line(line: str) -> bool:
    """Return True when a line is just column-header text, not transaction text."""
    stripped = line.strip()
    if not stripped:
        return True
    if any(ch.isdigit() for ch in stripped):
        return False

    words = [re.sub(r"[^A-Za-z0-9.]", "", word).lower() for word in stripped.split()]
    words = [word for word in words if word]
    if not words or len(words) > 4:
        return False

    header_words = {
        "date", "description", "details", "detail", "debit", "credit", "balance",
        "amount", "ref", "ref.", "reference", "number", "no", "no.", "value",
        "chq", "chq.", "cheque",
    }
    return all(word in header_words for word in words)


def _strip_page_header_prefix(description: str) -> str:
    """Extract the real transaction text from a description contaminated with page header content.

    Some bilingual bank statements (e.g. Sharjah Islamic Bank) have a page-header
    block whose x-coordinates overlap the description column.  pdfplumber merges it
    into the first cell of the table, producing text like:

        "Transaction history DMM CONSULTING LLC Currency AED Branch Al Tawar Branch
         Inward Telex Payment/Cellular Tech FZC/..."

    We find the LAST occurrence of a known metadata field pattern (Currency, Branch,
    Page, IBAN …) within the first 200 characters (the header block is always at the
    start) and return everything that follows it as the real description.
    Returns "" if no metadata boundary can be located via metadata fields,
    but tries a fallback: skip past the header-trigger pattern itself and
    return whatever follows.
    """
    # Limit search to the first 400 chars so that words like "Branch" or "Currency"
    # appearing inside the real description don't cause a false truncation point.
    search_in = description[:400]
    matches = list(_PAGE_META_FIELD_RE.finditer(search_in))

    # Also look for bare IBANs (e.g. "AE60086000009098870202") as boundary markers —
    # many statements embed the IBAN without the "IBAN" prefix.
    bare_iban_matches = list(_IBAN_RE.finditer(search_in))
    if bare_iban_matches:
        matches.extend(bare_iban_matches)
        matches.sort(key=lambda m: m.end())

    if matches:
        # Use the LAST metadata-field match as the boundary; everything after it
        # is the real description.  But first skip any trailing amounts / currency
        # codes / percentages / dates that are part of the header metadata.
        remainder = description[matches[-1].end():].strip()
        # Strip leading noise tokens: amounts (123.45), percentages (0%),
        # currency codes (AED), dates (08/10/2022), and common header words
        remainder = re.sub(
            r"^(?:"
            r"\d{1,2}[/\-.]\d{1,2}[/\-.]\d{2,4}"     # dates (try first — avoids partial match)
            r"|[\d,.]+%?"                               # amounts / percentages
            r"|[A-Z]{2,4}"                              # currency codes
            r"|(?:please\s+review|if\s+no\s+issues)"   # header phrases
            r"|\s+"
            r")+",
            "", remainder, flags=re.IGNORECASE
        ).strip()
        if len(remainder) > 3:
            # If the remainder is still very long and contaminated, try the
            # column-header-run approach below before returning.
            if len(remainder) < 200:
                return remainder

    # Reverse search: find the LAST column-header run (e.g. "Amount Balance Date
    # Ref. Number Description") and return everything after it.  This handles cases
    # where the entire text is header metadata followed by column headers followed
    # by the real transaction description at the very end.
    words = description.split()
    last_run_end: int | None = None
    run_start_idx: int | None = None
    run_len = 0
    for i, word in enumerate(words):
        clean_word = re.sub(r"[^A-Za-z0-9.]", "", word)
        role, conf = classify_column_header(clean_word)
        if conf >= 0.85 and role in _COL_HEADER_ROLES:
            if run_start_idx is None:
                run_start_idx = i
            run_len += 1
            if run_len >= 3:
                last_run_end = i + 1  # Track the end of this run
        else:
            run_start_idx = None
            run_len = 0

    if last_run_end is not None and last_run_end < len(words):
        # Continue past straggler header-adjacent words (e.g. "Number" after "Ref.",
        # or "Description" which is a column header but broke the run detection).
        skip_idx = last_run_end
        _extra_header_words = {
            "number", "no", "no.", "ref", "ref.", "reference",
            "description", "amount", "date", "balance",
            "type", "details", "particular", "particulars",
        }
        while skip_idx < len(words):
            w = words[skip_idx].strip("(),.").lower()
            if w in _extra_header_words:
                skip_idx += 1
            else:
                break
        after_headers = " ".join(words[skip_idx:]).strip()
        # Skip parenthetical qualifiers like "(Incl. VAT)" or "(AED)"
        after_headers = re.sub(r"^(?:\s*\([^)]*\)\s*)+", "", after_headers).strip()
        if len(after_headers) > 3:
            return after_headers

    # Fallback: skip past the header-trigger pattern that flagged this as contaminated.
    # e.g. "Account statement Salary Transfer From XYZ" → "Salary Transfer From XYZ"
    for p in _DESC_HEADER_RES:
        m = p.search(description)
        if m:
            remainder = description[m.end():].strip()
            if len(remainder) > 3:
                return remainder

    return ""


def _recover_description_from_words(page, row_idx: int, table_bbox: tuple | None,
                                     col_map: dict, table: list[list[str]]) -> str:
    """Recover a transaction description by reading word positions from the PDF page.

    When table extraction gives an empty or header-contaminated description (common
    for the first row on each page), we fall back to extracting words from the page
    that overlap the description column's x-range and the row's y-range.

    Args:
        page: pdfplumber page object
        row_idx: index of the row in the table (0-based, after header skip)
        table_bbox: bounding box of the table on the page, or None
        col_map: column mapping dict
        table: the cleaned table data

    Returns:
        Recovered description string, or "" if recovery fails.
    """
    if page is None or "description" not in col_map:
        return ""

    try:
        words = page.extract_words()
        if not words:
            return ""

        # Get the description column index
        desc_idx = col_map["description"]

        # We need to find the y-range for this row. Use the table's bounding box
        # and distribute rows evenly, or find the date word's y-position.
        date_idx = col_map.get("date")
        date_val = table[row_idx][date_idx].strip() if date_idx is not None and date_idx < len(table[row_idx]) else ""
        if not date_val:
            return ""

        # Find the date word on the page to anchor the y-position
        target_y = None
        for w in words:
            if w["text"].strip() == date_val.split()[0]:  # Match first part of date
                target_y = w["top"]
                break

        # Fallback: try partial date match
        if target_y is None:
            date_fragment = date_val[:6]  # e.g. "01/03/" or "01-Mar"
            for w in words:
                if date_fragment in w["text"]:
                    target_y = w["top"]
                    break

        if target_y is None:
            return ""

        # Collect all words on the same y-line (within tolerance)
        y_tolerance = 5
        row_words = [w for w in words if abs(w["top"] - target_y) <= y_tolerance]
        if not row_words:
            return ""

        # Sort by x-position
        row_words.sort(key=lambda w: w["x0"])

        # Determine x-boundaries for the description column.
        # The description starts after the date column and ends before the next
        # numeric column (debit/credit/amount/balance).
        # Use column headers' x-positions if available from word-based detection,
        # otherwise estimate from the table structure.

        # Find x-ranges of non-description columns to exclude
        # Collect x-positions of amount-like words (numbers with decimals/commas)
        amount_words_x = []
        for w in row_words:
            if AMOUNT_RE.match(w["text"].replace(",", "")):
                amount_words_x.append(w["x0"])

        # Description words: not the date word, not amount words, and between
        # the date's x-end and the first amount's x-start
        date_word_end = None
        for w in row_words:
            if w["text"].strip().startswith(date_val.split()[0][:4]):
                date_word_end = w["x1"]
                break

        if date_word_end is None:
            date_word_end = row_words[0]["x1"] if row_words else 0

        # First amount x-position marks the end of description zone
        desc_end_x = min(amount_words_x) - 5 if amount_words_x else float("inf")

        desc_parts = []
        for w in row_words:
            # Skip words that are before the description column
            if w["x0"] < date_word_end - 2:
                continue
            # Skip words that are in numeric columns
            if w["x0"] >= desc_end_x:
                continue
            # Skip pure numbers (they belong to amount columns); also skip
            # +/- prefixed amounts like "+2.00" (Mashreq-style signed columns).
            if AMOUNT_RE.match(w["text"].replace(",", "").lstrip("+")):
                continue
            # Skip date words
            if any(p.match(w["text"]) for p in DATE_PATTERNS):
                continue
            desc_parts.append(w["text"])

        # Also check continuation lines (words on y-lines just below the date line)
        # These are multi-line descriptions within the same table row.
        continuation_y_max = target_y + 40  # Look up to ~40px below
        next_date_y = float("inf")
        # Find the next row's date to limit how far down we look
        for w in words:
            if w["top"] > target_y + y_tolerance and any(p.match(w["text"]) for p in DATE_PATTERNS):
                next_date_y = w["top"]
                break

        continuation_y_max = min(continuation_y_max, next_date_y - 2)

        for w in words:
            if w["top"] <= target_y + y_tolerance:
                continue
            if w["top"] > continuation_y_max:
                continue
            if w["x0"] < date_word_end - 2:
                continue
            if w["x0"] >= desc_end_x:
                continue
            if AMOUNT_RE.match(w["text"].replace(",", "")):
                continue
            desc_parts.append(w["text"])

        recovered = " ".join(desc_parts).strip()
        recovered = re.sub(r"\s+", " ", recovered)

        # Don't return if it's just noise
        if recovered and not _is_noise_line(recovered):
            return recovered

    except Exception as e:
        logger.debug(f"Description recovery from words failed: {e}")

    return ""


def classify_column_header(header: str) -> tuple[str, float]:
    """Classify a column header string into a canonical role.

    Returns (role, confidence) where role is one of:
      "date" | "description" | "debit" | "credit" | "balance" | "amount" | "skip" | "unknown"
    and confidence is 0.0–1.0.

    Strategy (highest wins):
      1. Exact match in keyword set       → 1.0
      2. Substring / containment match    → 0.85
      3. Partial word match               → 0.70
    Skip columns are detected first to avoid false positives.
    """
    if not header:
        return "unknown", 0.0

    # Bilingual layouts (Arabic/Hebrew on top of English) often render as a
    # single header cell like "التاريخ Date" or "الرصيد\nBalance". The Arabic
    # glyphs prevent the English keyword from matching, so strip RTL ranges
    # first and use the ASCII-only form when it has enough English content.
    # Pure-Arabic or pure-Chinese headers (no English) keep their original
    # form so existing language-specific keywords (e.g. "银行参考号") still match.
    _RTL_CHARS_RE = re.compile(r"[֐-׿؀-ۿ܀-ݏݐ-ݿࢠ-ࣿיִ-ﻼ]+")
    _stripped = _RTL_CHARS_RE.sub(" ", header)
    _stripped = re.sub(r"\s+", " ", _stripped).strip()
    if len(_stripped) >= 2 and _stripped != header.strip():
        h_source = _stripped
    else:
        h_source = header

    # Normalise: keep Unicode headers so bilingual layouts can be classified.
    h = h_source
    h = re.sub(r"[*#()\[\]]", "", h)
    h = re.sub(r"\s+", " ", h).strip().lower()
    if not h:
        return "unknown", 0.0

    # ── 1. SKIP columns (highest priority) ──────────────────────────────────
    if h in SKIP_COL_KEYWORDS:
        return "skip", 1.0

    # Normalise dots-as-separators for abbreviations like "Ref. Number" → "ref number",
    # "S.No." → "s no", "Chq.No" → "chq no"
    h_nodot = re.sub(r"\.\s*", " ", h).strip()
    h_nodot = re.sub(r"\s+", " ", h_nodot)
    if h_nodot in SKIP_COL_KEYWORDS:
        return "skip", 1.0

    # Starts-with check: "Ref. Number Description" still starts with a skip keyword,
    # so the whole phrase is skip (multi-word cross-column false match guard).
    # Narrow exception: only "reference description" / "ref description" (exact) are
    # valid description column headers — NOT "ref number description" or similar.
    _skip_desc_exceptions = {"reference description", "ref description"}
    if h not in _skip_desc_exceptions and h_nodot not in _skip_desc_exceptions:
        _skip_lead = sorted(SKIP_COL_KEYWORDS, key=len, reverse=True)  # longest first
        for _sk in _skip_lead:
            if h.startswith(_sk + " ") or h_nodot.startswith(_sk + " "):
                return "skip", 0.95

    _skip_substrings = ("cheque no", "check no", "chq no", "ref no", "ref. no",
                        "voucher no", "serial no", "tran id", "txn id", "trans id",
                        "instrument no")
    if any(sub in h for sub in _skip_substrings) or any(sub in h_nodot for sub in _skip_substrings):
        # Don't skip if it also meaningfully identifies a real column
        if not any(sub in h for sub in ("description", "narration", "particular", "detail")):
            return "skip", 0.9

    scores: dict[str, float] = {}

    def _score(role: str, kw_set: set, substrings: tuple) -> None:
        if h in kw_set:
            scores[role] = max(scores.get(role, 0.0), 1.0)
        elif any(sub in h for sub in substrings):
            scores[role] = max(scores.get(role, 0.0), 0.85)

    # ── 2. Date ──────────────────────────────────────────────────────────────
    _score("date", DATE_KEYWORDS,
           ("date", " dt", "dated"))
    # bare "dt" only when it IS the whole header
    if h == "dt":
        scores["date"] = max(scores.get("date", 0.0), 0.80)

    # ── 3. Description ───────────────────────────────────────────────────────
    _score("description", DESC_KEYWORDS,
           ("description", "narration", "particular", "detail", "remark",
            "memo", "narrative", "beneficiar", "payee", "merchant"))

    # ── 4. Debit ─────────────────────────────────────────────────────────────
    _score("debit", DEBIT_KEYWORDS,
           ("debit", "withdrawal", "paid out", "money out", "outflow",
            "payment", "charge", "disbursement", "wdl"))
    if h in ("dr", "dr."):
        scores["debit"] = max(scores.get("debit", 0.0), 0.95)

    # ── 5. Credit ────────────────────────────────────────────────────────────
    _score("credit", CREDIT_KEYWORDS,
           ("credit", "deposit", "paid in", "money in", "inflow",
            "receipt", "collection"))
    if h in ("cr", "cr."):
        scores["credit"] = max(scores.get("credit", 0.0), 0.95)

    # ── 6. Balance ───────────────────────────────────────────────────────────
    _score("balance", BALANCE_KEYWORDS,
           ("balance", "bal."))
    # bare "bal" only when it IS the whole header (avoid "global", etc.)
    if h in ("bal", "bal."):
        scores["balance"] = max(scores.get("balance", 0.0), 0.90)

    # ── 7. Amount (combined/signed single column) ────────────────────────────
    _score("amount", AMOUNT_KEYWORDS,
           ("amount", " amt", "dr/cr", "cr/dr", "debit/credit", "credit/debit",
            "withdrawals/deposits", "deposits/withdrawals"))
    if h in ("dr/cr", "cr/dr", "dr / cr", "cr / dr",
             "dr./cr.", "cr./dr.", "dr. / cr.", "cr. / dr.",
             "debit/credit", "credit/debit"):
        scores["amount"] = max(scores.get("amount", 0.0), 0.95)
    # Also catch via h_nodot (e.g. "dr./cr." → "dr /cr" won't match above,
    # but "dr /cr" contains "dr" and "cr" — check common patterns)
    if h_nodot in ("dr /cr", "cr /dr", "dr/ cr", "cr/ dr"):
        scores["amount"] = max(scores.get("amount", 0.0), 0.90)

    if not scores:
        return "unknown", 0.0

    best_role = max(scores, key=lambda r: scores[r])
    return best_role, scores[best_role]


def _score_header_line(sorted_words: list[dict]) -> tuple[int, list[tuple[str, float]]]:
    """Score a line of sorted (by x) words for column header matches.

    Tries 3-word → 2-word → 1-word phrase combinations so that multi-word
    headers like "Transaction Date" or "Paid Out" are matched as a unit.

    IMPORTANT: If the current word alone is a strong skip (e.g. "Ref.", "Cheque"),
    we record it as skip immediately WITHOUT extending to longer phrases.  This
    prevents cross-column combinations like "Ref. Number Description" from being
    misclassified as a description column.

    Returns:
        score     — number of meaningful (non-skip/unknown) roles found
        col_roles — list of (role, x0) for every matched phrase
    """
    col_roles: list[tuple[str, float]] = []
    score = 0
    i = 0
    while i < len(sorted_words):
        # ── Early-exit for strong single-word skip tokens ─────────────────
        # e.g. "Ref.", "Cheque", "Serial" — do NOT try to extend these into
        # longer phrases that would span the next column's header words.
        single_role, single_conf = classify_column_header(sorted_words[i]["text"])
        if single_role == "skip" and single_conf >= 0.90:
            col_roles.append(("skip", sorted_words[i]["x0"]))
            i += 1
            continue

        # Try 2-word combination ONLY when the current word alone is weak.
        # 3-word phrases are intentionally avoided — they easily span column boundaries
        # (e.g. "Number Description Amount" would consume three separate columns).
        matched = False
        if i + 1 < len(sorted_words):
            next_role, next_conf = classify_column_header(sorted_words[i + 1]["text"])
            # Only combine when the next word is also individually weak (not a strong
            # standalone header word) AND is not a skip boundary.
            if next_conf < 0.70 and next_role != "skip":
                phrase = sorted_words[i]["text"] + " " + sorted_words[i + 1]["text"]
                role, confidence = classify_column_header(phrase)
                if confidence >= 0.70:
                    col_roles.append((role, sorted_words[i]["x0"]))
                    if role not in ("skip", "unknown"):
                        score += 1
                    i += 2
                    matched = True

        if not matched:
            # Single-word fallback
            role, confidence = classify_column_header(sorted_words[i]["text"])
            if confidence >= 0.70:
                col_roles.append((role, sorted_words[i]["x0"]))
                if role not in ("skip", "unknown"):
                    score += 1
            i += 1
    return score, col_roles


def _heal_truncated_balances(transactions: list[dict], pdf_path: Path) -> int:
    """Replace single-decimal balance values with their full-precision counterpart
    from the PDF's word extraction.

    pdfplumber's text-strategy table extraction occasionally clips the last digit
    of a balance value when the column boundary lands inside the word — e.g. the
    PDF actually contains "122,489.07" but the table cell reads "122,489.0". The
    page-level `extract_words()` call doesn't have this bug (it gives the full
    string), so we collect all 2-decimal candidates from page words and look up
    unique matches by prefix.
    """
    # Collect transactions that look truncated (decimal part has exactly 1 digit)
    suspects = [
        t for t in transactions
        if re.match(
            r"^-?[\d,]+\.\d$",
            str(t.get("balance", "") or "").replace("CR", "").replace("DR", "").strip(),
        )
    ]
    if not suspects:
        return 0

    # Collect every full-precision balance-like word across all pages
    full_words: set[str] = set()
    try:
        import pdfplumber  # noqa: PLC0415
        with pdfplumber.open(pdf_path) as pdf:
            for page in pdf.pages:
                for w in page.extract_words():
                    txt = w["text"].strip()
                    if re.match(r"^-?[\d,]+\.\d{2}(?:CR|DR)?$", txt):
                        full_words.add(txt)
    except Exception as e:
        logger.debug(f"Balance healing word-scan failed: {e}")
        return 0

    def _to_num(s: str) -> float | None:
        if not s:
            return None
        cleaned = re.sub(r"[^\d.\-]", "", str(s).rstrip("CRDcrd").strip())
        try:
            return float(cleaned)
        except ValueError:
            return None

    healed = 0
    for idx, t in enumerate(transactions):
        if t not in suspects:
            continue
        raw = str(t.get("balance", "") or "").strip()
        suffix = "CR" if raw.endswith("CR") else "DR" if raw.endswith("DR") else ""
        core = raw[:-2] if suffix else raw
        # Strict prefix match: full word starts with the truncated core AND
        # adds exactly one more digit before any optional suffix.
        candidates = [
            fw for fw in full_words
            if fw.startswith(core)
            and len(fw) == len(core) + 1 + (2 if fw.endswith(("CR", "DR")) else 0)
        ]
        if not candidates:
            continue
        if len(candidates) == 1:
            t["balance"] = candidates[0]
            healed += 1
            continue
        # Multiple candidates — disambiguate by looking at the neighbouring rows.
        # The true balance must satisfy: |this_bal - neighbour_bal| == neighbour_amount
        # for the neighbour whose amount value is known.
        chosen: str | None = None
        for offset in (1, -1):
            nb_idx = idx + offset
            if nb_idx < 0 or nb_idx >= len(transactions):
                continue
            nb = transactions[nb_idx]
            nb_bal = _to_num(nb.get("balance", ""))
            nb_amt = max(_to_num(nb.get("debit", "")) or 0, _to_num(nb.get("credit", "")) or 0)
            if nb_bal is None or nb_amt <= 0:
                continue
            best_c = None
            best_residual = float("inf")
            for c in candidates:
                c_num = _to_num(c)
                if c_num is None:
                    continue
                residual = abs(abs(c_num - nb_bal) - nb_amt)
                if residual < best_residual:
                    best_residual = residual
                    best_c = c
            if best_c is not None and best_residual < 0.01:
                chosen = best_c
                break
        if chosen:
            t["balance"] = chosen
            healed += 1

    if healed:
        logger.info(f"Balance healing: restored full precision on {healed} balance(s)")
    return healed


def _reconcile_debit_credit_via_balance(transactions: list[dict]) -> int:
    """Use the running-balance invariant to correct mis-classified debit/credit.

    Many bank statements (Sharjah Islamic Bank's Debit/Credit are visually
    separate columns but share an x-range, so pdfplumber merges them into one)
    arrive with all amounts in the wrong slot. We can recover the correct
    classification from each row's balance delta vs. the previous row:

        if balance dropped: the transaction was a debit
        if balance rose:    the transaction was a credit

    For each row we cross-check the current debit/credit assignment against
    the delta and flip when (a) the delta is unambiguous and (b) the value
    being flipped matches the delta magnitude. Returns the number of rows
    that were corrected.

    Conservative on purpose — we only flip when the delta exactly matches
    the recorded amount (within 0.02 to tolerate rounding). Rows without a
    previous balance or with ambiguous deltas are left untouched.
    """
    def _to_float(s: str) -> float | None:
        if not s:
            return None
        cleaned = re.sub(r"[^\d.\-]", "", str(s).rstrip("CRDcrd").strip())
        try:
            return float(cleaned)
        except ValueError:
            return None

    def _amounts_match(a: float, b: float) -> bool:
        # Allow both absolute and relative slack: pdfplumber sometimes truncates
        # the last digit of a balance (e.g. "122,489.0" instead of "122,489.07"),
        # so a strict 0.02 absolute tolerance would miss legitimate matches.
        # 0.5 absolute covers one-digit truncation; 0.0001 relative covers
        # rounding on large amounts.
        max_val = max(a, b, 1.0)
        return abs(a - b) < 0.5 or abs(a - b) / max_val < 0.0001

    prev_balance: float | None = None
    corrected = 0
    for t in transactions:
        bal = _to_float(t.get("balance", ""))
        if bal is None:
            continue
        if prev_balance is not None:
            delta = bal - prev_balance
            debit_val = _to_float(t.get("debit", ""))
            credit_val = _to_float(t.get("credit", ""))

            # Balance dropped → the row is a debit
            if delta < -0.005 and credit_val and not debit_val:
                if _amounts_match(credit_val, abs(delta)):
                    t["debit"] = t["credit"]
                    t["credit"] = ""
                    corrected += 1
            # Balance rose → the row is a credit
            elif delta > 0.005 and debit_val and not credit_val:
                if _amounts_match(debit_val, delta):
                    t["credit"] = t["debit"]
                    t["debit"] = ""
                    corrected += 1
        prev_balance = bal

    # Second pass: classify the FIRST row when there was no prev_balance to
    # compare against. We look one row ahead — if the next row's balance and
    # delta tell us whether this row was a debit or credit, we can deduce
    # the same for the current row via the chain
    #     this_bal = prev_opening_bal + (credit_this - debit_this)
    # When the row's amount is in the wrong slot and the next row resolves
    # cleanly via the same matching test, mirror the next row's classification.
    if transactions:
        # Find first reconcilable row pair (i, i+1) where i has only one of
        # debit/credit set but no prev_balance was available.
        for i in range(len(transactions) - 1):
            t = transactions[i]
            n = transactions[i + 1]
            bal_t = _to_float(t.get("balance", ""))
            bal_n = _to_float(n.get("balance", ""))
            if bal_t is None or bal_n is None:
                continue
            debit_t = _to_float(t.get("debit", ""))
            credit_t = _to_float(t.get("credit", ""))
            if (debit_t and credit_t) or (not debit_t and not credit_t):
                continue
            # Compute what the row's delta should have been (from a synthetic
            # opening balance derived from the row's own amount in either slot)
            amount = credit_t if credit_t else debit_t
            # If amount matches an inward direction relative to the *next* row
            # (i.e. balance after this row + next row's effect = next row's bal),
            # we trust the next row's delta to validate which side is correct.
            delta_next = bal_n - bal_t
            debit_n = _to_float(n.get("debit", ""))
            credit_n = _to_float(n.get("credit", ""))
            next_dir_debit = (
                delta_next < -0.005 and (debit_n or credit_n)
                and _amounts_match(debit_n or credit_n, abs(delta_next))
            )
            next_dir_credit = (
                delta_next > 0.005 and (debit_n or credit_n)
                and _amounts_match(debit_n or credit_n, delta_next)
            )
            if not (next_dir_debit or next_dir_credit):
                continue
            # If the next-row check passes, the current row's debit/credit slot
            # must be consistent with its own (unknown) opening balance. We can
            # only flip when the row's amount is sitting in the slot that
            # disagrees with the running-balance direction relative to the next.
            # Concretely: if balance went down across the boundary, the current
            # row's amount belongs to debit. (Reverse for up.)
            # This is a best-effort first-row repair.
            if next_dir_debit and credit_t and not debit_t:
                # Heuristic: when the *very first* row has credit set but the
                # statement is clearly debit-dominated (next row is debit too),
                # the first row is almost certainly a debit too.
                t["debit"] = t["credit"]
                t["credit"] = ""
                corrected += 1
            break  # only attempt repair on the leading row

    if corrected:
        logger.info(f"Balance-delta reconciliation: corrected {corrected} debit/credit assignment(s)")
    return corrected


def extract_transactions(pdf_path: str, template: dict | None = None) -> list[dict]:
    """Extract structured transactions from a bank statement PDF.

    Args:
        pdf_path: Path to the PDF file.
        template: Optional pre-matched template dict from template_engine.
                  If provided and contains a valid col_map, extraction will
                  use the saved column mapping and preferred strategy,
                  skipping column detection for faster, more accurate results.

    Returns:
        List of transaction dicts with keys: date, description, debit, credit, balance.
        The last element may be a metadata dict with key "_extraction_meta" containing
        strategy used, col_map, and template_matched flag (used by server for template saving).
    """
    pdf_path = Path(pdf_path)
    if not pdf_path.exists():
        raise FileNotFoundError(f"PDF file not found: {pdf_path}")
    if pdf_path.suffix.lower() != ".pdf":
        raise ValueError(f"Not a PDF file: {pdf_path}")

    # ── Template-guided extraction ────────────────────────────────────────────
    # If a matching template was found, use its saved col_map and preferred strategy.
    _tmpl_strategy = template.get("strategy") if template else None
    _tmpl_col_map  = template.get("col_map")  if template else None
    # text/words strategies don't require a col_map — allow empty {} or None.
    _tmpl_usable = (
        _tmpl_strategy in ("text", "words")
        or bool(_tmpl_col_map)
    )
    if template and _tmpl_strategy and _tmpl_usable:
        saved_col_map = template["col_map"]
        preferred_strategy = template["strategy"]
        logger.info(
            f"Using saved template (id={template.get('id')}, strategy={preferred_strategy}, "
            f"col_map={saved_col_map})"
        )

        transactions = _extract_with_template(pdf_path, saved_col_map, preferred_strategy)
        if transactions:
            # Quality gate: if template-guided extraction has poor financial data,
            # fall back to the full pipeline which can use hybrid strategies
            # (e.g. PyMuPDF for pages with tables + text for pages without).
            tmpl_quality = _extraction_quality_score(transactions)
            n = len(transactions)
            has_bal_pct = sum(1 for t in transactions if t.get("balance")) / n if n else 0
            if has_bal_pct < 0.40:
                logger.warning(
                    f"Template-guided extraction quality low ({has_bal_pct:.0%} rows have balance, "
                    f"quality={tmpl_quality:.1f}) — falling back to full pipeline"
                )
            else:
                _heal_truncated_balances(transactions, pdf_path)
                _reconcile_debit_credit_via_balance(transactions)
                # Append metadata for the server
                meta = {
                    "_extraction_meta": {
                        "strategy": preferred_strategy,
                        "col_map": saved_col_map,
                        "template_matched": True,
                        "template_id": template.get("id"),
                        "template_bank_name": template.get("bank_name"),
                    }
                }
                transactions.append(meta)
                logger.info(
                    f"Template-guided extraction: {len(transactions) - 1} transactions "
                    f"(template_id={template.get('id')})"
                )
                return transactions
        else:
            logger.warning("Template-guided extraction yielded 0 transactions, falling back to full pipeline")

    # ── Full pipeline (no template or template failed) ────────────────────────
    # Strategy 1: table-based extraction — run PyMuPDF first, then pdfplumber
    # as a co-primary, and use whichever scores higher on quality.
    #
    # For LARGE PDFs (> _LARGE_PDF_PAGES pages) the pdfplumber pass is the
    # single most expensive step in the pipeline. We skip it whenever PyMuPDF
    # already produced a clearly high-quality extraction (≥80% rows with both
    # amounts and balance). Small PDFs always run both strategies — the cost
    # is bounded and the extra coverage is worth it.
    _LARGE_PDF_PAGES = 20
    try:
        import fitz  # PyMuPDF — already a hard dependency  # noqa: PLC0415
        with fitz.open(str(pdf_path)) as _doc:
            _page_count = _doc.page_count
    except Exception:
        _page_count = 0

    pymupdf_txns, pymupdf_col_map = _extract_from_pymupdf(pdf_path)
    pymupdf_ok = bool(pymupdf_txns) and not _has_merged_rows(pymupdf_txns)

    _skip_pdfplumber = False
    if _page_count > _LARGE_PDF_PAGES and pymupdf_ok:
        n_pm = len(pymupdf_txns)
        has_bal_pct = sum(1 for t in pymupdf_txns if t.get("balance")) / n_pm if n_pm else 0
        has_amt_pct = sum(1 for t in pymupdf_txns if t.get("debit") or t.get("credit")) / n_pm if n_pm else 0
        if has_bal_pct >= 0.80 and has_amt_pct >= 0.80:
            logger.info(
                f"Large PDF ({_page_count} pages): PyMuPDF coverage is high "
                f"(amounts={has_amt_pct:.0%}, balance={has_bal_pct:.0%}) — "
                "skipping pdfplumber co-primary to save time"
            )
            _skip_pdfplumber = True

    if _skip_pdfplumber:
        pdfplumber_txns, pdfplumber_col_map = [], None
    else:
        try:
            pdfplumber_txns, pdfplumber_col_map = _extract_from_tables(pdf_path)
        except Exception as e:
            logger.warning(f"pdfplumber table extraction failed: {e}")
            pdfplumber_txns, pdfplumber_col_map = [], None

    pdfplumber_ok = bool(pdfplumber_txns) and not _has_merged_rows(pdfplumber_txns)

    if pymupdf_ok or pdfplumber_ok:
        if pymupdf_ok and pdfplumber_ok:
            # Both found valid transactions — prefer whichever scores higher on
            # quality (weighted by financial data completeness, not raw row count).
            # This prevents a strategy that extracts hundreds of garbage rows from
            # beating one that extracts fewer but correctly structured transactions.
            pymupdf_score = _extraction_quality_score(pymupdf_txns)
            pdfplumber_score = _extraction_quality_score(pdfplumber_txns)
            if pymupdf_score >= pdfplumber_score:
                transactions = pymupdf_txns
                source = "pymupdf"
                detected_col_map = pymupdf_col_map
                # Supplement: inject pdfplumber rows for date ranges winner missed
                transactions = _fill_page_gaps(transactions, pdfplumber_txns, "pymupdf", "pdfplumber")
            else:
                transactions = pdfplumber_txns
                source = "pdfplumber"
                detected_col_map = pdfplumber_col_map
                transactions = _fill_page_gaps(transactions, pymupdf_txns, "pdfplumber", "pymupdf")
        elif pymupdf_ok:
            transactions = pymupdf_txns
            source = "pymupdf"
            detected_col_map = pymupdf_col_map
        else:
            transactions = pdfplumber_txns
            source = "pdfplumber"
            detected_col_map = pdfplumber_col_map

        peer_txns = pdfplumber_txns if source == "pymupdf" else pymupdf_txns
        enriched = _enrich_transactions(transactions, peer_txns)
        if enriched:
            logger.info(f"Enriched {enriched} existing transactions with peer table-strategy data")

        logger.info(
            f"Strategy 1 ({source}): {len(transactions)} transactions "
            f"(pymupdf={len(pymupdf_txns)}, pdfplumber={len(pdfplumber_txns)})"
        )

        # ── Quality gate: if table strategy has very poor financial data coverage,
        # try words/text as an alternative and prefer whichever is better.
        # Threshold: <30% of rows with debit/credit means the col_map is likely wrong.
        table_quality = _extraction_quality_score(transactions)
        n = len(transactions)
        has_amounts_pct = sum(1 for t in transactions if t.get("debit") or t.get("credit")) / n if n else 0
        if has_amounts_pct < 0.30:
            logger.info(
                f"Strategy 1 quality low ({has_amounts_pct:.0%} rows have amounts) — "
                "trying Strategy 2 (words) as alternative"
            )
            try:
                words_txns = _extract_from_words(pdf_path)
                if words_txns and _extraction_quality_score(words_txns) > table_quality:
                    logger.info(
                        f"Words strategy is better ({len(words_txns)} txns) — using it instead"
                    )
                    meta = {"_extraction_meta": {"strategy": "words", "template_matched": False}}
                    words_txns.append(meta)
                    return words_txns
            except Exception as e:
                logger.warning(f"Word extraction (quality fallback) failed: {e}")

        # ── Supplement: if text/words extraction finds significantly more
        # transactions, some pages likely had no tables.  Merge the extra
        # transactions (those not already present by date+amount+balance).
        # Skip supplement when table strategy already has good coverage — running
        # words/text strategies on large PDFs is expensive and rarely helps.
        # For large PDFs we use a stricter threshold: only supplement when the
        # primary clearly missed something (<40% amounts), otherwise the extra
        # two full-document scans cost minutes for marginal gain.
        supplement_candidates = []
        _supplement_threshold = 0.40 if _page_count > _LARGE_PDF_PAGES else 0.70
        if has_amounts_pct < _supplement_threshold:
            for strat_name, strat_fn in [("words", _extract_from_words), ("text", _extract_from_text)]:
                try:
                    strat_txns = strat_fn(pdf_path)
                    if strat_txns and len(strat_txns) > len(transactions) * 1.3:
                        supplement_candidates.append((strat_name, strat_txns))
                except Exception:
                    pass

        if supplement_candidates:
            # Pick the supplement with the highest quality score
            supplement_candidates.sort(key=lambda x: _extraction_quality_score(x[1]), reverse=True)
            supp_name, supp_txns = supplement_candidates[0]

            enriched = _enrich_transactions(transactions, supp_txns)
            if enriched:
                logger.info(
                    f"Enriched {enriched} existing transactions with {supp_name}-strategy data"
                )

            existing_keys = {
                (t.get("date", ""), t.get("debit", ""), t.get("credit", ""), t.get("balance", ""))
                for t in transactions
            }
            added = 0
            for t in supp_txns:
                key = (t.get("date", ""), t.get("debit", ""), t.get("credit", ""), t.get("balance", ""))
                if key not in existing_keys:
                    transactions.append(t)
                    existing_keys.add(key)
                    added += 1
            if added:
                logger.info(
                    f"Supplemented table extraction with {added} {supp_name}-strategy "
                    f"transactions (total now {len(transactions)})"
                )

        # Deduplicate: remove any transactions added by gap-fill that are
        # exact duplicates of existing rows (same date + debit + credit + balance).
        transactions = _deduplicate_transactions(transactions)
        _heal_truncated_balances(transactions, pdf_path)
        _reconcile_debit_credit_via_balance(transactions)

        # Append metadata for template saving (include col_map)
        meta = {"_extraction_meta": {
            "strategy": source,
            "template_matched": False,
            "col_map": detected_col_map or {},
        }}
        transactions.append(meta)
        return transactions

    # Strategy 2: position-based word extraction (for PDFs without table structures)
    logger.info("Table strategies found 0 valid transactions, trying Strategy 2 (words)")
    words_txns: list[dict] = []
    try:
        words_txns = _extract_from_words(pdf_path)
    except Exception as e:
        logger.warning(f"Word extraction failed: {e}")

    # Strategy 3: text-based line parsing — only needed when words strategy is weak.
    # Skip the expensive text scan when words already has good quality (all rows
    # have amounts and ≥95% have balance), since text strategy is much slower.
    text_txns: list[dict] = []
    words_n = len(words_txns)
    words_has_bal_pct = (
        sum(1 for t in words_txns if t.get("balance")) / words_n if words_n else 0
    )
    words_has_amt_pct = (
        sum(1 for t in words_txns if t.get("debit") or t.get("credit")) / words_n if words_n else 0
    )
    if words_has_bal_pct < 0.95 or words_has_amt_pct < 0.90 or words_n == 0:
        try:
            text_txns = _extract_from_text(pdf_path)
        except Exception as e:
            logger.warning(f"Text extraction failed: {e}")
    else:
        logger.info(
            f"Skipping text strategy — words already high quality "
            f"({words_n} txns, {words_has_bal_pct:.0%} balance, {words_has_amt_pct:.0%} amounts)"
        )

    # Pick whichever fallback strategy gives better quality
    if words_txns or text_txns:
        words_score = _extraction_quality_score(words_txns)
        text_score = _extraction_quality_score(text_txns)
        if text_score > words_score:
            logger.info(
                f"Text strategy wins fallback ({len(text_txns)} txns, score={text_score:.1f}) "
                f"over words ({len(words_txns)} txns, score={words_score:.1f})"
            )
            transactions = text_txns
            fallback_source = "text"
        else:
            logger.info(
                f"Words strategy wins fallback ({len(words_txns)} txns, score={words_score:.1f}) "
                f"over text ({len(text_txns)} txns, score={text_score:.1f})"
            )
            transactions = words_txns
            fallback_source = "words"
        if transactions:
            _heal_truncated_balances(transactions, pdf_path)
            _reconcile_debit_credit_via_balance(transactions)
            meta = {"_extraction_meta": {"strategy": fallback_source, "template_matched": False}}
            transactions.append(meta)
            return transactions

    return []


def _extract_with_template(
    pdf_path: Path, col_map: dict, strategy: str,
) -> list[dict]:
    """Extract transactions using a saved template's column mapping and strategy.

    This skips column detection entirely — the col_map is already known.
    Falls back to the full pipeline if the preferred strategy fails.
    """
    txns: list[dict] = []
    peer_candidates: list[list[dict]] = []

    if strategy == "pymupdf":
        txns, _ = _extract_from_pymupdf(pdf_path, forced_col_map=col_map)
        try:
            peer_txns, _ = _extract_from_tables(pdf_path)
            peer_candidates.append(peer_txns)
        except Exception:
            pass
    elif strategy == "pdfplumber":
        txns, _ = _extract_from_tables(pdf_path, forced_col_map=col_map)
        try:
            peer_txns, _ = _extract_from_pymupdf(pdf_path)
            peer_candidates.append(peer_txns)
        except Exception:
            pass
    elif strategy == "words":
        txns = _extract_from_words(pdf_path)
    elif strategy == "text":
        txns = _extract_from_text(pdf_path)
    else:
        return []

    # Enrich the template-guided result with peer strategies before the quality
    # gate runs. This is especially useful when the forced template captures the
    # right rows but intermittently misses the running-balance column.
    for strat_fn in (_extract_from_words, _extract_from_text):
        try:
            peer_candidates.append(strat_fn(pdf_path))
        except Exception:
            pass

    for peer_txns in peer_candidates:
        if peer_txns:
            _enrich_transactions(txns, peer_txns)

    return txns


def _has_merged_rows(transactions: list[dict]) -> bool:
    """Detect if table extraction produced merged rows (multiple transactions per row).

    When pdfplumber can't detect row boundaries, it merges all rows on a page into
    a single row.  The telltale sign: the date field contains multiple dates separated
    by spaces (e.g. "22/01/2025 22/01/2025 20/01/2025 19/01/2025").
    """
    if not transactions:
        return False
    merged_count = 0
    for txn in transactions:
        date = txn.get("date", "")
        # Count how many distinct (non-overlapping) date matches appear in the date field.
        # Multiple patterns can match the same text (e.g. "28 Nov 2024" matches both
        # "D Mon YYYY" and "D Month YY-YYYY"), so deduplicate by match span.
        seen_spans: set[tuple[int, int]] = set()
        for p in DATE_PATTERNS:
            for m in p.finditer(date):
                seen_spans.add((m.start(), m.end()))
        if len(seen_spans) > 1:
            merged_count += 1
    # If more than 30% of rows have multiple dates, extraction is broken
    return merged_count > len(transactions) * 0.3


def _extraction_quality_score(transactions: list[dict]) -> float:
    """Score extraction quality as a weighted transaction count.

    Returns a float that naturally ranks strategies:
      - More transactions with financial data score higher
      - Transactions missing both debit and credit are penalised (worth 0.1)
      - Having a balance value adds a small bonus per row (0.3)
      - Descriptions of decent length add a small bonus per row (up to 0.2)

    The description-length bonus prevents a strategy that extracts the same row
    count with truncated descriptions from beating one with full descriptions.
    """
    if not transactions:
        return 0.0
    score = 0.0
    for t in transactions:
        has_amount = bool(t.get("debit") or t.get("credit"))
        has_balance = bool(t.get("balance"))
        row_score = (0.7 if has_amount else 0.1) + (0.3 if has_balance else 0.0)
        desc_len = len(str(t.get("description", "") or "").strip())
        if desc_len >= 40:
            row_score += 0.2
        elif desc_len >= 20:
            row_score += 0.1
        score += row_score
    return score


def _normalize_merge_date_key(date_str: str) -> str:
    """Normalize dates for cross-strategy matching.

    This intentionally ignores year precision when the same row appears as
    `01-07-20` in one strategy and `01-07-2025` in another.
    """
    date_str = str(date_str or "").strip()
    m = re.match(r"^(\d{1,2}[/\-.]\d{1,2})(?:[/\-.]\d{2,4})?$", date_str)
    if m:
        return m.group(1)
    return date_str


def _normalize_merge_description(desc: str) -> str:
    """Normalize descriptions for cross-strategy matching."""
    desc = re.sub(r"\s+", " ", str(desc or "")).strip().lower()
    # Some layouts leak the second half of the year into the description:
    # `25 Merchant Payment...` instead of `...2025` in the date column.
    desc = re.sub(r"^\d{2}\s+", "", desc)
    return desc


def _txn_merge_key(txn: dict) -> tuple:
    """Key for enriching the same transaction across strategies.

    Uses the normalized date, a description prefix, and explicit debit/credit
    amounts. Balance is excluded so a stronger variant can fill it in later.
    """
    desc = _normalize_merge_description(txn.get("description", ""))
    return (
        _normalize_merge_date_key(txn.get("date", "")),
        desc[:60],
        str(txn.get("debit", "") or "").strip(),
        str(txn.get("credit", "") or "").strip(),
    )


def _enrich_transactions(base: list[dict], candidates: list[dict]) -> int:
    """Fill missing fields in base rows from equivalent candidate rows.

    The primary use-case is when the chosen strategy extracted the transaction
    row but missed the running balance, while another strategy found it.
    """
    if not base or not candidates:
        return 0

    mergeable: dict[tuple, list[dict]] = defaultdict(list)
    loose_mergeable: dict[tuple, list[dict]] = defaultdict(list)
    for row in base:
        strict_key = _txn_merge_key(row)
        mergeable[strict_key].append(row)
        loose_key = (
            _normalize_merge_date_key(row.get("date", "")),
            str(row.get("debit", "") or "").strip(),
            str(row.get("credit", "") or "").strip(),
        )
        loose_mergeable[loose_key].append(row)

    updates = 0
    for cand in candidates:
        matches = mergeable.get(_txn_merge_key(cand), [])
        if len(matches) == 1:
            row = matches[0]
        else:
            loose_key = (
                _normalize_merge_date_key(cand.get("date", "")),
                str(cand.get("debit", "") or "").strip(),
                str(cand.get("credit", "") or "").strip(),
            )
            loose_matches = loose_mergeable.get(loose_key, [])
            cand_desc = _normalize_merge_description(cand.get("description", ""))
            narrowed = [
                row for row in loose_matches
                if (
                    _normalize_merge_description(row.get("description", "")) in cand_desc
                    or cand_desc in _normalize_merge_description(row.get("description", ""))
                )
            ]
            if len(narrowed) != 1:
                continue
            row = narrowed[0]

        changed = False
        if not row.get("balance") and cand.get("balance"):
            row["balance"] = cand["balance"]
            changed = True
        if (
            len(str(cand.get("description", "") or "")) >
            len(str(row.get("description", "") or ""))
        ):
            row["description"] = cand["description"]
            changed = True
        if (
            len(str(cand.get("date", "") or "")) >
            len(str(row.get("date", "") or ""))
        ):
            row["date"] = cand["date"]
            changed = True

        if changed:
            updates += 1

    return updates


def _fill_page_gaps(
    winner: list[dict], loser: list[dict], winner_source: str, loser_source: str
) -> list[dict]:
    """Supplement the winner strategy with transactions from the loser on missed pages.

    Some pages fail with one strategy but succeed with the other.  This function
    detects date ranges where the winner has NO transactions and injects the
    loser's transactions for those ranges, then re-sorts by date.

    Only injects when the loser's transactions for a gap have meaningful financial
    data (at least 50% have debit or credit).
    """
    if not loser:
        return winner

    # Build a set of dates already in winner (normalised to string)
    winner_dates: set[str] = {t.get("date", "") for t in winner}

    # Find loser transactions whose dates don't appear at all in winner
    gaps: list[dict] = []
    for t in loser:
        d = t.get("date", "")
        if d and d not in winner_dates:
            gaps.append(t)

    if not gaps:
        return winner

    # Only inject if at least 50% of gap rows have financial amounts
    # (protects against injecting header rows or garbage)
    gap_with_amounts = sum(1 for t in gaps if t.get("debit") or t.get("credit"))
    if gap_with_amounts < len(gaps) * 0.5:
        logger.info(
            f"Gap fill: {len(gaps)} rows from {loser_source} skipped "
            f"(only {gap_with_amounts}/{len(gaps)} have amounts)"
        )
        return winner

    logger.info(
        f"Gap fill: injecting {len(gaps)} rows from {loser_source} into {winner_source} "
        f"(dates not covered by winner)"
    )
    combined = winner + gaps
    # Re-sort by date string (lexicographic — good enough for same-format dates)
    # Transactions with the same date keep their relative order via stable sort.
    combined.sort(key=lambda t: t.get("date", ""))
    return combined


def _deduplicate_transactions(transactions: list[dict]) -> list[dict]:
    """Remove exact duplicate transactions.

    Two transactions are considered duplicates when all five fields are identical:
    date, description (first 40 chars), debit, credit, and balance.

    Using description in the key prevents false removal of legitimate same-day,
    same-amount transactions with different descriptions (e.g. two salary credits).
    Preserves first occurrence and order.
    """
    seen: set[tuple] = set()
    result: list[dict] = []
    for t in transactions:
        desc_prefix = t.get("description", "")[:40]
        key = (
            t.get("date", ""),
            desc_prefix,
            t.get("debit", ""),
            t.get("credit", ""),
            t.get("balance", ""),
        )
        if key not in seen:
            seen.add(key)
            result.append(t)
        else:
            logger.debug(f"Dedup: removed duplicate transaction: {key}")
    if len(result) < len(transactions):
        logger.info(f"Dedup: removed {len(transactions) - len(result)} duplicate transaction(s)")
    return result


def _extract_from_tables(pdf_path: Path, forced_col_map: dict | None = None) -> list[dict]:
    """Extract transactions from structured PDF tables by mapping columns."""
    transactions = []
    col_map = forced_col_map  # Use template col_map if provided, else detect
    col_map_num_cols = 0  # Number of columns col_map was detected for

    if forced_col_map:
        logger.info(f"pdfplumber using forced col_map from template: {forced_col_map}")

    with pdfplumber.open(pdf_path) as pdf:
        for page_num, page in enumerate(pdf.pages, 1):
            try:
                tables = page.extract_tables()
            except Exception as e:
                # PSKeyword, PDFSyntaxError, and other parsing errors on individual
                # pages must not abort the whole document — skip the page and continue.
                logger.warning(f"Page {page_num}: pdfplumber table extraction error (skipping): {e}")
                continue
            if not tables:
                logger.debug(f"Page {page_num}: no tables found")
                continue

            logger.debug(f"Page {page_num}: found {len(tables)} table(s), rows per table: {[len(t) for t in tables]}")

            # ── Fallback: if default extraction gives a 1-col table but we
            # already know the PDF has multi-column tables (from a previous
            # page), retry with text-based column detection.  Some PDFs have
            # ruling lines only on page 1; subsequent pages rely on text
            # alignment, which the default "lines" strategy cannot detect.
            if (
                col_map is not None
                and col_map_num_cols > 1
                and all(len(t[0]) <= 1 for t in tables if t)
            ):
                logger.info(
                    f"Page {page_num}: default extraction gave 1-col table(s) "
                    f"but expected {col_map_num_cols} cols, retrying with text strategy"
                )
                text_tables = page.extract_tables({
                    "vertical_strategy": "text",
                    "horizontal_strategy": "text",
                })
                if text_tables and any(len(t[0]) > 1 for t in text_tables if t):
                    tables = text_tables
                    logger.info(
                        f"Page {page_num}: text strategy found {len(tables)} table(s) "
                        f"with {len(tables[0][0]) if tables[0] else 0} cols"
                    )

            # Track where this page's transactions start so we can supplement
            # with a word scan after all tables are processed.
            page_start_idx = len(transactions)

            for ti, table in enumerate(tables):
                cleaned = _clean_table(table)
                if not cleaned:
                    logger.debug(f"Page {page_num}: table {ti} empty after cleaning")
                    continue

                num_cols = len(cleaned[0])
                logger.info(f"Page {page_num}, table {ti}: {len(cleaned)} rows, {num_cols} cols, first row: {[c[:40] for c in cleaned[0]]}")
                if len(cleaned) <= 5:
                    for ri, r in enumerate(cleaned):
                        logger.info(f"  Page {page_num}, table {ti}, row {ri}: {[c[:50] for c in r]}")

                # ── Per-page column mapping ────────────────────────────
                # When a page has a different column count than the
                # primary col_map (e.g. last page has 5 cols vs 6),
                # use a PAGE-LOCAL col_map so we don't corrupt the
                # primary mapping for subsequent pages.
                page_col_map = col_map  # default: use the primary
                if col_map is not None and num_cols != col_map_num_cols and (
                    not forced_col_map or num_cols < col_map_num_cols
                ):
                    logger.info(f"Page {page_num}, table {ti}: col_map has {col_map_num_cols} cols but table has {num_cols} cols, re-detecting")
                    new_map = _detect_columns(cleaned, page)
                    if new_map:
                        page_col_map = new_map
                        logger.info(f"Page {page_num}: page-local col_map re-detected: {page_col_map}")
                        if "amount" in page_col_map:
                            amt_idx = page_col_map["amount"]
                            has_explicit_plus = False
                            skip_rows = page_col_map.get("header_row_count", 0)
                            for sample_row in cleaned[skip_rows:skip_rows + 30]:
                                if amt_idx < len(sample_row):
                                    val = sample_row[amt_idx].strip()
                                    if val.startswith("+"):
                                        has_explicit_plus = True
                                        break
                            page_col_map["unsigned_is_debit"] = has_explicit_plus
                    else:
                        # Re-detection failed — try content inference
                        max_col_idx = max(v for k, v in col_map.items() if isinstance(v, int))
                        if num_cols > max_col_idx:
                            logger.info(f"Page {page_num}, table {ti}: re-detection failed but table has enough cols ({num_cols} > max_idx {max_col_idx}), trying existing col_map")
                        else:
                            inferred = _infer_columns_by_content(cleaned)
                            if inferred:
                                page_col_map = inferred
                                logger.info(f"Page {page_num}, table {ti}: content-inferred page-local col_map: {page_col_map}")
                            else:
                                logger.info(f"Page {page_num}, table {ti}: skipping table (re-detection failed, {num_cols} cols < needed {max_col_idx + 1})")
                                continue

                # Detect column mapping once (skip if forced col_map from template)
                if col_map is None:
                    # Need at least 3 rows (header + 2 data) to avoid collapsed tables
                    if len(cleaned) < 3:
                        logger.debug(f"Page {page_num}: skipping table with only {len(cleaned)} rows")
                        continue
                    col_map = _detect_columns(cleaned, page)
                    if not col_map:
                        logger.debug(f"Page {page_num}: could not detect columns")
                        continue
                    col_map_num_cols = num_cols
                    page_col_map = col_map
                    logger.info(f"Column mapping detected on page {page_num}: {col_map}")
                    # Detect sign convention for single "amount" column
                    if "amount" in col_map:
                        amt_idx = col_map["amount"]
                        has_explicit_plus = False
                        skip_rows = col_map.get("header_row_count", 0)
                        for sample_row in cleaned[skip_rows:skip_rows + 30]:
                            if amt_idx < len(sample_row):
                                val = sample_row[amt_idx].strip()
                                if val.startswith("+"):
                                    has_explicit_plus = True
                                    break
                        col_map["unsigned_is_debit"] = has_explicit_plus
                elif forced_col_map and col_map_num_cols == 0:
                    col_map_num_cols = num_cols

                # Process ALL rows using page_col_map (which may differ from
                # the primary col_map on pages with different column counts).
                # _row_to_transaction() validates the date column; header rows
                # are rejected by the date-pattern check and return None.
                #
                # Continuation rows (no date, only description text) are merged
                # into the previous transaction's description.
                effective_map = page_col_map
                page_txn_count = 0
                desc_idx = effective_map.get("description")
                for ri, row in enumerate(cleaned):
                    txn = _row_to_transaction(row, effective_map)
                    if txn:
                        # If description is empty or very short, try word-position recovery
                        if len(txn["description"]) < 3:
                            recovered = _recover_description_from_words(
                                page, ri, None, effective_map, cleaned
                            )
                            if recovered:
                                txn["description"] = recovered
                                logger.debug(f"Page {page_num}: recovered description for row {ri}: {txn['description'][:60]}...")
                        transactions.append(txn)
                        page_txn_count += 1
                        if page_txn_count <= 3:
                            logger.info(f"Page {page_num}, table {ti}, row {ri}: EXTRACTED txn #{len(transactions)}: date={txn['date']}, desc={txn['description'][:50]!r}, debit={txn['debit']}, credit={txn['credit']}, balance={txn['balance']}")
                    else:
                        # Check if this is a continuation row (no date but has description text).
                        # Merge non-date, non-amount text into the previous transaction's description.
                        # Cap at 8 continuation lines to prevent runaway merging of
                        # footer/next-transaction text into a single description.
                        _MAX_CONTINUATION_LINES = 8
                        if transactions:
                            date_idx = effective_map.get("date", 0)
                            date_cell = row[date_idx].strip() if isinstance(date_idx, int) and date_idx < len(row) else ""
                            cur_desc_lines = transactions[-1]["description"].count("\n") + 1 if transactions[-1]["description"] else 0
                            # A row with amounts but no date is a failed-parse transaction,
                            # not a continuation. Merging it would silently drop the row.
                            row_has_amount = _row_has_amount_value(row, effective_map)
                            if row_has_amount:
                                logger.warning(
                                    f"Page {page_num}, table {ti}, row {ri}: row has amount values "
                                    f"but no date — not merging into previous description: "
                                    f"{[c[:40] for c in row]}"
                                )
                            row_is_page_header = _row_looks_like_page_header(row)
                            if row_is_page_header:
                                logger.info(
                                    f"Page {page_num}, table {ti}, row {ri}: row looks like "
                                    f"page-header metadata — not merging into previous "
                                    f"description: {[c[:40] for c in row]}"
                                )
                            if not date_cell and not row_has_amount and not row_is_page_header and cur_desc_lines < _MAX_CONTINUATION_LINES:
                                # Collect text from all non-financial columns
                                amount_cols = {
                                    effective_map.get(k)
                                    for k in ("debit", "credit", "balance", "amount")
                                    if isinstance(effective_map.get(k), int)
                                }
                                amount_cols.add(date_idx)
                                parts = []
                                for ci, cell in enumerate(row):
                                    if ci in amount_cols:
                                        continue
                                    txt = cell.strip()
                                    if (
                                        txt
                                        and len(txt) > 1
                                        and not _is_noise_line(txt)
                                        and not _is_standalone_amount_line(txt)
                                    ):
                                        # IBAN at start of continuation text signals a new
                                        # transaction reference (SWIFT format /GB51NWBK...),
                                        # not a continuation of the current description.
                                        stripped_txt = txt.lstrip("/")
                                        if _IBAN_RE.match(stripped_txt):
                                            parts = []  # discard — belongs to next txn
                                            break
                                        parts.append(txt)
                                if parts:
                                    continuation_text = "\n".join(
                                        _normalize_multiline_text(p) for p in parts if p
                                    )
                                    transactions[-1]["description"] = _clean_description(
                                        _append_description(
                                            transactions[-1]["description"],
                                            continuation_text,
                                        )
                                    )

                        if ri < 3:
                            logger.info(f"Page {page_num}, table {ti}, row {ri}: REJECTED (returned None), row={[c[:40] for c in row]}")
                logger.info(f"Page {page_num}, table {ti}: extracted {page_txn_count} transactions (running total: {len(transactions)})")

            # Supplement: scan page words for any transaction rows that pdfplumber's
            # table bbox missed (most commonly the first data row of a continuation
            # page when a full-width banner sits above the table's detected boundary).
            if col_map is not None:
                page_txns = transactions[page_start_idx:]
                missed = _find_missed_rows_word_scan(page, page_txns, col_map)
                if missed:
                    for i, m in enumerate(missed):
                        transactions.insert(page_start_idx + i, m)
                    logger.info(
                        f"Page {page_num}: word scan recovered {len(missed)} missed row(s): "
                        f"{[m['date'] + ' ' + m['description'][:30] for m in missed]}"
                    )

    # ── Second-pass fallback: if default strategy found nothing, retry all
    # pages with text-based column detection.  Some PDFs (e.g. Wio Bank)
    # have zero ruling lines — every page returns a single-column table
    # under the default "lines" strategy, so col_map is never detected.
    # The text strategy uses character positions to infer column boundaries.
    if not transactions and not forced_col_map:
        logger.info("Default strategy found 0 transactions, retrying all pages with text strategy")

        # Step 1: Find a page where we can detect column headers and build
        # explicit vertical lines from header word x-positions.  This avoids
        # the over-segmentation problem where the text strategy creates too
        # many columns on pages with mixed layouts (headers, account info, etc.).
        explicit_lines: list[float] | None = None
        header_page_width: float = 0

        best_score = 0
        best_roles: list = []
        best_words: list = []
        best_page_width: float = 0

        with pdfplumber.open(pdf_path) as pdf:
            for page in pdf.pages:
                words = page.extract_words()
                if not words:
                    continue
                # Group words by y-position to find header lines.
                # Merge lines within 5px of each other (some PDFs have
                # headers like "Fees" at y=263 and "Date" at y=266).
                lines_by_top: dict[int, list] = defaultdict(list)
                for w in words:
                    lines_by_top[round(w["top"])].append(w)

                # Merge nearby y-lines into bands
                sorted_tops = sorted(lines_by_top.keys())
                merged_lines: list[list] = []
                for top in sorted_tops:
                    if merged_lines and abs(top - merged_lines[-1][0][0]) <= 5:
                        # Merge with previous band (use min top as reference)
                        merged_lines[-1].extend(
                            (top, w) for w in lines_by_top[top]
                        )
                    else:
                        merged_lines.append(
                            [(top, w) for w in lines_by_top[top]]
                        )

                for band in merged_lines:
                    all_words_in_band = [w for _, w in band]
                    sorted_lw = sorted(all_words_in_band, key=lambda w: w["x0"])
                    score, col_roles = _score_header_line(sorted_lw)
                    if score <= best_score:
                        continue
                    # Prefer lines with diverse roles (date+desc+amount)
                    # over lines with duplicate roles (balance+balance)
                    unique_roles = {r for r, _ in col_roles if r not in ("skip", "unknown")}
                    has_date = "date" in unique_roles
                    has_amount = bool(unique_roles & {"debit", "credit", "balance", "amount"})
                    if has_date and has_amount:
                        best_score = score
                        best_roles = col_roles
                        best_words = sorted_lw
                        best_page_width = page.width

        if best_score >= 2 and best_words:
            # Use ALL header word positions as column boundaries,
            # not just the classified ones — unrecognized columns
            # like "Fees" still need boundaries for proper separation.
            # Group nearby words (within 15px) as one column.
            # Shift each boundary 3pt left so text starting at the
            # boundary is included in the column (pdfplumber is strict
            # about left-edge alignment).
            x_positions = []
            for w in best_words:
                x0 = max(0, w["x0"] - 3)
                if not x_positions or abs(x0 - x_positions[-1]) > 15:
                    x_positions.append(x0)
            x_positions.sort()
            x_positions.append(best_page_width)
            explicit_lines = x_positions
            header_page_width = best_page_width
            logger.info(
                f"Text strategy: best header line score={best_score}, "
                f"explicit_lines={[round(x, 1) for x in explicit_lines]}"
            )

        # Step 2: Two-pass approach.
        # Pass 1: Find col_map from the best page (one with recognizable headers).
        #         Pages with mixed content (account info + data) may fail detection.
        # Pass 2: Extract transactions from ALL pages using the detected col_map.
        col_map = None
        col_map_num_cols = 0

        # NOTE: We intentionally do NOT use pdfplumber's "explicit" vertical
        # strategy here even when explicit_lines is available.  In practice,
        # pdfplumber silently drops the leftmost and rightmost column when the
        # explicit boundaries sit close to the page edge, producing fewer
        # columns than expected and a wrong col_map.  The text strategy is
        # robust enough on its own; the wrong-col_map problem (e.g. Wio Bank
        # cover page) is handled by date-content validation in pass 1 below.
        _page_table_settings = {
            "vertical_strategy": "text",
            "horizontal_strategy": "text",
        }

        with pdfplumber.open(pdf_path) as pdf:
            # ── Pass 1: detect col_map ────────────────────────────────────
            for page_num, page in enumerate(pdf.pages, 1):
                text_tables = page.extract_tables(_page_table_settings)
                if not text_tables:
                    continue

                for table in text_tables:
                    cleaned = _clean_table(table)
                    if not cleaned or len(cleaned) < 3:
                        continue
                    candidate = _detect_columns(cleaned, page)
                    if candidate:
                        has_financial = any(
                            k in candidate for k in ("debit", "credit", "balance", "amount")
                        )
                        if not has_financial:
                            continue
                        # Require at least one data row with date-like content
                        # in the date column.  This rejects col_maps from cover /
                        # summary pages whose "Date" header column actually holds
                        # non-date text (e.g. Wio Bank cover page, index 3).
                        if "date" in candidate:
                            date_col = candidate["date"]
                            # Date column must precede description column.
                            # If date appears AFTER description in the table
                            # (e.g. Wio Bank cover page: date=3, desc=0),
                            # the col_map is from a summary/cover page — skip.
                            desc_col = candidate.get("description")
                            if desc_col is not None and date_col > desc_col:
                                logger.debug(
                                    f"Text strategy pass 1: rejected col_map on page "
                                    f"{page_num} — date col {date_col} > desc col {desc_col}"
                                )
                                continue
                            has_valid_date = any(
                                date_col < len(row)
                                and row[date_col]
                                and DATE_START_RE.match(str(row[date_col]).strip())
                                for row in cleaned[1:]
                            )
                            if not has_valid_date:
                                logger.debug(
                                    f"Text strategy pass 1: rejected col_map on page "
                                    f"{page_num} — date col {date_col} has no date values"
                                )
                                continue
                        col_map = candidate
                        col_map_num_cols = len(cleaned[0])
                        logger.info(
                            f"Text strategy pass 1: col_map detected on page {page_num} "
                            f"({col_map_num_cols} cols): {col_map}"
                        )
                        break
                if col_map:
                    break

            if not col_map:
                logger.info("Text strategy pass 1: no col_map detected on any page")

            # ── Pass 2: extract transactions from all pages ───────────────
            if col_map:
                for page_num, page in enumerate(pdf.pages, 1):
                    text_tables = page.extract_tables(_page_table_settings)
                    if not text_tables:
                        continue

                    multi_col_tables = [t for t in text_tables if t and len(t[0]) > 1]
                    if not multi_col_tables:
                        continue

                    for ti, table in enumerate(multi_col_tables):
                        cleaned = _clean_table(table)
                        if not cleaned:
                            continue

                        num_cols = len(cleaned[0])

                        # If column count differs, try re-detecting for this page
                        page_col_map = col_map
                        if num_cols != col_map_num_cols:
                            new_map = _detect_columns(cleaned, page)
                            if new_map:
                                page_col_map = new_map
                            else:
                                # Can't detect — still try with original col_map
                                # if the table has enough columns
                                max_idx = max(v for k, v in col_map.items() if isinstance(v, int))
                                if num_cols <= max_idx:
                                    continue

                        for row in cleaned:
                            txn = _row_to_transaction(row, page_col_map)
                            if txn:
                                transactions.append(txn)

        # ── Pre-pass-3 validation: check pass 2 results quality ─────────
        # If most transactions from pass 2 have no debit/credit (amounts
        # only in balance column or missing), the column mapping is wrong.
        # Discard and let Strategy 2/3 handle it.
        if transactions and col_map:
            no_dc = sum(1 for t in transactions if not t.get("debit") and not t.get("credit"))
            if len(transactions) > 0 and no_dc / len(transactions) > 0.7:
                logger.warning(
                    f"Text strategy pass 2: {no_dc}/{len(transactions)} transactions "
                    f"have no debit/credit — discarding results"
                )
                transactions = []
                col_map = None

        # ── Pass 3: Recover transactions from pages that the text strategy
        # couldn't parse due to mixed content (account info + data).
        # Detect which pages were missed by checking if their date-containing
        # rows produced any transactions, then retry those pages using word-
        # position extraction with the known col_map's column roles.
        if transactions and col_map:
            captured_dates = {(t["date"], t["description"][:20]) for t in transactions}
            with pdfplumber.open(pdf_path) as pdf:
                for page_num, page in enumerate(pdf.pages, 1):
                    # Quick check: does this page have date-like text?
                    text = page.extract_text()
                    if not text:
                        continue
                    page_dates = set()
                    for line in text.split("\n"):
                        line = line.strip()
                        if DATE_START_RE.match(line):
                            for p in DATE_PATTERNS:
                                dm = p.match(line)
                                if dm:
                                    page_dates.add(dm.group(1))
                                    break
                    if not page_dates:
                        continue

                    # Check if any of this page's dates are NOT in captured transactions
                    page_txn_count = sum(
                        1 for d, desc in captured_dates if d in page_dates
                    )
                    if page_txn_count >= len(page_dates) * 0.5:
                        continue  # Most dates from this page are already captured

                    # This page was likely missed — try word-position recovery
                    logger.info(
                        f"Text strategy pass 3: page {page_num} appears to have "
                        f"uncaptured transactions ({len(page_dates)} dates, "
                        f"{page_txn_count} captured), attempting word-position recovery"
                    )
                    # Use _extract_from_text strategy for just this page's text
                    prev_balance = None
                    for line in text.split("\n"):
                        line = line.strip()
                        if not line or _is_noise_line(line):
                            continue
                        txn = _parse_text_line(line, prev_balance)
                        if txn:
                            key = (txn["date"], txn["description"][:20])
                            if key not in captured_dates:
                                transactions.append(txn)
                                captured_dates.add(key)
                                if txn["balance"]:
                                    try:
                                        prev_balance = float(
                                            txn["balance"].replace(",", "")
                                        )
                                    except ValueError:
                                        pass

        if transactions and col_map:
            # Validate: financial columns (debit/credit) must actually contain
            # monetary values (numbers with decimals).  If they look like plain
            # integers (e.g. reference numbers), the column mapping is wrong
            # and the results are unreliable — discard and let Strategy 2/3
            # handle it.
            discard = False
            for fin_key in ("debit", "credit"):
                if fin_key not in col_map or not isinstance(col_map[fin_key], int):
                    continue
                # Check if the majority of values in this column are plain integers
                col_idx = col_map[fin_key]
                plain_int_count = 0
                value_count = 0
                for txn in transactions[:20]:
                    val = txn.get(fin_key, "").strip()
                    if not val:
                        continue
                    value_count += 1
                    cleaned_val = val.replace(",", "").replace("-", "")
                    if cleaned_val.isdigit() and len(cleaned_val) >= 5:
                        # 5+ digit integer with no decimal → likely a ref number
                        plain_int_count += 1
                if value_count > 0 and plain_int_count / value_count > 0.5:
                    logger.warning(
                        f"Text strategy fallback: '{fin_key}' column appears to contain "
                        f"reference numbers, not monetary amounts ({plain_int_count}/{value_count} "
                        f"are 5+ digit integers) — discarding results"
                    )
                    discard = True
                    break

            # Also check: if most transactions have NO debit/credit
            # (all amounts only in balance column), the columns are misaligned.
            if not discard:
                no_debit_credit = 0
                sample = transactions[:30]
                for txn in sample:
                    if not txn.get("debit") and not txn.get("credit"):
                        no_debit_credit += 1
                if len(sample) > 0 and no_debit_credit / len(sample) > 0.7:
                    logger.warning(
                        f"Text strategy fallback: {no_debit_credit}/{len(sample)} "
                        f"transactions have no debit/credit — discarding results"
                    )
                    discard = True

            # Also check: if a balance column is mapped but most transactions
            # still lack a balance value, the table is over-split (text
            # strategy created too many sub-columns) — discard.
            if not discard and "balance" in col_map:
                no_bal = sum(1 for t in transactions if not t.get("balance"))
                if len(transactions) > 0 and no_bal / len(transactions) > 0.2:
                    logger.warning(
                        f"Text strategy fallback: {no_bal}/{len(transactions)} "
                        f"transactions lack balance despite balance column — "
                        f"discarding (over-split table)"
                    )
                    discard = True

            # Also check: if most "date" values contain extra text beyond
            # the date (e.g. "02/01/2024 27155127"), the table columns are
            # misaligned (date+ref merged into one column).
            if not discard:
                bad_date_count = 0
                date_sample = 0
                for txn in transactions[:30]:
                    d = txn.get("date", "").strip()
                    if d:
                        date_sample += 1
                        # A clean date should be <= 12 chars (e.g. "31/12/2024")
                        if len(d) > 12:
                            bad_date_count += 1
                if date_sample > 0 and bad_date_count / date_sample > 0.5:
                    logger.warning(
                        f"Text strategy fallback: date column contains extra text "
                        f"({bad_date_count}/{date_sample} dates > 12 chars) — "
                        f"discarding results"
                    )
                    discard = True

            if not discard:
                logger.info(f"Text strategy fallback: {len(transactions)} transactions extracted")
            else:
                transactions = []
                col_map = None

    logger.info(f"Strategy 1 (_extract_from_tables): total {len(transactions)} transactions extracted")
    return transactions, col_map


def _find_missed_rows_word_scan(page, captured_txns: list[dict], col_map: dict) -> list[dict]:
    """Find transaction rows visible on the page that were not captured by table extraction.

    pdfplumber's table bbox detection sometimes excludes the first data row of a
    continuation page because a full-width page banner (e.g. "Transactions Details
    for the period…") sits between the column-header line and the first data row,
    causing pdfplumber to start its table boundary below that row.

    This function scans the page's extracted words for date-starting lines, extracts
    their field values by x-coordinate, and returns any that are not already present
    in captured_txns (matched by date + balance).
    """
    try:
        words = page.extract_words()
        if not words:
            return []

        # Detect column x-positions from the page's header words.
        col_boundaries = _detect_word_columns(words)
        if not col_boundaries:
            return []

        # Build a lookup of already-captured (date, balance) pairs so we can skip
        # rows that the table extraction already got.  When balance is absent we
        # fall back to (date, debit, credit) to avoid false collisions.
        def _key(t: dict) -> tuple:
            return (t["date"], t["balance"]) if t["balance"] else (t["date"], t["debit"], t["credit"])

        captured_keys = {_key(t) for t in captured_txns}

        # Group words by y-line.
        lines_by_top: dict = defaultdict(list)
        for w in words:
            lines_by_top[round(w["top"])].append(w)

        missed: list[dict] = []
        processed_tops: set[int] = set()
        # Multi-line description recovery: when a date-row has continuation lines
        # below it (descriptions that span multiple visual lines in the PDF), the
        # following non-date bands carry the rest of that description. Append them
        # to the prior date-row's description until we hit either the next date-row
        # or _MAX_CONTINUATION_Y pixels below the date-row.
        _MAX_CONTINUATION_Y = 60
        last_candidate: dict | None = None
        last_candidate_top: int | None = None

        for top in sorted(lines_by_top.keys()):
            if top in processed_tops:
                continue

            # Merge words from y-lines within ±5 px of this top.
            # In many PDFs the date/amount words and the description words within
            # the same table row render at slightly different vertical positions
            # (1–4 px apart).  Without this band-merge the description text ends
            # up in a separate y-bucket from the date and is never seen.
            y_tol = 5
            band_words: list[dict] = []
            for y, wds in lines_by_top.items():
                if abs(y - top) <= y_tol:
                    band_words.extend(wds)
                    processed_tops.add(y)

            line_words = sorted(band_words, key=lambda w: w["x0"])
            if not line_words:
                continue

            first_text = line_words[0]["text"]
            is_date_row = any(p.match(first_text) for p in DATE_PATTERNS)

            if not is_date_row:
                # Continuation row: append description-zone words to the prior
                # date-row's description, but only while we're still within the
                # vertical proximity of that row. Once we drift further than
                # _MAX_CONTINUATION_Y we assume the prior row has ended.
                if last_candidate is None or last_candidate_top is None:
                    continue
                if top - last_candidate_top > _MAX_CONTINUATION_Y:
                    last_candidate = None
                    last_candidate_top = None
                    continue
                # Stop continuation when the band looks like a footer/summary
                # line (e.g. "Opening balance :", "Total debit amount :",
                # "Credit count :") — those are not transaction text.
                band_text_lower = " ".join(w["text"] for w in line_words).lower()
                if any(phrase in band_text_lower for phrase in SKIP_PHRASES):
                    last_candidate = None
                    last_candidate_top = None
                    continue
                cont_parts: list[str] = []
                for w in line_words:
                    x = w["x0"]
                    text = w["text"]
                    if AMOUNT_RE.match(text.replace(",", "").lstrip("+-")):
                        continue
                    if text.rstrip(".").upper() in ("CR", "DR"):
                        continue
                    if x >= col_boundaries["balance_x"] - 15:
                        continue
                    cont_parts.append(text)
                if cont_parts:
                    extra = " ".join(cont_parts)
                    extra = re.sub(r"\b\d{1,2}[/\-.]\d{1,2}[/\-.]\d{4}\b", "", extra)
                    extra = re.sub(r"\s+", " ", extra).strip()
                    if extra:
                        prev = last_candidate["description"]
                        last_candidate["description"] = (prev + "\n" + extra).strip() if prev else extra
                continue

            # Extract field values by x-position (same logic as _extract_from_words).
            date = first_text
            description_parts: list[str] = []
            debit = ""
            credit = ""
            balance = ""
            amount_raw = ""

            for w in line_words[1:]:
                x = w["x0"]
                text = w["text"]

                if text.rstrip(".").upper() in ("CR", "DR"):
                    continue

                is_number = bool(AMOUNT_RE.match(text.replace(",", "")))

                if x >= col_boundaries["balance_x"] - 15:
                    balance = text
                elif col_boundaries.get("credit_x") and x >= col_boundaries["credit_x"] - 15:
                    if is_number:
                        credit = text
                    else:
                        description_parts.append(text)
                elif col_boundaries.get("debit_x") and x >= col_boundaries["debit_x"] - 15:
                    if is_number:
                        debit = text
                    else:
                        description_parts.append(text)
                elif col_boundaries.get("amount_x") and x >= col_boundaries["amount_x"] - 15:
                    if is_number:
                        amount_raw = text
                    else:
                        description_parts.append(text)
                else:
                    description_parts.append(text)

            if amount_raw and not debit and not credit:
                debit, credit = _split_signed_amount(amount_raw, unsigned_is_debit=False)

            balance = _clean_amount(balance)
            debit = _clean_amount(debit)
            credit = _clean_amount(credit)

            if not balance and not debit and not credit:
                continue

            # Skip summary rows.
            row_text_lower = (date + " " + " ".join(description_parts)).lower()
            if any(phrase in row_text_lower for phrase in SKIP_PHRASES):
                continue

            # Build description: strip any date tokens that leaked in from adjacent
            # columns (e.g. a "Value Date" column whose x falls in the desc zone).
            desc = " ".join(description_parts)
            desc = re.sub(r"\b\d{1,2}[/\-.]\d{1,2}[/\-.]\d{4}\b", "", desc)
            desc = re.sub(r"\s+", " ", desc).strip()
            # Strip a leading reference/transaction code (the Reference No column
            # often falls inside the description x-zone, e.g. Banque Misr's
            # "803C002AED000001 Credit Interest" → "Credit Interest").
            desc = _strip_leading_ref(desc)

            candidate = {"date": date, "description": desc,
                         "debit": debit, "credit": credit, "balance": balance}

            # Skip if already captured.
            if _key(candidate) in captured_keys:
                last_candidate = None
                last_candidate_top = None
                continue

            missed.append(candidate)
            captured_keys.add(_key(candidate))  # Avoid duplicating within this scan
            last_candidate = candidate
            last_candidate_top = top

        # Final pass: clean up descriptions after continuation lines have been
        # appended. _clean_description handles footer markers, header bleed, and
        # multi-line noise lines.
        for txn in missed:
            txn["description"] = _clean_description(txn["description"])

        return missed

    except Exception as e:
        logger.debug(f"Word scan for missed rows failed: {e}")
        return []


def _extract_from_pymupdf(pdf_path: Path, forced_col_map: dict | None = None) -> list[dict]:
    """Extract transactions using PyMuPDF's visual table finder.

    PyMuPDF detects table row boundaries from the PDF's ruling lines and
    graphical elements rather than from text-stream coordinates.  This means
    it correctly captures the first data row on every page — a row that
    pdfplumber sometimes clips because it sits close to the page-header band
    and falls just outside pdfplumber's inferred table bounding-box.

    The result is compared against pdfplumber's result in extract_transactions();
    whichever strategy finds more valid rows is used.

    If forced_col_map is provided (from a saved template), column detection
    is skipped and the saved mapping is used directly.
    """
    try:
        import fitz  # PyMuPDF  # noqa: PLC0415
    except ImportError:
        logger.debug("PyMuPDF not installed — skipping (pip install pymupdf)")
        return [], None

    transactions: list[dict] = []
    col_map: dict | None = forced_col_map
    col_map_num_cols = 0

    try:
        doc = fitz.open(str(pdf_path))
    except Exception as e:
        logger.warning(f"PyMuPDF could not open PDF: {e}")
        return [], None

    if forced_col_map:
        logger.info(f"PyMuPDF using forced col_map from template: {forced_col_map}")

    try:
        for page_num, page in enumerate(doc, 1):
            try:
                tabs = page.find_tables()
            except AttributeError:
                logger.warning(
                    "PyMuPDF >= 1.23.0 required for find_tables(). "
                    "Upgrade: pip install --upgrade pymupdf"
                )
                return [], None
            except Exception as e:
                logger.warning(f"PyMuPDF page {page_num}: find_tables() error (skipping): {e}")
                continue

            if not tabs.tables:
                logger.debug(f"PyMuPDF page {page_num}: no tables found")
                continue

            logger.debug(f"PyMuPDF page {page_num}: {len(tabs.tables)} table(s)")

            for ti, tab in enumerate(tabs.tables):
                raw = tab.extract()  # list[list[str | None]]
                cleaned = _clean_table(raw)
                if not cleaned:
                    continue

                num_cols = len(cleaned[0])
                logger.info(
                    f"PyMuPDF page {page_num}, table {ti}: "
                    f"{len(cleaned)} rows × {num_cols} cols, "
                    f"first row: {[c[:40] for c in cleaned[0]]}"
                )

                # Re-detect columns when column count changes between pages
                # (skip re-detection if using a forced col_map from template)
                if col_map is not None and num_cols != col_map_num_cols and not forced_col_map:
                    new_map = _detect_columns(cleaned, None)
                    if new_map:
                        col_map = new_map
                        col_map_num_cols = num_cols
                        logger.info(f"PyMuPDF column mapping re-detected on page {page_num}: {col_map}")
                        if "amount" in col_map:
                            amt_idx = col_map["amount"]
                            skip = col_map.get("header_row_count", 0)
                            col_map["unsigned_is_debit"] = any(
                                cleaned[r][amt_idx].strip().startswith("+")
                                for r in range(skip, min(skip + 30, len(cleaned)))
                                if amt_idx < len(cleaned[r])
                            )

                # First-time column detection (skip if forced col_map)
                if col_map is None:
                    if len(cleaned) < 3:
                        logger.debug(
                            f"PyMuPDF page {page_num}, table {ti}: "
                            f"too few rows ({len(cleaned)}), skipping"
                        )
                        continue
                    col_map = _detect_columns(cleaned, None)
                    if not col_map:
                        logger.debug(
                            f"PyMuPDF page {page_num}, table {ti}: column detection failed"
                        )
                        continue
                    col_map_num_cols = num_cols
                    logger.info(f"PyMuPDF column mapping detected on page {page_num}: {col_map}")
                    if "amount" in col_map:
                        amt_idx = col_map["amount"]
                        skip = col_map.get("header_row_count", 0)
                        col_map["unsigned_is_debit"] = any(
                            cleaned[r][amt_idx].strip().startswith("+")
                            for r in range(skip, min(skip + 30, len(cleaned)))
                            if amt_idx < len(cleaned[r])
                        )
                elif forced_col_map and col_map_num_cols == 0:
                    col_map_num_cols = num_cols

                page_txn_count = 0
                desc_idx = col_map.get("description")
                for ri, row in enumerate(cleaned):
                    txn = _row_to_transaction(row, col_map)
                    if txn:
                        transactions.append(txn)
                        page_txn_count += 1
                        if page_txn_count <= 3:
                            logger.info(
                                f"PyMuPDF page {page_num}, table {ti}, row {ri}: "
                                f"txn #{len(transactions)}: date={txn['date']}, "
                                f"desc={txn['description'][:50]!r}, "
                                f"debit={txn['debit']}, credit={txn['credit']}, "
                                f"balance={txn['balance']}"
                            )
                    else:
                        # Continuation rows in PyMuPDF tables behave the same way as
                        # pdfplumber: no date, no financial values, but text that
                        # belongs to the previous transaction's description.
                        _MAX_CONTINUATION_LINES = 8
                        if transactions:
                            date_idx = col_map.get("date", 0)
                            date_cell = row[date_idx].strip() if isinstance(date_idx, int) and date_idx < len(row) else ""
                            cur_desc_lines = transactions[-1]["description"].count("\n") + 1 if transactions[-1]["description"] else 0
                            # A row with amounts but no date is a failed-parse transaction,
                            # not a continuation. Merging it would silently drop the row.
                            row_has_amount = _row_has_amount_value(row, col_map)
                            if row_has_amount:
                                logger.warning(
                                    f"PyMuPDF page {page_num}, table {ti}, row {ri}: row has "
                                    f"amount values but no date — not merging into previous "
                                    f"description: {[c[:40] for c in row]}"
                                )
                            row_is_page_header = _row_looks_like_page_header(row)
                            if row_is_page_header:
                                logger.info(
                                    f"PyMuPDF page {page_num}, table {ti}, row {ri}: row looks "
                                    f"like page-header metadata — not merging into previous "
                                    f"description: {[c[:40] for c in row]}"
                                )
                            if not date_cell and not row_has_amount and not row_is_page_header and cur_desc_lines < _MAX_CONTINUATION_LINES:
                                amount_cols = {
                                    col_map.get(k)
                                    for k in ("debit", "credit", "balance", "amount")
                                    if isinstance(col_map.get(k), int)
                                }
                                amount_cols.add(date_idx)
                                parts = []
                                for ci, cell in enumerate(row):
                                    if ci in amount_cols:
                                        continue
                                    txt = cell.strip()
                                    if (
                                        txt
                                        and len(txt) > 1
                                        and not _is_noise_line(txt)
                                        and not _is_standalone_amount_line(txt)
                                    ):
                                        stripped_txt = txt.lstrip("/")
                                        if _IBAN_RE.match(stripped_txt):
                                            parts = []
                                            break
                                        parts.append(txt)
                                if parts:
                                    continuation_text = "\n".join(
                                        _normalize_multiline_text(p) for p in parts if p
                                    )
                                    transactions[-1]["description"] = _clean_description(
                                        _append_description(
                                            transactions[-1]["description"],
                                            continuation_text,
                                        )
                                    )
                        if ri < 3:
                            logger.info(
                                f"PyMuPDF page {page_num}, table {ti}, row {ri}: "
                                f"REJECTED: {[c[:40] for c in row]}"
                            )

                logger.info(
                    f"PyMuPDF page {page_num}, table {ti}: "
                    f"{page_txn_count} transactions (running total: {len(transactions)})"
                )
    finally:
        doc.close()

    logger.info(f"PyMuPDF strategy: {len(transactions)} transactions extracted")
    return transactions, col_map


def _detect_columns(table: list[list[str]], page=None) -> dict | None:
    """Detect column mapping by examining header rows or inferring from content."""
    # First try header-based detection from table data
    for header_idx in range(min(3, len(table))):
        row = table[header_idx]
        # Keep Unicode headers so bilingual layouts can be classified.
        raw_headers = [str(cell or "").replace("\n", " ").strip() for cell in row]

        col_map: dict = {}
        assigned: set[str] = set()

        # Two-pass: high-confidence first, then fill gaps with lower-confidence
        for min_conf in (0.85, 0.65):
            for i, raw_h in enumerate(raw_headers):
                if not raw_h:
                    continue
                role, confidence = classify_column_header(raw_h)
                if role in ("unknown", "skip"):
                    continue
                if role not in assigned and confidence >= min_conf:
                    col_map[role] = i
                    assigned.add(role)

        # "amount" is a single column combining debit/credit — still valid
        has_amounts = ("debit" in col_map or "credit" in col_map or "balance" in col_map or "amount" in col_map)
        if "date" in col_map and has_amounts:
            col_map["header_row_count"] = header_idx + 1
            return col_map

    # Second: try detecting headers from page words (headers outside the table)
    if page is not None:
        num_cols = len(table[0]) if table else 0
        col_map = _detect_columns_from_page_words(page, num_cols)
        if col_map:
            return col_map

    # Last resort: infer from content (need enough rows to avoid summary tables)
    if len(table) >= 5:
        return _infer_columns_by_content(table)
    return None


def _detect_columns_from_page_words(page, num_table_cols: int) -> dict | None:
    """Detect column mapping from header words visible on the page.

    When table headers aren't part of the extracted table data, this detects
    header keywords from page words and maps them to table column indices by
    x-coordinate order.  Uses _score_header_line so multi-word headers like
    "Transaction Date" or "Paid Out" are matched as a phrase.
    """
    if num_table_cols < 3:
        return None

    words = page.extract_words()
    if not words:
        return None

    # Group words by y-position
    lines_by_top = defaultdict(list)
    for w in words:
        lines_by_top[round(w["top"])].append(w)

    # Find the line with the highest column-header score
    best_line_words: list | None = None
    best_score = 0
    best_col_roles: list = []

    for line_words in lines_by_top.values():
        sorted_lw = sorted(line_words, key=lambda w: w["x0"])
        score, col_roles = _score_header_line(sorted_lw)
        if score > best_score:
            best_score = score
            best_line_words = sorted_lw
            best_col_roles = col_roles

    if best_score < 2 or best_line_words is None:
        return None

    # Map header roles to table column indices by position order
    col_map: dict = {"header_row_count": 0}
    col_idx = 0
    assigned: set[str] = set()
    for role, _ in best_col_roles:
        if col_idx >= num_table_cols:
            break
        if role not in ("skip", "unknown") and role not in assigned:
            col_map[role] = col_idx
            assigned.add(role)
        col_idx += 1

    if "date" not in col_map:
        return None
    has_amounts = "balance" in col_map or "debit" in col_map or "credit" in col_map or "amount" in col_map
    if not has_amounts:
        return None

    return col_map


def _infer_columns_by_content(table: list[list[str]]) -> dict | None:
    """Infer column roles from cell content when no header is found."""
    if len(table) < 2:
        return None

    num_cols = max(len(row) for row in table)
    if num_cols < 3:
        return None

    sample_rows = table[:30]
    total_sampled = len(sample_rows)

    # Score each column by content type
    date_scores = [0] * num_cols
    amount_scores = [0] * num_cols
    monetary_scores = [0] * num_cols  # amounts with decimals/commas (not plain integers)
    text_scores = [0] * num_cols
    empty_scores = [0] * num_cols

    for row in sample_rows:
        for i in range(num_cols):
            cell = row[i].strip() if i < len(row) else ""
            if not cell:
                empty_scores[i] += 1
                continue
            if any(p.search(cell) for p in DATE_PATTERNS):
                date_scores[i] += 1
            elif FLEX_AMOUNT_RE.search(cell):
                amount_scores[i] += 1
                # Monetary amounts have decimals or commas; plain integers are likely ref numbers
                if "." in cell or "," in cell:
                    monetary_scores[i] += 1
            elif len(cell) > 3:
                text_scores[i] += 1

    # Find the FIRST date column (transaction date, not value date)
    # Threshold: at least 2 dates, and at least 15% of non-empty rows
    # (lower than 30% because continuation rows inflate the empty count)
    date_col = None
    for i in range(num_cols):
        non_empty_rows = total_sampled - empty_scores[i]
        date_pct = date_scores[i] / non_empty_rows if non_empty_rows > 0 else 0
        if date_scores[i] >= 2 and date_pct >= 0.5:
            date_col = i
            break

    if date_col is None:
        return None

    # Find description column (highest text score, not a date column)
    desc_col = None
    best_text = 0
    for i in range(num_cols):
        if i == date_col:
            continue
        if text_scores[i] > best_text:
            best_text = text_scores[i]
            desc_col = i

    if desc_col is None:
        desc_col = 1 if date_col != 1 else 0

    # Amount columns: must be monetary (have decimals/commas), not date/desc
    # Plain integer columns (ref numbers, cheque numbers) are excluded
    amount_cols = []
    for i in range(num_cols):
        if i == date_col or i == desc_col:
            continue
        # Require that at least some values look like monetary amounts (have . or ,)
        # A column of pure integers (ref numbers) will have monetary_scores == 0
        if monetary_scores[i] >= 1:
            amount_cols.append(i)

    # Fallback: if no monetary columns found, use any amount columns
    if not amount_cols:
        for i in range(num_cols):
            if i == date_col or i == desc_col:
                continue
            if amount_scores[i] >= 1:
                amount_cols.append(i)

    if not amount_cols:
        return None

    col_map = {"date": date_col, "description": desc_col, "header_row_count": 0}

    # Filter out secondary date columns from amount_cols
    amount_cols = [i for i in amount_cols if date_scores[i] < amount_scores[i]]

    if len(amount_cols) == 1:
        col_map["balance"] = amount_cols[0]
    elif len(amount_cols) == 2:
        # Could be debit+balance, debit+credit, or amount+balance
        e0 = empty_scores[amount_cols[0]]
        e1 = empty_scores[amount_cols[1]]

        # Check if first col has explicit +/- signs (signed amount column).
        # Mashreq-style PDFs use + for credits and - for debits; checking only
        # for "-" misses statements where all sampled rows are credits ("+").
        has_explicit_sign = False
        for row in sample_rows:
            if amount_cols[0] < len(row):
                val = row[amount_cols[0]].strip()
                if val.startswith("-") or val.startswith("+"):
                    has_explicit_sign = True
                    break

        # Balance signature: running-balance values cluster tightly because
        # they are consecutive account totals — within one statement the
        # max/min ratio is almost always under 50× (often under 10×).
        # Real debit/credit columns mix small fees ($0.02) with large
        # transfers ($250k+), giving a ratio in the thousands or millions.
        # We use a *trimmed* magnitude ratio (median-filter outliers ≥1000×
        # the median to suppress IBAN/reference-number bleed) so a single
        # garbage cell doesn't poison the signal.
        def _trimmed_magnitude_ratio(col_idx: int) -> float:
            vals: list[float] = []
            for row in sample_rows:
                if col_idx >= len(row):
                    continue
                raw = str(row[col_idx] or "").strip()
                if not raw:
                    continue
                cleaned = re.sub(r"[^\d.\-]", "", raw.rstrip("CRDcrd").strip())
                try:
                    v = float(cleaned)
                except ValueError:
                    continue
                if v != 0:
                    vals.append(abs(v))
            if len(vals) < 3:
                return float("inf")
            vals.sort()
            median = vals[len(vals) // 2]
            if median <= 0:
                return float("inf")
            # Drop wild outliers (>1000× off the median) — these are usually
            # IBANs or reference numbers misread as amounts.
            trimmed = [v for v in vals if 0.001 <= v / median <= 1000]
            if len(trimmed) < 3:
                return float("inf")
            mn, mx = min(trimmed), max(trimmed)
            return mx / mn if mn > 0 else float("inf")

        def _looks_like_running_balance(col_idx: int) -> bool:
            # Balance ratio is small AND the column being compared to has
            # a much wider ratio (otherwise we can't tell debit vs balance).
            return _trimmed_magnitude_ratio(col_idx) < 50

        if has_explicit_sign:
            # Signed amount + balance (e.g. Wio Bank, or Sharjah page 10)
            col_map["amount"] = amount_cols[0]
            col_map["balance"] = amount_cols[1]
        elif _looks_like_running_balance(amount_cols[1]):
            # Rightmost is a running balance. Left is an unsigned amount column
            # that combines debit and credit values. Sign is recovered later
            # via balance-delta reconciliation; for now both go into "amount".
            col_map["amount"] = amount_cols[0]
            col_map["balance"] = amount_cols[1]
        elif e0 > total_sampled * 0.3 or e1 > total_sampled * 0.3:
            # Looks like debit/credit (one is often empty)
            col_map["debit"] = amount_cols[0]
            col_map["credit"] = amount_cols[1]
        else:
            col_map["debit"] = amount_cols[0]
            col_map["balance"] = amount_cols[1]
    elif len(amount_cols) >= 3:
        # Typically: debit, credit, balance (last one is balance)
        col_map["debit"] = amount_cols[0]
        col_map["credit"] = amount_cols[1]
        col_map["balance"] = amount_cols[-1]

    return col_map


def _row_to_transaction(row: list[str], col_map: dict) -> dict | None:
    """Convert a table row to a transaction dict using column mapping."""

    def get(key: str) -> str:
        idx = col_map.get(key)
        if idx is not None and idx < len(row):
            return row[idx].strip()
        return ""

    # Any cell > 2000 chars is certainly a header/footer dump, not transaction data
    if any(len(str(c)) > 2000 for c in row):
        logger.debug(f"Row rejected (cell >2000 chars): {[c[:40] for c in row]}")
        return None

    raw_date_cell = get("date")
    if _count_date_matches(raw_date_cell) > 1:
        logger.debug(f"Row rejected (multiple dates in date cell): {raw_date_cell[:120]!r}")
        return None

    for amount_key in ("debit", "credit", "balance", "amount"):
        raw_amount_cell = get(amount_key)
        if _count_amount_matches(raw_amount_cell) > 1:
            logger.debug(
                f"Row rejected (multiple amounts in {amount_key} cell): "
                f"{raw_amount_cell[:120]!r}"
            )
            return None

    # Date check first so we can skip non-transaction rows quickly.
    date = _normalize_split_year_prefix(get("date"))
    date = re.sub(r"\s*([/\-.])\s*", r"\1", date)
    date = re.sub(r"\s+", " ", date).strip()
    # Trim to just the matched date — prevents ref-number bleed when pdfplumber
    # merges adjacent columns (e.g. "01/01/2025 P810554223" → "01/01/2025").
    _date_match = next((p.search(date) for p in DATE_PATTERNS if p.search(date)), None)
    if not _date_match:
        logger.debug(f"Row rejected (no valid date): date_cell={date!r}, row={[c[:30] for c in row]}")
        return None
    date = _date_match.group(1) if _date_match.lastindex else _date_match.group(0)

    # Cross-cell year recovery: when the column boundary splits "2025" so that
    # "01-01-20" is in the date cell and "25" bleeds into the description cell,
    # detect this and move the trailing year digits back to the date.
    _split_year_recovered = False
    _two_digit_year_match = re.match(r"^(\d{1,2})[/\-.](\d{1,2})[/\-.](\d{2})$", date)
    if _two_digit_year_match:
        raw_desc = get("description")
        _desc_year_match = re.match(r"^(\d{2})(?:\s+|$)", raw_desc)
        if _desc_year_match:
            candidate_year = date[-2:] + _desc_year_match.group(1)
            if candidate_year.startswith("20") or candidate_year.startswith("19"):
                sep = date[2] if len(date) > 2 and not date[2].isdigit() else "-"
                date = f"{_two_digit_year_match.group(1)}{sep}{_two_digit_year_match.group(2)}{sep}{candidate_year}"
                _split_year_recovered = True

    # Parse amount cells before skip-phrase checks. This prevents valid first-row
    # transactions (with page-header contamination) from being discarded.
    balance = _clean_amount(get("balance"))
    if "amount" in col_map:
        raw_amount = get("amount")
        unsigned_is_debit = col_map.get("unsigned_is_debit", False)
        debit, credit = _split_signed_amount(raw_amount, unsigned_is_debit)
    else:
        debit = _clean_amount(get("debit"))
        credit = _clean_amount(get("credit"))

    if not debit and not credit and not balance:
        logger.debug(f"Row rejected (no amounts): date={date!r}, row={[c[:30] for c in row]}")
        return None

    # Pre-clean description before row-level filters. The first transaction row on
    # each page often carries merged page-header metadata in this cell.
    description = _normalize_multiline_text(get("description"))
    # If we recovered year digits from the description, strip them
    if _split_year_recovered:
        description = re.sub(r"^\d{2}\s*", "", description, count=1)
    desc_lower = description.lower()
    # Also catch headers that appear after a newline boundary inside a multiline
    # description cell — pdfplumber sometimes merges a page-header line ABOVE
    # the real transaction text into the same cell.
    desc_lines_for_header = description.split("\n")
    header_in_subsequent_line = any(
        any(p.match(ln.lstrip()) for p in _DESC_HEADER_RES)
        for ln in desc_lines_for_header[1:]
    )
    header_was_contaminated = (
        any(p.match(description) for p in _DESC_HEADER_RES)
        or header_in_subsequent_line
        or (
            len(description) > 40
            and (
                bool(_PAGE_META_FIELD_RE.search(description[:220]))
                or any(phrase in desc_lower for phrase in _SOFT_SKIP_PHRASES)
            )
        )
    )
    if header_was_contaminated:
        cleaned_desc = _strip_page_header_prefix(description)
        # Keep row even if description recovery returns empty.
        description = cleaned_desc

    # Skip financial summary rows (totals/opening/closing balance).
    # For contaminated rows, evaluate only non-description cells.
    desc_idx = col_map.get("description")
    if header_was_contaminated:
        scan_row = ["" if i == desc_idx else str(c) for i, c in enumerate(row)]
        row_text = " ".join(scan_row).lower()
        matched_phrase = next((p for p in SKIP_PHRASES if p in row_text), None)
        if matched_phrase:
            logger.debug(f"Row rejected (hard skip, contaminated): phrase={matched_phrase!r}, date={date!r}")
            return None
    else:
        row_text = " ".join(str(c) for c in row).lower()
        matched_phrase = next((p for p in SKIP_PHRASES if p in row_text), None)
        if matched_phrase:
            logger.debug(f"Row rejected (hard skip): phrase={matched_phrase!r}, date={date!r}")
            return None
        # Do not apply soft-skip phrase rejection for date+amount-valid rows.
        # Those phrases are common inside header-contaminated first transactions.

    # Strip leading reference codes from description (safety net for cases where
    # pdfplumber merged the ref-number column into the description column)
    description = _strip_leading_ref(description)

    # Clean merged footer/metadata content from description
    description = _clean_description(description)

    return {
        "date": date,
        "description": description,
        "debit": debit,
        "credit": credit,
        "balance": balance,
    }

def _split_signed_amount(raw: str, unsigned_is_debit: bool = False) -> tuple[str, str]:
    """Split a signed amount into (debit, credit).

    If unsigned_is_debit=True (explicit + signs exist): unsigned = debit, + = credit, - = debit.
    If unsigned_is_debit=False (standard math): unsigned = credit, - = debit.
    """
    if not raw:
        return "", ""
    raw = raw.strip().replace(" ", "")

    # Determine sign
    is_credit = raw.startswith("+")
    is_debit = raw.startswith("-")

    # Strip sign for cleaning
    cleaned = raw.lstrip("+-")
    # Remove non-numeric except comma and dot
    cleaned = re.sub(r"[^\d,.]", "", cleaned)

    if not FLEX_AMOUNT_RE.fullmatch(cleaned):
        return "", ""

    if is_credit:
        return "", cleaned
    elif is_debit:
        return cleaned, ""  # Debit stored as positive; sign is implied by the debit field
    else:
        # No explicit sign — convention-dependent
        if unsigned_is_debit:
            return cleaned, ""
        else:
            return "", cleaned


def _extract_from_words(pdf_path: Path) -> list[dict]:
    """Extract transactions using word positions when tables aren't detected.

    Uses a two-pass approach:
      Pass 1 — scan ALL pages to find the best column header line (highest score).
               This means pages with an account summary before the transaction table
               no longer prevent column detection.
      Pass 2 — extract transactions from ALL pages using the detected col_boundaries.

    Detects column layout from header words (Date, Description, Withdrawal/Debit,
    Deposit/Credit, Balance) and assigns values by x-coordinate.
    """
    # ── Pass 1: find best col_boundaries across all pages ────────────────────
    col_boundaries = None
    best_header_score = 0

    with pdfplumber.open(pdf_path) as pdf:
        for page_num, page in enumerate(pdf.pages):
            try:
                words = page.extract_words()
            except Exception as e:
                logger.warning(f"Words pass 1, page {page_num + 1}: extract_words error: {e}")
                continue
            if not words:
                continue
            candidate = _detect_word_columns(words)
            if candidate is None:
                continue
            # Score this candidate: count columns it covers
            col_count = sum(1 for k in ("debit_x", "credit_x", "balance_x", "amount_x")
                            if candidate.get(k) is not None)
            if col_count > best_header_score:
                best_header_score = col_count
                col_boundaries = candidate
                logger.info(
                    f"Word strategy: best col_boundaries updated from page {page_num + 1} "
                    f"(score={col_count}): {col_boundaries}"
                )

    if col_boundaries is None:
        logger.info("Word strategy: no column headers found on any page")
        return []

    # ── Pass 2: extract transactions using detected col_boundaries ────────────
    transactions = []
    pending_desc_lines = []

    with pdfplumber.open(pdf_path) as pdf:
        for page_num, page in enumerate(pdf.pages):
            try:
                words = page.extract_words()
            except Exception as e:
                logger.warning(f"Words pass 2, page {page_num + 1}: extract_words error: {e}")
                continue
            if not words:
                continue

            # Group words by y-position (same line)
            lines_by_top = defaultdict(list)
            for w in words:
                lines_by_top[round(w['top'])].append(w)

            last_date_y = None  # y-position of the most recent date line
            last_line_y = None  # y-position of the most recently processed line
            for top in sorted(lines_by_top.keys()):
                line_words = sorted(lines_by_top[top], key=lambda w: w['x0'])
                if not line_words:
                    continue

                first_text = line_words[0]['text']
                # Check if this line starts with a date
                if not any(p.match(first_text) for p in DATE_PATTERNS):
                    # Non-date line — accumulate as description
                    line_text = " ".join(w['text'] for w in line_words)
                    if _is_noise_line(line_text):
                        # If this noise line contains column headers, clear pending
                        # (everything accumulated before was page header metadata)
                        line_lower = line_text.lower()
                        header_kws = {"date", "description", "withdrawal", "deposit",
                                      "balance", "narration", "particulars", "debit", "credit"}
                        if len(set(line_lower.split()) & header_kws) >= 3:
                            pending_desc_lines = []
                        continue
                    # Decide: continuation of previous transaction or pending for next?
                    # Use two y-gap checks:
                    #   1. Distance from date line < 25px (overall transaction zone)
                    #   2. Distance from previous line < 15px (inter-line continuity)
                    # The inter-line check catches transaction boundaries where the
                    # gap between rows (e.g. 18.5px in RAKBANK) is larger than the
                    # gap within a row (e.g. 4-9px).
                    inter_line_gap = (top - last_line_y) if last_line_y is not None else 0
                    is_continuation = (
                        transactions
                        and not pending_desc_lines
                        and last_date_y is not None
                        and (top - last_date_y) < 25
                        and inter_line_gap < 15
                    )
                    if is_continuation:
                        if not _is_noise_line(line_text) and not _is_standalone_amount_line(line_text):
                            prev = transactions[-1]
                            prev["description"] = _clean_description(
                                _append_description(prev["description"], line_text)
                            )
                        last_line_y = top
                    else:
                        if not _is_standalone_amount_line(line_text):
                            pending_desc_lines.append(line_text)
                        continue

                # Date line — extract column values by x-position
                last_date_y = top
                last_line_y = top
                date = first_text
                description_parts = []
                debit = ""
                credit = ""
                balance = ""

                amount_raw = ""  # for single "amount" column
                skipped_value_date = False
                for w in line_words[1:]:  # Skip the date word
                    x = w["x0"]
                    text = w["text"]

                    # Skip "Cr." / "Dr." suffixes on balance
                    if text.rstrip(".").upper() in ("CR", "DR"):
                        continue

                    # Skip value-date words (second date that follows the transaction date)
                    if not skipped_value_date and any(p.match(text) for p in DATE_PATTERNS):
                        skipped_value_date = True
                        continue

                    is_number = bool(AMOUNT_RE.match(text.replace(",", "")))

                    if x >= col_boundaries["balance_x"] - 15:
                        balance = text
                    elif col_boundaries.get("credit_x") and x >= col_boundaries["credit_x"] - 15:
                        if is_number:
                            credit = text
                        else:
                            description_parts.append(text)
                    elif col_boundaries.get("debit_x") and x >= col_boundaries["debit_x"] - 15:
                        if is_number:
                            debit = text
                        else:
                            description_parts.append(text)
                    elif col_boundaries.get("amount_x") and x >= col_boundaries["amount_x"] - 15:
                        # Single signed-amount column (Dr/Cr combined)
                        if is_number:
                            amount_raw = text
                        else:
                            description_parts.append(text)
                    else:
                        description_parts.append(text)

                # Split combined amount column into debit / credit
                if amount_raw and not debit and not credit:
                    debit, credit = _split_signed_amount(amount_raw, unsigned_is_debit=False)

                desc = " ".join(description_parts)
                if pending_desc_lines:
                    pre = "\n".join(_normalize_multiline_text(line) for line in pending_desc_lines if line)
                    desc = _append_description(pre, desc) if desc else pre
                    pending_desc_lines = []

                # Preserve zero balances (0.00 is valid) but clean amounts normally
                raw_balance = balance.strip().replace(" ", "")
                balance = _clean_amount(balance)
                if not balance and raw_balance and re.fullmatch(r"0+(?:\.0+)?", raw_balance.replace(",", "")):
                    balance = "0.00"
                debit = _clean_amount(debit)
                credit = _clean_amount(credit)

                if not debit and not credit and not balance:
                    continue

                # Clean page-header dumps from description but keep the transaction
                header_contaminated = any(p.match(desc) for p in _DESC_HEADER_RES)
                if header_contaminated:
                    stripped = _strip_page_header_prefix(desc)
                    desc = stripped if stripped else desc
                    # Don't skip — the row has valid date + amounts even if desc is empty

                # Strip leading reference codes from description
                desc = _strip_leading_ref(desc)

                # Clean merged footer/metadata content from description
                desc = _clean_description(desc)

                # Only apply SKIP_PHRASES when NOT header-contaminated
                if not header_contaminated:
                    row_text = (date + " " + desc).lower()
                    if any(phrase in row_text for phrase in SKIP_PHRASES):
                        continue

                transactions.append({
                    "date": date,
                    "description": desc,
                    "debit": debit,
                    "credit": credit,
                    "balance": balance,
                })

    return transactions


def _detect_word_columns(words: list[dict]) -> dict | None:
    """Detect column x-positions from header words on a page.

    Uses _score_header_line so multi-word headers like "Transaction Date",
    "Value Date", "Paid Out", "Paid In", "Running Balance" are matched as
    phrases, not just individual words.
    """
    # Group words by y-position
    lines_by_top = defaultdict(list)
    for w in words:
        lines_by_top[round(w["top"])].append(w)

    # Find the line with the highest column-header score
    best_line: list | None = None
    best_score = 0
    best_col_roles: list = []

    for line_words in lines_by_top.values():
        sorted_lw = sorted(line_words, key=lambda w: w["x0"])
        score, col_roles = _score_header_line(sorted_lw)
        if score > best_score:
            best_score = score
            best_line = sorted_lw
            best_col_roles = col_roles

    if best_score < 2 or best_line is None:
        return None

    # Build x-position map from matched roles (first occurrence wins)
    header_map: dict[str, float] = {}
    for role, x in best_col_roles:
        if role not in ("skip", "unknown") and role not in header_map:
            header_map[role] = x
        # Normalise "description" key to "desc" for downstream compatibility
        if role == "description" and "desc" not in header_map:
            header_map["desc"] = x

    if "date" not in header_map:
        return None
    has_amounts = "balance" in header_map or "debit" in header_map or "credit" in header_map or "amount" in header_map
    if not has_amounts:
        return None

    return {
        "date_x":    header_map.get("date", 0),
        "desc_x":    header_map.get("desc", header_map.get("date", 0) + 60),
        "debit_x":   header_map.get("debit"),
        "credit_x":  header_map.get("credit"),
        "balance_x": header_map.get("balance", header_map.get("amount", 500)),
        "amount_x":  header_map.get("amount"),
    }


def _row_has_amount_value(row: list, col_map: dict) -> bool:
    """True if any amount-type column in the row contains a real numeric value.

    Used to distinguish a true continuation row (text-only) from a failed-parse
    transaction row where the date cell came back empty due to table-detection
    wobble. Merging the latter into the previous transaction silently drops it.
    """
    for key in ("debit", "credit", "amount", "balance"):
        idx = col_map.get(key)
        if isinstance(idx, int) and 0 <= idx < len(row):
            if _clean_amount(str(row[idx] or "")):
                return True
    return False


def _is_noise_line(line: str) -> bool:
    """Check if a line is noise (headers, footers, metadata) — not transaction content."""
    stripped = line.strip()
    if not stripped:
        return True
    if re.match(r"^(?:REF#|/REF/|//REC/|SRN:)", stripped, re.IGNORECASE):
        return False
    line_lower = line.lower()
    # Skip known non-content lines
    if any(phrase in line_lower for phrase in SKIP_PHRASES):
        return True
    if line_lower.startswith("page ") or line_lower.startswith("page[") or re.match(r"^page\d", line_lower):
        return True
    # Very short single-word lines that are clearly metadata, not descriptions
    if stripped.rstrip(".") in ("accurate", "verified"):
        return True
    # Lines that are mostly non-ASCII (Arabic/other scripts) with no useful ASCII content
    ascii_chars = sum(1 for c in line if c.isascii() and c.isalnum())
    if ascii_chars < 3 and len(line) > 5:
        return True
    # Table header lines
    header_kws = {"date", "description", "withdrawal", "deposit", "balance", "cheque",
                  "narration", "particulars", "debit", "credit"}
    words = set(line_lower.split())
    if len(words & header_kws) >= 3:
        return True
    # Common footer patterns — only match when the line is short (clearly metadata,
    # not a transaction description mentioning a bank name)
    if len(line) < 120:
        if ("regulated" in line_lower and ("licensed" in line_lower or "central bank" in line_lower or "is regulated" in line_lower)):
            return True
    # Page number patterns like "[52] نم [2] ةحفص" or "Page [2] of [52]" or "page1 of8"
    if re.search(r"\[\d+\]\s*(of|نم)\s*\[\d+\]", line, re.IGNORECASE):
        return True
    if re.search(r"\bpage\s*\d+\s*of\s*\d+\b", line, re.IGNORECASE):
        return True
    # Page header / metadata lines
    if "your bank statement" in line_lower:
        return True
    if "statement of account" in line_lower:
        return True
    if "statement period" in line_lower or "date issued" in line_lower:
        return True
    # "Account Type / Number / Holder" lines are noise ONLY when the line is a
    # labeled metadata field (e.g. "Account Number: 12345678" or
    # "Account Number 12345678" with nothing else after). Real transaction
    # descriptions for cheque transfers often contain phrases like
    # "Account Number 80309100036511 To Account Number 80309100024534" —
    # those must NOT be discarded.
    _acct_field = re.match(
        r"^\s*account\s+(?:type|number|holder)\b\s*:?\s*",
        line_lower,
    )
    if _acct_field:
        rest = line_lower[_acct_field.end():].strip()
        # Empty rest → bare label. Pure digit/dash/space rest → labeled value.
        if not rest or re.fullmatch(r"[\d\-\s]*", rest):
            return True
        # Otherwise the words after "Account Number" are transaction prose
        # (e.g. "... To Account Number XYZ ...") — keep the line.
    if "current account transactions" in line_lower:
        return True
    if line_lower.startswith("iban:") or line_lower.startswith("branch:") or line_lower.startswith("currency:"):
        return True
    # Soft-skip phrases that are clearly metadata, not transaction descriptions
    _noise_phrases = [
        "account statement from", "please review this account statement",
        "if no issues are reported", "this bank is regulated",
        "confirmation of the correctness", "correctness of the statement",
        "electronically generated statement", "does not require a signature",
        "no notice of disagreement", "account opened",
        "interest rate", "current_account",
        "available balance as of", "licensed & regulated",
        "central bank of the uae",
        "statement date",
        "online banking - go to relationship summary",
        "select card>> block card",
        "phone banking - call",
        "unauthorized transaction",
        "wakala deposits is based on wakala contract",
        "savings account is based on mudaraba contract",
        "profit calculation, distribution and payment",
        "complaints management unit",
        "errors and omissions excepted",
        "banking / select card>>manage card >> block card",
        "main menu.",
        "sharia, as defined in the aaoifi",
        "sharia standards and the guidance of dib issc",
        "realization of profit from the underlying investments",
        "complaint within an estimated average of",
        # Mashreqbank-specific
        "report any discrepancies",
        "all charges, terms and conditions",
        "please note that for foreign currency",
        "subject to change",
        "indicative only",
        "central bank of the united arab emirates",
    ]
    if any(phrase in line_lower for phrase in _noise_phrases):
        return True
    if re.match(r"^\d+\s*-\s+", line_lower):
        return True
    if re.search(r"(?:\(cid:\d+\)){2,}", line, re.IGNORECASE):
        return True
    # Lines that are ONLY an IBAN number (no other content) are metadata
    # Don't filter lines where IBAN-like refs appear as part of transaction descriptions
    stripped = line.strip()
    if _IBAN_RE.fullmatch(stripped) or (line_lower.startswith("iban") and len(stripped) < 60):
        return True
    # Footer / contact info lines
    if any(marker in line_lower for marker in _DESC_FOOTER_MARKERS):
        return True
    # Phone numbers embedded in a short line (e.g. "600 500 946" or "+971 4 123 4567")
    # Only match very short lines that are predominantly phone numbers
    if len(line) < 25:
        phone_match = re.search(r"\b(?:\+\d{1,3}\s?)?\d[\d\s\-]{7,}\d\b", line)
        if phone_match and (phone_match.end() - phone_match.start()) > len(line) * 0.5:
            return True
    # Lines that are ONLY Arabic/RTL characters (no English transaction content)
    if "©" in line:
        return True
    ascii_alnum = sum(1 for c in line if c.isascii() and c.isalnum())
    arabic_chars = len(re.findall(r"[\u0600-\u06FF\u0750-\u077F\uFB50-\uFDFF\uFE70-\uFEFF]", line))
    if arabic_chars > 0 and ascii_alnum < 5 and len(line) > 5:
        return True
    # Predominantly-Arabic disclaimer lines (e.g. the trailing Mashreq footer
    # "accurate. <Arabic ...>") have some ASCII content but the Arabic clearly
    # dominates \u2014 treat those as noise too.
    if arabic_chars > 10 and arabic_chars > ascii_alnum * 2:
        return True
    return False


_REF_ONLY_DESC_RE = re.compile(r"(?=.*\d)(?=.*[A-Za-z])[A-Za-z0-9]{6,30}")
# A token with ≥10 chars looks like a bank reference code when it's either
# alphanumeric mixed (e.g. "035PF07250077139", "030POSB250160SQv") or pure
# digits (e.g. "0352663250350456" — too long to be a comma-less amount).
# When such a token is the LAST whitespace-separated chunk of a date-line's
# description, the date line carries no real narration text and the prefix
# from the line above (Mashreq layout) should be used as the description.
_REF_LIKE_TOKEN_RE = re.compile(
    r"(?:"
    r"(?=.*\d)(?=.*[A-Za-z])[A-Za-z0-9]{10,}"   # mixed letters+digits, ≥10 chars
    r"|\d{10,}"                                   # pure digits, ≥10 chars
    r"|0\d{4,}"                                   # leading-zero pure digits
    r")"
)


def _attach_continuation_to_prev(transactions: list[dict], lines: list[str]) -> None:
    """Attach a list of pending text lines to the previous transaction's description."""
    if not transactions or not lines:
        return
    prev = transactions[-1]
    for line in lines:
        if not line:
            continue
        if _is_noise_line(line) or _is_standalone_amount_line(line):
            continue
        prev["description"] = _clean_description(
            _append_description(prev["description"], line)
        )


def _extract_from_text(pdf_path: Path) -> list[dict]:
    """Extract transactions by parsing raw text lines from the PDF.

    Each transaction's description may have one prefix line BEFORE the date+amount
    line and any number of suffix lines AFTER it. To distinguish prefix-of-next
    from suffix-of-previous, non-date lines are buffered in ``pending_desc_lines``
    until the next date line arrives:

      * If the date line's own description is empty or just a ref code, the LAST
        pending line is treated as the prefix for the new transaction and earlier
        pending lines become suffix for the previous transaction.
      * Otherwise all pending lines become suffix for the previous transaction.

    Page metadata that leaks past the noise filter still reaches the buffer; it
    only causes harm if it ends up as ``pending[-1]`` when the first date line
    on a page arrives. The opening-balance check runs BEFORE the noise filter so
    statements that label their opening balance with a SKIP_PHRASES word (e.g.
    Mashreq's "Opening balance 104,477.54") still seed prev_balance.
    """
    transactions: list[dict] = []
    prev_balance: float | None = None

    with pdfplumber.open(pdf_path) as pdf:
        for page_num, page in enumerate(pdf.pages, 1):
            try:
                text = page.extract_text()
            except Exception as e:
                logger.warning(f"Text strategy, page {page_num}: extract_text error (skipping): {e}")
                continue
            if not text:
                continue

            pending_desc_lines: list[str] = []

            for line in text.split("\n"):
                line = line.strip()
                if not line:
                    continue

                # Capture opening balance BEFORE the noise filter — many banks
                # label this line "Opening balance ..." which SKIP_PHRASES kills.
                if prev_balance is None:
                    bal_match = re.search(
                        r"(?:balance\s+brought|opening\s+balance|b[/.]?f)\b.*?([\d,]+(?:\.\d{1,2})?)\s*$",
                        line, re.IGNORECASE,
                    )
                    if bal_match:
                        try:
                            prev_balance = float(bal_match.group(1).replace(",", ""))
                        except ValueError:
                            pass
                        continue

                if _is_noise_line(line):
                    continue

                txn = _parse_text_line(line, prev_balance)
                if txn:
                    desc = (txn.get("description") or "").strip()
                    desc_is_just_ref = bool(_REF_ONLY_DESC_RE.fullmatch(desc))
                    last_token = desc.rsplit(None, 1)[-1] if desc else ""
                    desc_ends_with_ref = bool(_REF_LIKE_TOKEN_RE.fullmatch(last_token))

                    if pending_desc_lines and (not desc or desc_is_just_ref or desc_ends_with_ref):
                        # Mashreq-style layout: the line right above the date
                        # line is the description prefix for this transaction.
                        # Triggered when the date line carries no real narration
                        # text — only a ref code (or an IBAN+ref pair).
                        prefix_line = pending_desc_lines[-1]
                        suffix_for_prev = pending_desc_lines[:-1]
                        if desc:
                            txn["description"] = _clean_description(
                                _append_description(prefix_line, desc)
                            )
                        else:
                            txn["description"] = _normalize_multiline_text(prefix_line)
                    else:
                        suffix_for_prev = list(pending_desc_lines)

                    _attach_continuation_to_prev(transactions, suffix_for_prev)
                    pending_desc_lines = []

                    transactions.append(txn)
                    if txn["balance"]:
                        try:
                            prev_balance = float(txn["balance"].replace(",", ""))
                        except ValueError:
                            pass
                    continue

                # Non-date line: balance-on-its-own-line OR continuation buffer.
                # Require a decimal point or thousands-comma so bare reference
                # fragments like "921944" don't get misread as a balance value.
                raw_line = line.strip()
                stripped = raw_line.replace(",", "")
                looks_like_amount = re.fullmatch(r"[+\-]?[\d,]+(?:\.\d{1,2})?", raw_line) is not None
                has_separator = "." in raw_line or "," in raw_line
                is_balance_line = (
                    transactions
                    and not transactions[-1].get("balance")
                    and looks_like_amount
                    and has_separator
                    and not DATE_START_RE.match(line)
                )
                if is_balance_line:
                    prev = transactions[-1]
                    new_balance = float(stripped.lstrip("+-"))
                    prev["balance"] = f"{new_balance:,.2f}"
                    amt_str = prev.get("debit") or prev.get("credit") or ""
                    if amt_str and prev_balance is not None:
                        try:
                            amt_val = float(amt_str.replace(",", ""))
                            if new_balance < prev_balance - 0.5:
                                prev["debit"] = f"{amt_val:,.2f}"
                                prev["credit"] = ""
                            elif new_balance > prev_balance + 0.5:
                                prev["debit"] = ""
                                prev["credit"] = f"{amt_val:,.2f}"
                        except ValueError:
                            pass
                    prev_balance = new_balance
                    continue

                if _is_standalone_amount_line(line):
                    continue

                pending_desc_lines.append(line)

            # End of page: any remaining pending lines are continuation for the
            # last transaction (the next page's first lines will repopulate the
            # buffer fresh, so a prefix on page N+1 cannot leak across).
            _attach_continuation_to_prev(transactions, pending_desc_lines)

    return transactions


def _parse_text_line(line: str, prev_balance: float | None = None) -> dict | None:
    """Parse a single text line into a transaction dict.

    Uses prev_balance (if available) to determine debit vs credit:
    balance increased → credit, balance decreased → debit.
    """
    line = _normalize_split_year_prefix(line.strip())

    # Try date at start of line first
    date_match = None
    rest_start = 0
    if DATE_START_RE.match(line):
        for pattern in DATE_PATTERNS:
            date_match = pattern.match(line)
            if date_match:
                break

    # Fallback: date after a serial number prefix (e.g. "1 17-Sep-2025 ...")
    if not date_match:
        sr_prefix = re.match(r"^\d{1,4}\s+", line)
        if sr_prefix:
            after_sr = line[sr_prefix.end():]
            if DATE_START_RE.match(after_sr):
                for pattern in DATE_PATTERNS:
                    date_match = pattern.match(after_sr)
                    if date_match:
                        rest_start = sr_prefix.end()
                        break

    if not date_match:
        return None

    date = date_match.group(1)
    rest = line[rest_start + date_match.end():].strip()

    # Strip a second date (Value Date) if it appears right after the transaction date
    for pattern in DATE_PATTERNS:
        m2 = pattern.match(rest)
        if m2:
            rest = rest[m2.end():].strip()
            break

    # Skip summary/total lines
    line_lower = line.lower()
    if any(phrase in line_lower for phrase in SKIP_PHRASES):
        return None

    # Find all potential amounts in the line (whitespace-bounded numbers)
    all_matches = list(TEXT_AMOUNT_RE.finditer(rest))
    if not all_matches:
        return None

    # Take trailing amounts from the right (last 2 or 1 numbers are amount+balance)
    # Work backwards to find the trailing number cluster
    trailing_amounts = []
    trailing_start = len(rest)
    for m in reversed(all_matches):
        # Check if this match connects to the trailing cluster (only whitespace between)
        gap = rest[m.end():trailing_start].strip()
        if gap == "" or not trailing_amounts:
            trailing_amounts.insert(0, m)
            trailing_start = m.start()
        else:
            break

    if not trailing_amounts:
        return None

    # Filter out small plain integers from the leading edge of the trailing
    # cluster — they're likely part of the description (e.g. "SITE 48",
    # "FLOOR 3", "REF 12345").  Keep only amounts that look monetary:
    # have a decimal point, have commas, are negative, or are large (>= 1000).
    # Exception: if prev_balance validates the small integer as the real
    # transaction amount (prev_balance ± small_int ≈ trailing_balance), keep it.
    # This handles banks like Mashreq where fees are whole-number amounts (e.g. "2").
    while len(trailing_amounts) > 1:
        first_raw = trailing_amounts[0].group(1)
        first_val = first_raw.replace(",", "").lstrip("+-")
        has_decimal = "." in first_val
        has_comma = "," in first_raw
        is_negative = first_raw.startswith("-")
        is_explicit_plus = first_raw.startswith("+")
        is_large = float(first_val) >= 1000 if first_val.replace(".", "").isdigit() else False
        is_plain_year = (
            not has_decimal
            and not has_comma
            and not is_negative
            and not is_explicit_plus
            and first_val.isdigit()
            and len(first_val) == 4
            and 1900 <= int(first_val) <= 2099
        )
        if is_plain_year and len(trailing_amounts) >= 3:
            trailing_amounts.pop(0)
            continue
        # Reject pure-digit tokens that look like reference numbers, not amounts:
        #   - 10+ digits with no separator (any real bank amount that large would
        #     be comma-grouped, e.g. "1,234,567,890")
        #   - 5+ digits with a leading zero (legitimate amounts don't have
        #     leading zeros; ref codes like "0352663250350456" do)
        is_ref_like_digits = (
            not has_decimal
            and not has_comma
            and not is_negative
            and not is_explicit_plus
            and first_val.isdigit()
            and (
                len(first_val) >= 10
                or (first_val.startswith("0") and len(first_val) >= 5)
            )
        )
        if is_ref_like_digits:
            trailing_amounts.pop(0)
            continue
        if not has_decimal and not is_negative and not is_explicit_plus and not is_large:
            # Small plain integer — check balance chain before discarding
            if prev_balance is not None and len(trailing_amounts) == 2:
                try:
                    cand_amt = float(first_val)
                    cand_bal = float(trailing_amounts[1].group(1).replace(",", "").lstrip("+-"))
                    if (abs(prev_balance + cand_amt - cand_bal) < 1.0 or
                            abs(prev_balance - cand_amt - cand_bal) < 1.0):
                        break  # balance chain confirms this integer is the real amount
                except ValueError:
                    pass
            trailing_amounts.pop(0)
        else:
            break

    if not trailing_amounts:
        return None

    # Description is everything before the trailing amounts
    desc_end = trailing_amounts[0].start()
    description = rest[:desc_end].strip()
    description = re.sub(r"\s+", " ", description).strip()
    # Strip leading reference codes that bled into description (e.g. Mashreq
    # "030POSB2433302kK Visa Purchase..." → "Visa Purchase...")
    description = _strip_leading_ref(description)

    amount_strs = [m.group(1) for m in trailing_amounts]

    if len(amount_strs) == 1:
        val = float(amount_strs[0].replace(",", ""))
        if val < 0:
            return {"date": date, "description": description,
                    "debit": f"{abs(val):,.2f}", "credit": "", "balance": ""}
        elif prev_balance is not None:
            # Single positive amount with prev_balance context.
            # Check if balance chain validates it as a real transaction amount
            # (prev_balance ± val ≈ some reasonable next balance), which means
            # the balance will appear on the next line.  Treat as debit if
            # prev_balance - val > 0 (more common), otherwise credit.
            # Only fall back to "balance" interpretation when the value is very
            # close to prev_balance itself (within 1%).
            if abs(val - prev_balance) / max(prev_balance, 1) < 0.01:
                # Value ≈ prev_balance → likely a balance echo, not an amount
                return {"date": date, "description": description,
                        "debit": "", "credit": "", "balance": f"{val:,.2f}"}
            else:
                # Treat as a transaction amount; debit/credit will be
                # reclassified when the standalone balance line is detected.
                # Default to credit (positive); reclassification fixes it.
                return {"date": date, "description": description,
                        "debit": "", "credit": f"{val:,.2f}", "balance": ""}
        else:
            # No previous balance — positive single amount is credit
            return {"date": date, "description": description,
                    "debit": "", "credit": f"{val:,.2f}", "balance": ""}

    # Last = balance, second-to-last = amount
    balance_str = amount_strs[-1]
    amount_str = amount_strs[-2]
    amount_val = float(amount_str.replace(",", ""))
    balance_val = float(balance_str.replace(",", ""))

    # Use balance delta to classify debit vs credit
    if amount_val < 0:
        debit = f"{abs(amount_val):,.2f}"
        credit = ""
    elif amount_str.startswith("+"):
        # Explicit + sign → unconditionally credit (Mashreq / WIO style)
        debit = ""
        credit = f"{amount_val:,.2f}"
    elif prev_balance is not None:
        # Compare with previous balance to determine direction
        if balance_val < prev_balance:
            # Balance decreased → debit (money out)
            debit = f"{amount_val:,.2f}"
            credit = ""
        else:
            # Balance increased → credit (money in)
            debit = ""
            credit = f"{amount_val:,.2f}"
    else:
        # No previous balance — can't determine, default to credit for positive
        debit = ""
        credit = f"{amount_val:,.2f}"

    return {
        "date": date,
        "description": description,
        "debit": debit,
        "credit": credit,
        "balance": f"{balance_val:,.2f}",
    }


# Regex for a leading reference/transaction code in a description field.
# Matches an all-alphanumeric token (letters+digits, no spaces/specials) that:
#   • is at least 6 characters long
#   • contains at least one digit (so it's not a plain English word)
#   • is followed by a space and more text (not the entire description)
# Examples matched: "P104736051", "TXN20240201", "CHQ001234", "REF123456",
#                   "030POSB2433302kK", "099REFEAED", "033INCR243450722"
# Examples NOT matched: "From", "Invoice", "SALARY" (no digits or too short)
_REF_CODE_RE = re.compile(r"^((?=[A-Za-z0-9]*\d)[A-Za-z0-9]{6,})\s+(?=\S)")


def _strip_leading_ref(description: str) -> str:
    """Remove a leading reference/transaction code from a description string.

    Called as a safety net when column detection may have merged the ref-number
    column with the description column (e.g. pdfplumber merging narrow PDF cols).

    Only strips when the first token:
      - is entirely alphanumeric (A-Z, 0-9 only, no spaces or special chars)
      - contains at least one digit (looks like a code, not a plain word)
      - is at least 6 characters long
      - is followed by more content (not the whole description)

    Also strips a secondary short numeric sub-code (e.g. "099REFEAED 00002 Desc"
    → strips both "099REFEAED" and "00002").

    Handles standalone short numeric codes (e.g. "00001 Monthly Maintenance Fee")
    that appear when the main ref was on a separate line in multi-line transactions.
    """
    # IBAN prefix (e.g. "AE060500...") is legitimate transaction data — the
    # recipient/sender IBAN, not a column-bleed ref code. Keep it intact.
    if _IBAN_RE.match(description):
        return description
    m = _REF_CODE_RE.match(description)
    if m:
        remainder = description[m.end():].strip()
        if remainder:  # Only strip if there's still content left
            # Check for a secondary short numeric code (e.g. "00001", "00002")
            sub_code = re.match(r"^(\d{3,6})\s+(?=\S)", remainder)
            if sub_code:
                after_sub = remainder[sub_code.end():].strip()
                if after_sub:
                    return after_sub
            # If remainder is just a short numeric code with no further text,
            # strip it too (e.g. "099REFEAED 00002" → both are ref codes)
            if re.fullmatch(r"\d{3,6}", remainder):
                return ""
            return remainder
    # Also handle standalone short numeric sub-codes (e.g. "00001 Monthly Maintenance Fee")
    # These appear when the main ref code was on a different line in multi-line transactions.
    sub_code = re.match(r"^(\d{3,6})\s+(?=\S)", description)
    if sub_code:
        after_sub = description[sub_code.end():].strip()
        # Only strip if remaining text starts with a letter (looks like actual description)
        if after_sub and after_sub[0].isalpha():
            return after_sub
    return description


def _clean_amount(value: str) -> str:
    """Clean and normalize an amount string. Returns '' for zero values."""
    if not value:
        return ""
    value = value.strip().replace(" ", "")

    # Treat zero as empty (e.g. "0.00" in debit column means no debit)
    stripped = value.replace(",", "").lstrip("0").lstrip(".")
    if not stripped or all(c == "0" for c in stripped):
        return ""

    # Strip leading +
    if value.startswith("+"):
        value = value[1:]

    if value.startswith("(") and value.endswith(")"):
        value = "-" + value[1:-1]

    if not FLEX_AMOUNT_RE.fullmatch(value):
        value = re.sub(r"[A-Za-z$€£¥₹+]", "", value).strip().rstrip(".")
        if not FLEX_AMOUNT_RE.fullmatch(value):
            return ""

    # Validate thousands-grouping: if commas are present, groups after the first
    # must be exactly 3 digits (e.g. "1,234,567.89" is valid but
    # "-2146188822,11125238215385" is not — it's concatenated IDs, not an amount).
    sign_stripped = value.lstrip("-")
    if "," in sign_stripped:
        dot_pos = sign_stripped.find(".")
        integer_part = sign_stripped[:dot_pos] if dot_pos != -1 else sign_stripped
        parts = integer_part.split(",")
        # First group: 1–3 digits; every subsequent group: exactly 3 digits
        if len(parts) > 1:
            first_ok = 1 <= len(parts[0]) <= 3
            rest_ok = all(len(p) == 3 and p.isdigit() for p in parts[1:])
            if not (first_ok and rest_ok):
                return ""

    return value


def _clean_table(table: list[list]) -> list[list[str]]:
    """Clean a table by replacing None values and stripping whitespace."""
    cleaned = []
    for row in table:
        if row is None:
            continue
        cleaned_row = [_normalize_multiline_text(cell) if cell else "" for cell in row]
        if any(cell for cell in cleaned_row):
            cleaned.append(cleaned_row)
    return cleaned



