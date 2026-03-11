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
    # D Mon YYYY / D Month YYYY  e.g. "1 Jan 2024", "15 January 2024"
    re.compile(rf"\b(\d{{1,2}}\s+{_MONTH}\s+\d{{4}})\b", re.IGNORECASE),
    # D-Mon-YYYY / D/Mon/YYYY    e.g. "4-Nov-2025", "4/Nov/2025"
    re.compile(rf"\b(\d{{1,2}}[/\-.]{_MONTH}[/\-.]\d{{4}})\b", re.IGNORECASE),
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
    rf"|^\d{{1,2}}\s+{_MONTH}\s+\d{{2,4}}"
    rf"|^{_MONTH}",
    re.IGNORECASE,
)

AMOUNT_RE = re.compile(r"-?[\d,]+\.\d{2}")
SIGNED_AMOUNT_RE = re.compile(r"[+\-]?[\d,]+\.\d{2}")
FLEX_AMOUNT_RE = re.compile(r"-?[\d,]+(?:\.\d{1,2})?$")
# For finding amounts in text lines (must be whitespace-bounded)
TEXT_AMOUNT_RE = re.compile(r"(?:^|\s)(-?[\d,]+(?:\.\d{1,2})?)(?=\s|$)")

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
}

AMOUNT_KEYWORDS = {
    "amount", "amt",
    "transaction amount", "txn amount", "tran amount", "trans amount",
    "net amount", "net amt",
    "local amount", "foreign amount",
    # Combined Dr/Cr column (signed single amount column)
    "dr/cr", "cr/dr", "dr / cr", "cr / dr",
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
}

# Rows containing these phrases (case-insensitive) are not real transactions
SKIP_PHRASES = [
    "total records", "total debit", "total credit", "grand total",
    "opening balance", "closing balance", "balance brought", "balance carried",
    "statement summary", "account summary", "end of statement",
    "balance b/f", "balance c/f", "brought forward", "carried forward",
    # Page header / account info markers
    "summary of accounts", "summary of savings",
    "please review this account statement",
    "account statement from",
    "account type", "account holder",
    "if no issues are reported",
    # Footer / disclaimer text that pdfplumber merges into rows
    "confirmation of the correctness", "correctness of the statement",
    "electronically generated statement", "does not require a signature",
    "does not require . a signature",
    "no notice of disagreement", "paid up capital",
    "registered details: emirates",
]

# Regex to detect IBAN numbers in description text (clear sign of a header dump)
_IBAN_RE = re.compile(r"\b[A-Z]{2}\d{15,}\b")

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
]

# Matches a single page-header metadata field (keyword + value).
# Used to find where the page header block ends inside a contaminated description cell,
# e.g. "Transaction history Acme Corp Currency AED Branch Al Tawar Branch <real desc>"
#                                       ^^^^^^^^^^^^^ ^^^^^^^^^^^^^^^^^^^^^^^^^
# Taking the end position of the LAST match gives the start of the real description.
_PAGE_META_FIELD_RE = re.compile(
    r"(?:"
    r"currency\s+[A-Z]{2,4}"                             # Currency AED
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
    "this is a digital stamp", "standard terms", "terms and conditions",
    "does not require signature", "does not require . a signature",
    "wio bank pjsc", "po box",
    "transaction history",
    "confirmation of the correctness", "correctness of the statement",
    "electronically generated", "registered details",
    "no notice of disagreement", "paid up capital",
    "tax registration number",
]

# Regex: truncate at © symbol or start of Arabic/RTL block mid-description
_DESC_POISON_RE = re.compile(
    r"©|\(c\)\s*\d{4}"                  # copyright mark
    r"|[\u0600-\u06FF\u0750-\u077F]"    # Arabic / Farsi / Urdu characters
    r"|\bpo\s+box\b",                    # mailing address
    re.IGNORECASE,
)
# Strip trailing phone-number sequences from descriptions
_TRAILING_PHONE_RE = re.compile(r"\s+\d[\d\s\-]{5,}\d\s*$")

