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
    "this is a digital stamp",
    "does not require signature", "does not require . a signature",
    "wio bank pjsc",
    "confirmation of the correctness", "correctness of the statement",
    "electronically generated statement", "registered details: emirates",
    "no notice of disagreement", "paid up capital",
    "tax registration number",
]
# These markers are only truncated when they appear far enough into the text
# (>= _FOOTER_MIN_POS chars in).  Short descriptions must not be destroyed.
_FOOTER_MIN_POS = 30

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

    The function is deliberately conservative: it only truncates when the marker
    appears well past the start of the text (_FOOTER_MIN_POS chars in) so that
    short or medium-length descriptions are never accidentally destroyed.
    """
    if not description:
        return description

    # 1. Truncate at known footer text markers — only when they appear far enough
    #    into the description that there is meaningful content before the marker.
    desc_lower = description.lower()
    for marker in _DESC_FOOTER_MARKERS:
        idx = desc_lower.find(marker)
        if idx >= _FOOTER_MIN_POS:
            description = description[:idx].strip()
            desc_lower = description.lower()
            break

    # 2. Truncate at © or first Arabic character (clearly non-transaction content)
    #    Only if there's enough real content before it.
    m = _DESC_POISON_RE.search(description)
    if m and m.start() >= _FOOTER_MIN_POS:
        description = description[:m.start()].strip()

    # 3. Strip trailing phone-number-like digit sequences
    description = _TRAILING_PHONE_RE.sub("", description).strip()

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
    # Limit search to the first 200 chars so that words like "Branch" or "Currency"
    # appearing inside the real description don't cause a false truncation point.
    search_in = description[:200]
    matches = list(_PAGE_META_FIELD_RE.finditer(search_in))
    if matches:
        remainder = description[matches[-1].end():].strip()
        if len(remainder) > 3:
            return remainder

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
            # Skip pure numbers (they belong to amount columns)
            if AMOUNT_RE.match(w["text"].replace(",", "")):
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
    if template and template.get("col_map") and template.get("strategy"):
        saved_col_map = template["col_map"]
        preferred_strategy = template["strategy"]
        logger.info(
            f"Using saved template (id={template.get('id')}, strategy={preferred_strategy}, "
            f"col_map={saved_col_map})"
        )

        transactions = _extract_with_template(pdf_path, saved_col_map, preferred_strategy)
        if transactions:
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
    # Strategy 1: table-based extraction — run PyMuPDF and pdfplumber in parallel,
    # then use whichever yields more valid (non-merged) transactions.
    # PyMuPDF uses visual ruling-line detection so it captures the first data row
    # on every page; pdfplumber handles edge cases where PyMuPDF finds no tables.
    pymupdf_txns, pymupdf_col_map = _extract_from_pymupdf(pdf_path)
    try:
        pdfplumber_txns, pdfplumber_col_map = _extract_from_tables(pdf_path)
    except Exception as e:
        logger.warning(f"pdfplumber table extraction failed: {e}")
        pdfplumber_txns, pdfplumber_col_map = [], None

    pymupdf_ok = bool(pymupdf_txns) and not _has_merged_rows(pymupdf_txns)
    pdfplumber_ok = bool(pdfplumber_txns) and not _has_merged_rows(pdfplumber_txns)

    if pymupdf_ok or pdfplumber_ok:
        if pymupdf_ok and pdfplumber_ok:
            # Both found valid transactions — prefer whichever found more rows.
            # PyMuPDF typically wins by the number of first-row-per-page it captures.
            if len(pymupdf_txns) >= len(pdfplumber_txns):
                transactions = pymupdf_txns
                source = "pymupdf"
                detected_col_map = pymupdf_col_map
            else:
                transactions = pdfplumber_txns
                source = "pdfplumber"
                detected_col_map = pdfplumber_col_map
        elif pymupdf_ok:
            transactions = pymupdf_txns
            source = "pymupdf"
            detected_col_map = pymupdf_col_map
        else:
            transactions = pdfplumber_txns
            source = "pdfplumber"
            detected_col_map = pdfplumber_col_map

        logger.info(
            f"Strategy 1 ({source}): {len(transactions)} transactions "
            f"(pymupdf={len(pymupdf_txns)}, pdfplumber={len(pdfplumber_txns)})"
        )
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
    try:
        transactions = _extract_from_words(pdf_path)
        if transactions:
            meta = {"_extraction_meta": {"strategy": "words", "template_matched": False}}
            transactions.append(meta)
            return transactions
    except Exception as e:
        logger.warning(f"Word extraction failed: {e}")

    # Strategy 3: text-based line parsing
    try:
        transactions = _extract_from_text(pdf_path)
        if transactions:
            meta = {"_extraction_meta": {"strategy": "text", "template_matched": False}}
            transactions.append(meta)
        return transactions
    except Exception as e:
        logger.warning(f"Text extraction failed: {e}")
        return []


def _extract_with_template(
    pdf_path: Path, col_map: dict, strategy: str,
) -> list[dict]:
    """Extract transactions using a saved template's column mapping and strategy.

    This skips column detection entirely — the col_map is already known.
    Falls back to the full pipeline if the preferred strategy fails.
    """
    if strategy == "pymupdf":
        txns, _ = _extract_from_pymupdf(pdf_path, forced_col_map=col_map)
        return txns
    elif strategy == "pdfplumber":
        txns, _ = _extract_from_tables(pdf_path, forced_col_map=col_map)
        return txns
    elif strategy == "words":
        return _extract_from_words(pdf_path)
    elif strategy == "text":
        return _extract_from_text(pdf_path)
    return []


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


def _extract_from_tables(pdf_path: Path, forced_col_map: dict | None = None) -> list[dict]:
    """Extract transactions from structured PDF tables by mapping columns."""
    transactions = []
    col_map = forced_col_map  # Use template col_map if provided, else detect
    col_map_num_cols = 0  # Number of columns col_map was detected for

    if forced_col_map:
        logger.info(f"pdfplumber using forced col_map from template: {forced_col_map}")

    with pdfplumber.open(pdf_path) as pdf:
        for page_num, page in enumerate(pdf.pages, 1):
            tables = page.extract_tables()
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

                # If col_map was detected for a different column count, try re-detecting
                # (skip re-detection if using a forced col_map from template)
                if col_map is not None and num_cols != col_map_num_cols and not forced_col_map:
                    logger.info(f"Page {page_num}, table {ti}: col_map has {col_map_num_cols} cols but table has {num_cols} cols, re-detecting")
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
                        # Re-detection failed. If the table has MORE cols than col_map needs,
                        # still try processing with existing col_map (columns may just be offset).
                        # If fewer cols, skip — the existing col_map indices would be out of range.
                        max_col_idx = max(v for k, v in col_map.items() if isinstance(v, int))
                        if num_cols > max_col_idx:
                            logger.info(f"Page {page_num}, table {ti}: re-detection failed but table has enough cols ({num_cols} > max_idx {max_col_idx}), trying existing col_map")
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

                # Process ALL rows — no header skipping.
                # _row_to_transaction() validates the date column; header rows
                # (containing "Date" / "التاريخ" instead of an actual date) are
                # rejected by the date-pattern check and return None.
                # This eliminates false skips of first-row data on every page.
                page_txn_count = 0
                for ri, row in enumerate(cleaned):
                    txn = _row_to_transaction(row, col_map)
                    if txn:
                        # If description is empty or very short, try word-position recovery
                        if len(txn["description"]) < 3:
                            recovered = _recover_description_from_words(
                                page, ri, None, col_map, cleaned
                            )
                            if recovered:
                                txn["description"] = recovered
                                logger.debug(f"Page {page_num}: recovered description for row {ri}: {txn['description'][:60]}...")
                        transactions.append(txn)
                        page_txn_count += 1
                        # Log first 3 transactions per page for debugging
                        if page_txn_count <= 3:
                            logger.info(f"Page {page_num}, table {ti}, row {ri}: EXTRACTED txn #{len(transactions)}: date={txn['date']}, desc={txn['description'][:50]!r}, debit={txn['debit']}, credit={txn['credit']}, balance={txn['balance']}")
                    else:
                        # Log first few rejected rows per page too
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

        with pdfplumber.open(pdf_path) as pdf:
            # ── Pass 1: detect col_map ────────────────────────────────────
            for page_num, page in enumerate(pdf.pages, 1):
                text_tables = page.extract_tables({
                    "vertical_strategy": "text",
                    "horizontal_strategy": "text",
                })
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
                        if has_financial:
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
                    text_tables = page.extract_tables({
                        "vertical_strategy": "text",
                        "horizontal_strategy": "text",
                    })
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

            # Only process bands whose leftmost word is a date.
            first_text = line_words[0]["text"]
            if not any(p.match(first_text) for p in DATE_PATTERNS):
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

            candidate = {"date": date, "description": desc,
                         "debit": debit, "credit": credit, "balance": balance}

            # Skip if already captured.
            if _key(candidate) in captured_keys:
                continue

            missed.append(candidate)
            captured_keys.add(_key(candidate))  # Avoid duplicating within this scan

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
                    elif ri < 3:
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
        logger.debug(f"Row rejected (cell >2000 chars): {[c[:40] for c in row]}")
        return None

    # Date check first so we can skip non-transaction rows quickly.
    date = get("date")
    date = re.sub(r"\s*([/\-.])\s*", r"\1", date)
    date = re.sub(r"\s+", " ", date).strip()
    if not date or not any(p.search(date) for p in DATE_PATTERNS):
        logger.debug(f"Row rejected (no valid date): date_cell={date!r}, row={[c[:30] for c in row]}")
        return None

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
    description = re.sub(r"\s+", " ", get("description")).strip()
    desc_lower = description.lower()
    header_was_contaminated = (
        any(p.match(description) for p in _DESC_HEADER_RES)
        or bool(_IBAN_RE.search(description[:140]))
        or (
            len(description) > 80
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
                    # Use y-gap: if close to the last date line (<25px), it's continuation.
                    # Otherwise, it's a pending description for the next transaction.
                    # A wider gap (25px vs old 18px) catches multi-line descriptions
                    # that span 2-3 lines within a single table row.
                    is_continuation = (
                        transactions
                        and not pending_desc_lines
                        and last_date_y is not None
                        and (top - last_date_y) < 25
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

                # Clean page-header dumps from description but keep the transaction
                header_contaminated = any(p.match(desc) for p in _DESC_HEADER_RES)
                if header_contaminated:
                    stripped = _strip_page_header_prefix(desc)
                    desc = stripped if stripped else desc
                    # Don't skip — the row has valid date + amounts even if desc is empty

                # Description is kept exactly as extracted — no regex cleaning.

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
    # Common footer patterns — only match when the line is short (clearly metadata,
    # not a transaction description mentioning a bank name)
    if len(line) < 80:
        if ("regulated" in line_lower and "licensed" in line_lower):
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
    # Only match very short lines that are predominantly phone numbers
    if len(line) < 25:
        phone_match = re.search(r"\b(?:\+\d{1,3}\s?)?\d[\d\s\-]{7,}\d\b", line)
        if phone_match and (phone_match.end() - phone_match.start()) > len(line) * 0.5:
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
                    # Prepend accumulated description lines (but only if we've
                    # already seen at least one transaction — lines before the
                    # first transaction are page headers / account info, not
                    # description continuations)
                    if pending_desc_lines and transactions:
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

    # Filter out small plain integers from the leading edge of the trailing
    # cluster — they're likely part of the description (e.g. "SITE 48",
    # "FLOOR 3", "REF 12345").  Keep only amounts that look monetary:
    # have a decimal point, have commas, are negative, or are large (>= 1000).
    while len(trailing_amounts) > 1:
        first_val = trailing_amounts[0].group(1).replace(",", "")
        has_decimal = "." in first_val
        is_negative = first_val.startswith("-")
        is_large = abs(float(first_val)) >= 1000 if first_val.replace("-", "").replace(".", "").isdigit() else False
        if not has_decimal and not is_negative and not is_large:
            # Small plain integer — likely part of description, not an amount
            trailing_amounts.pop(0)
        else:
            break

    if not trailing_amounts:
        return None

    # Description is everything before the trailing amounts
    desc_end = trailing_amounts[0].start()
    description = rest[:desc_end].strip()
    description = re.sub(r"\s+", " ", description).strip()

    amount_strs = [m.group(1) for m in trailing_amounts]

    if len(amount_strs) == 1:
        val = float(amount_strs[0].replace(",", ""))
        if val < 0:
            return {"date": date, "description": description,
                    "debit": f"{abs(val):,.2f}", "credit": "", "balance": ""}
        elif prev_balance is not None:
            # With a previous balance, we can infer: if this positive amount
            # would make sense as a new balance (close to prev_balance), treat
            # it as a balance.  Otherwise treat it as a credit amount.
            # Heuristic: if it's within 2x of prev_balance, likely a balance.
            if prev_balance > 0 and 0.1 * prev_balance <= val <= 10 * prev_balance:
                return {"date": date, "description": description,
                        "debit": "", "credit": "", "balance": f"{val:,.2f}"}
            else:
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