# Column roles that, when appearing consecutively inside a description, indicate
# that the next page's column header row was merged in.
_COL_HEADER_ROLES = frozenset({
    "date", "description", "debit", "credit", "balance", "amount", "skip",
})


def _clean_description(description: str) -> str:
    """Strip footer/metadata content merged into a transaction description.

    Handles four cases:
      1. Known text markers ("for assistance", "contact us", etc.)
      2. Arabic characters or © copyright symbol appearing mid-text
      3. Trailing phone-number sequences
      4. Next-page column headers merged in (e.g. "... Amount Balance Date Ref. Number")
    """
    if not description:
        return description

    # 1. Truncate at known footer text markers
    desc_lower = description.lower()
    for marker in _DESC_FOOTER_MARKERS:
        idx = desc_lower.find(marker)
        if idx > 10:
            description = description[:idx].strip()
            desc_lower = description.lower()
            break

    # 2. Truncate at © or first Arabic character (clearly non-transaction content)
    m = _DESC_POISON_RE.search(description)
    if m and m.start() > 10:
        description = description[:m.start()].strip()

    # 3. Strip trailing phone-number-like digit sequences
    description = _TRAILING_PHONE_RE.sub("", description).strip()

    # 4. Detect consecutive column-header words merged into description.
    #    e.g. "Owais Arshad Khan Amount Balance Date Ref. Number Description..."
    #    Three or more consecutive words that all classify as column headers → truncate.
    words = description.split()
    run_start: int | None = None
    run_len = 0
    for i, word in enumerate(words):
        # Strip punctuation/brackets but keep dots (for "Ref.")
        clean_word = re.sub(r"[^A-Za-z0-9.]", "", word)
        role, conf = classify_column_header(clean_word)
        if conf >= 0.80 and role in _COL_HEADER_ROLES:
            if run_start is None:
                run_start = i
            run_len += 1
            # Require at least 3 consecutive header words AND real content before them
            if run_len >= 3 and run_start > 0:
                description = " ".join(words[:run_start]).strip()
                break
        else:
            run_start = None
            run_len = 0

    return description


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
    Returns "" if no metadata boundary can be located.
    """
    # Limit search to the first 200 chars so that words like "Branch" or "Currency"
    # appearing inside the real description don't cause a false truncation point.
    search_in = description[:200]
    matches = list(_PAGE_META_FIELD_RE.finditer(search_in))
    if not matches:
        return ""
    remainder = description[matches[-1].end():].strip()
    return remainder if len(remainder) > 5 else ""


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

    # Normalise: strip non-ASCII (bilingual PDFs), collapse whitespace, lowercase
    h = re.sub(r"[^\x00-\x7F]", "", header)
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
           ("amount", " amt", "dr/cr", "cr/dr"))
    if h in ("dr/cr", "cr/dr", "dr / cr", "cr / dr"):
        scores["amount"] = max(scores.get("amount", 0.0), 0.95)

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


def extract_transactions(pdf_path: str) -> list[dict]:
    """Extract structured transactions from a bank statement PDF."""
    pdf_path = Path(pdf_path)
    if not pdf_path.exists():
        raise FileNotFoundError(f"PDF file not found: {pdf_path}")
    if pdf_path.suffix.lower() != ".pdf":
        raise ValueError(f"Not a PDF file: {pdf_path}")

    # Strategy 1: structured table extraction
    transactions = _extract_from_tables(pdf_path)
    if transactions and not _has_merged_rows(transactions):
        return transactions

    # Strategy 2: position-based word extraction (for PDFs without table structures)
    # Also used as fallback when Strategy 1 produces merged rows
    if transactions:
        logger.info("Strategy 1 produced merged rows, falling back to word/text extraction")
    transactions = _extract_from_words(pdf_path)
    if transactions:
        return transactions

    # Strategy 3: text-based line parsing
    return _extract_from_text(pdf_path)


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
        # Count how many date patterns appear in the date field
        date_matches = []
        for p in DATE_PATTERNS:
            date_matches.extend(p.findall(date))
        if len(date_matches) > 1:
            merged_count += 1
    # If more than 30% of rows have multiple dates, extraction is broken
    return merged_count > len(transactions) * 0.3


def _extract_from_tables(pdf_path: Path) -> list[dict]:
    """Extract transactions from structured PDF tables by mapping columns."""
    transactions = []
    col_map = None  # Detect once, reuse across pages
    col_map_num_cols = 0  # Number of columns col_map was detected for

    with pdfplumber.open(pdf_path) as pdf:
        for page_num, page in enumerate(pdf.pages, 1):
            tables = page.extract_tables()
            if not tables:
                logger.debug(f"Page {page_num}: no tables found")
                continue

            logger.debug(f"Page {page_num}: found {len(tables)} table(s), rows per table: {[len(t) for t in tables]}")

            for table in tables:
                cleaned = _clean_table(table)
                if not cleaned:
                    continue

                num_cols = len(cleaned[0])
                logger.debug(f"Page {page_num}: table with {len(cleaned)} rows, {num_cols} cols, first row: {cleaned[0][:3]}...")
                if len(cleaned) <= 3:
                    for ri, r in enumerate(cleaned):
                        logger.debug(f"  Row {ri}: {[c[:50] for c in r]}")

                # If col_map was detected for a different column count, reset it
                if col_map is not None and num_cols != col_map_num_cols:
                    logger.debug(f"Page {page_num}: col_map has {col_map_num_cols} cols but table has {num_cols} cols, re-detecting")
                    new_map = _detect_columns(cleaned, page)
                    if new_map:
                        col_map = new_map
                        col_map_num_cols = num_cols
                        logger.info(f"Column mapping re-detected on page {page_num}: {col_map}")
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
                    else:
                        # Can't detect for this table, skip it
                        continue

                # Detect column mapping once
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

                # Skip header rows — detect repeated headers on each page
                skip = col_map.get("header_row_count", 0)
                start = 0
                if skip > 0 and len(cleaned) > skip:
                    # Check if first row looks like the header (contains header keywords)
                    first_row_text = " ".join(cleaned[0]).lower()
                    if any(kw in first_row_text for kw in ("date", "description", "narration", "debit", "credit", "balance", "particulars")):
                        start = skip

                for row in cleaned[start:]:
                    txn = _row_to_transaction(row, col_map)
                    if txn:
                        transactions.append(txn)

    return transactions


def _detect_columns(table: list[list[str]], page=None) -> dict | None:
    """Detect column mapping by examining header rows or inferring from content."""
    # First try header-based detection from table data
    for header_idx in range(min(3, len(table))):
        row = table[header_idx]
        # Strip newlines and non-ASCII (bilingual headers like "Date\nتاريخ")
        raw_headers = [re.sub(r"[^\x00-\x7F]", "", cell).replace("\n", " ").strip() for cell in row]

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
    date_col = None
    for i in range(num_cols):
        if date_scores[i] >= max(2, total_sampled * 0.3):
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
        # Could be debit+balance or debit+credit — check if one has many empties
        e0 = empty_scores[amount_cols[0]]
        e1 = empty_scores[amount_cols[1]]
        if e0 > total_sampled * 0.3 or e1 > total_sampled * 0.3:
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
        return None

    # ── Pre-clean description BEFORE row-level filters ───────────────────────
    # pdfplumber can merge page-header content (IBAN, "account type", etc.) into
    # the description cell of the first table row on each page.  If we run the
    # SKIP_PHRASES / IBAN row-scan first we will falsely discard valid rows.
    # Strip the header prefix now so the row-scan sees clean text.
    description = re.sub(r"\s+", " ", get("description")).strip()
    if "reference" not in col_map and "ref" not in col_map:
        description = _strip_leading_ref(description)
    header_was_contaminated = any(p.match(description) for p in _DESC_HEADER_RES)
    if header_was_contaminated:
        description = _strip_page_header_prefix(description)
        # Don't drop the row — it still has valid financial data (date, amounts, balance)

    # ── Pre-filter: scan row using the cleaned description ───────────────────
    desc_idx = col_map.get("description")
    scan_row = [
        description if i == desc_idx else str(c)
        for i, c in enumerate(row)
    ]
    row_text = " ".join(scan_row).lower()
    if any(phrase in row_text for phrase in SKIP_PHRASES):
        return None
    # Only check for IBAN in rows that were header-contaminated (page header merged
    # into description).  Normal transaction descriptions often contain IBAN-like
    # reference numbers (e.g. "AE600260000959054852801") that are NOT metadata.
    if header_was_contaminated and _IBAN_RE.search(" ".join(scan_row)):
        return None

    # ── Date ─────────────────────────────────────────────────────────────────
    date = get("date")
    # Normalize whitespace around date separators (pdfplumber wraps wide-table cells)
    # e.g. "27-Sep- 2025" → "27-Sep-2025", "15-Oct- 2025" → "15-Oct-2025"
    date = re.sub(r"\s*([/\-.])\s*", r"\1", date)
    date = re.sub(r"\s+", " ", date).strip()
    if not date or not any(p.search(date) for p in DATE_PATTERNS):
        return None

    # ── Description (already extracted and header-stripped above) ────────────
    # Only reject on IBAN if the description was header-contaminated and still
    # contains an IBAN after stripping — indicates unrecoverable header content.
    if header_was_contaminated and _IBAN_RE.search(description):
        return None

    # Strip footer/metadata merged into the description
    description = _clean_description(description)

    balance = _clean_amount(get("balance"))

    # Handle single "amount" column (signed values split into debit/credit)
    if "amount" in col_map:
        raw_amount = get("amount")
        unsigned_is_debit = col_map.get("unsigned_is_debit", False)
        debit, credit = _split_signed_amount(raw_amount, unsigned_is_debit)
    else:
        debit = _clean_amount(get("debit"))
        credit = _clean_amount(get("credit"))

    if not debit and not credit and not balance:
        return None

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
        return cleaned, ""
    else:
        # No explicit sign — convention-dependent
        if unsigned_is_debit:
            return cleaned, ""
        else:
            return "", cleaned


def _extract_from_words(pdf_path: Path) -> list[dict]:
    """Extract transactions using word positions when tables aren't detected.

    Detects column layout from header words (Date, Description, Withdrawal/Debit,
    Deposit/Credit, Balance) and assigns values by x-coordinate.
    """
    col_boundaries = None  # {date_x, desc_x, debit_x, credit_x, balance_x}
    transactions = []
    pending_desc_lines = []

    with pdfplumber.open(pdf_path) as pdf:
        for page_num, page in enumerate(pdf.pages):
            words = page.extract_words()
            if not words:
                continue

            # Try to detect column positions from header words on this page
            if col_boundaries is None:
                col_boundaries = _detect_word_columns(words)
                if col_boundaries is None:
                    continue
                logger.info(f"Word column positions detected on page {page_num + 1}: {col_boundaries}")

            # Group words by y-position (same line)
            lines_by_top = defaultdict(list)
            for w in words:
                lines_by_top[round(w['top'])].append(w)

            last_date_y = None  # y-position of the most recent date line
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
                    # Use y-gap: if close to the last date line (<15px), it's continuation.
                    # Otherwise, it's a pending description for the next transaction.
                    is_continuation = (
                        transactions
                        and not pending_desc_lines
                        and last_date_y is not None
                        and (top - last_date_y) < 18
                    )
                    if is_continuation:
                        prev = transactions[-1]
                        prev["description"] = (prev["description"] + " " + line_text).strip()
                        last_date_y = top  # Extend the "current transaction" zone
                    else:
                        pending_desc_lines.append(line_text)
                    continue

                # Date line — extract column values by x-position
                last_date_y = top
                date = first_text
                description_parts = []
                debit = ""
                credit = ""
                balance = ""

                amount_raw = ""  # for single "amount" column
                for w in line_words[1:]:  # Skip the date word
                    x = w["x0"]
                    text = w["text"]

                    # Skip "Cr." / "Dr." suffixes on balance
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
                    pre = " ".join(pending_desc_lines)
                    desc = (pre + " " + desc).strip() if desc else pre
                    pending_desc_lines = []

                balance = _clean_amount(balance)
                debit = _clean_amount(debit)
                credit = _clean_amount(credit)

                if not debit and not credit and not balance:
                    continue

                # Skip page-header dumps and summary rows
                header_contaminated = any(p.match(desc) for p in _DESC_HEADER_RES)
                if header_contaminated:
                    desc = _strip_page_header_prefix(desc)
                    if not desc:
                        continue
                if header_contaminated and _IBAN_RE.search(desc):
                    continue
                # Strip footer/metadata merged into the description
                desc = _clean_description(desc)
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


def _is_noise_line(line: str) -> bool:
    """Check if a line is noise (headers, footers, metadata) — not transaction content."""
    line_lower = line.lower()
    # Skip known non-content lines
    if any(phrase in line_lower for phrase in SKIP_PHRASES):
        return True
    if line_lower.startswith("page ") or line_lower.startswith("page["):
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
    # Common footer patterns
    if "national bank" in line_lower or "central bank" in line_lower:
        return True
    if "regulated" in line_lower and "licensed" in line_lower:
        return True
    # Page number patterns like "[52] نم [2] ةحفص" or "Page [2] of [52]"
    if re.search(r"\[\d+\]\s*(of|نم)\s*\[\d+\]", line, re.IGNORECASE):
        return True
    # Page header / metadata lines
    if "your bank statement" in line_lower:
        return True
    if "statement of account" in line_lower:
        return True
    if "statement period" in line_lower or "date issued" in line_lower:
        return True
    if "account type" in line_lower or "account number" in line_lower:
        return True
    if "current account transactions" in line_lower:
        return True
    if line_lower.startswith("iban:") or line_lower.startswith("branch:") or line_lower.startswith("currency:"):
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
    if re.search(r"\b(?:\+\d{1,3}\s?)?\d[\d\s\-]{7,}\d\b", line) and len(line) < 50:
        return True
    # Lines that are ONLY Arabic/RTL characters (no English transaction content)
    if "©" in line:
        return True
    ascii_alnum = sum(1 for c in line if c.isascii() and c.isalnum())
    arabic_chars = len(re.findall(r"[\u0600-\u06FF]", line))
    if arabic_chars > 0 and ascii_alnum < 5 and len(line) > 5:
        return True
    return False


def _extract_from_text(pdf_path: Path) -> list[dict]:
    """Extract transactions by parsing raw text lines from the PDF.

    Handles multi-line transactions where description lines appear
    before and/or after the date+amount line.
    """
    transactions = []
    prev_balance = None
    pending_desc_lines = []  # Non-date lines accumulated before a date line

    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages:
            text = page.extract_text()
            if not text:
                continue

            for line in text.split("\n"):
                line = line.strip()
                if not line:
                    continue

                # Skip noise lines
                if _is_noise_line(line):
                    # Don't clear pending — noise can appear between desc and date lines
                    continue

                # Try to capture opening balance from lines like "Balance Brought FWD 33,087.14"
                if prev_balance is None:
                    bal_match = re.search(
                        r"(?:balance\s+brought|opening\s+balance|b[/.]?f)\b.*?([\d,]+(?:\.\d{1,2})?)\s*$",
                        line, re.IGNORECASE,
                    )
                    if bal_match:
                        prev_balance = float(bal_match.group(1).replace(",", ""))
                        continue

                txn = _parse_text_line(line, prev_balance)
                if txn:
                    # Prepend accumulated description lines
                    if pending_desc_lines:
                        pre_desc = " ".join(pending_desc_lines)
                        if txn["description"]:
                            txn["description"] = pre_desc + " " + txn["description"]
                        else:
                            txn["description"] = pre_desc
                    pending_desc_lines = []

                    transactions.append(txn)
                    if txn["balance"]:
                        try:
                            prev_balance = float(txn["balance"].replace(",", ""))
                        except ValueError:
                            pass
                else:
                    # Non-date line: accumulate as description
                    # If we just processed a transaction (pending is empty),
                    # this is a continuation line for the previous transaction
                    if transactions and not pending_desc_lines:
                        prev = transactions[-1]
                        prev["description"] = (
                            (prev["description"] + " " + line).strip()
                            if prev["description"] else line
                        )
                    else:
                        pending_desc_lines.append(line)

    return transactions


def _parse_text_line(line: str, prev_balance: float | None = None) -> dict | None:
    """Parse a single text line into a transaction dict.

    Uses prev_balance (if available) to determine debit vs credit:
    balance increased → credit, balance decreased → debit.
    """
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

    # Description is everything before the trailing amounts
    desc_end = trailing_amounts[0].start()
    description = rest[:desc_end].strip()
    description = re.sub(r"\s+", " ", description).strip()
    description = _strip_leading_ref(description)
    description = _clean_description(description)

    amount_strs = [m.group(1) for m in trailing_amounts]

    if len(amount_strs) == 1:
        val = float(amount_strs[0].replace(",", ""))
        if val < 0:
            return {"date": date, "description": description,
                    "debit": f"{abs(val):,.2f}", "credit": "", "balance": ""}
        else:
            return {"date": date, "description": description,
                    "debit": "", "credit": "", "balance": f"{val:,.2f}"}

    # Last = balance, second-to-last = amount
    balance_str = amount_strs[-1]
    amount_str = amount_strs[-2]
    amount_val = float(amount_str.replace(",", ""))
    balance_val = float(balance_str.replace(",", ""))

    # Use balance delta to classify debit vs credit
    if amount_val < 0:
        debit = f"{abs(amount_val):,.2f}"
        credit = ""
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
# Examples matched: "P104736051", "TXN20240201", "CHQ001234", "REF123456"
# Examples NOT matched: "From", "Invoice", "SALARY" (no digits or too short)
_REF_CODE_RE = re.compile(r"^([A-Za-z]{0,4}\d{6,}[A-Za-z0-9]*)\s+(?=\S)")


def _strip_leading_ref(description: str) -> str:
    """Remove a leading reference/transaction code from a description string.

    Called as a safety net when column detection may have merged the ref-number
    column with the description column (e.g. pdfplumber merging narrow PDF cols).

    Only strips when the first token:
      - is entirely alphanumeric (A-Z, 0-9 only, no spaces or special chars)
      - contains at least one letter AND at least one digit (looks like a code)
      - is at least 6 characters long
      - is followed by more content (not the whole description)
    """
    m = _REF_CODE_RE.match(description)
    if m:
        remainder = description[m.end():].strip()
        if remainder:  # Only strip if there's still content left
            return remainder
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

    return value


def _clean_table(table: list[list]) -> list[list[str]]:
    """Clean a table by replacing None values and stripping whitespace."""
    cleaned = []
    for row in table:
        if row is None:
            continue
        cleaned_row = [
            (str(cell).strip().replace("\n", " ") if cell else "")
            for cell in row
        ]
        if any(cell for cell in cleaned_row):
            cleaned.append(cleaned_row)
    return cleaned
