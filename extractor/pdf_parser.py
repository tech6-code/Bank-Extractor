import re
import logging
from collections import defaultdict
import pdfplumber
from pathlib import Path

logger = logging.getLogger(__name__)

# Flexible date patterns (support 1-2 digit day/month)
DATE_PATTERNS = [
    re.compile(r"\b(\d{1,2}[/\-.]\d{1,2}[/\-.]\d{4})\b"),
    re.compile(r"\b(\d{4}[/\-.]\d{1,2}[/\-.]\d{1,2})\b"),
    re.compile(r"\b(\d{1,2}\s+\w{3}\s+\d{4})\b"),
    re.compile(r"\b(\d{1,2}[/\-.]\w{3}[/\-.]\d{4})\b"),  # D-Mon-YYYY e.g. 4/Nov/2025
]
DATE_START_RE = re.compile(r"^\d{1,2}[/\-.](\d{1,2}|\w{3})[/\-.]\d{4}")
AMOUNT_RE = re.compile(r"-?[\d,]+\.\d{2}")
SIGNED_AMOUNT_RE = re.compile(r"[+\-]?[\d,]+\.\d{2}")
FLEX_AMOUNT_RE = re.compile(r"-?[\d,]+(?:\.\d{1,2})?$")
# For finding amounts in text lines (must be whitespace-bounded)
TEXT_AMOUNT_RE = re.compile(r"(?:^|\s)(-?[\d,]+(?:\.\d{1,2})?)(?=\s|$)")

# Header keywords
DATE_KEYWORDS = {"date", "txn date", "transaction date", "value date", "posting date", "trans date"}
DESC_KEYWORDS = {"description", "particulars", "narration", "details", "transaction details", "remarks"}
DEBIT_KEYWORDS = {"debit", "withdrawal", "withdrawals", "dr", "debit amount", "dr amount", "paid out"}
CREDIT_KEYWORDS = {"credit", "deposit", "deposits", "cr", "credit amount", "cr amount", "paid in"}
BALANCE_KEYWORDS = {"balance", "closing balance", "running balance", "available balance"}
AMOUNT_KEYWORDS = {"amount", "transaction amount", "txn amount"}

# Rows containing these phrases (case-insensitive) are not real transactions
SKIP_PHRASES = [
    "total records", "total debit", "total credit", "grand total",
    "opening balance", "closing balance", "balance brought", "balance carried",
    "statement summary", "account summary", "end of statement",
    "balance b/f", "balance c/f", "brought forward", "carried forward",
]


def extract_transactions(pdf_path: str) -> list[dict]:
    """Extract structured transactions from a bank statement PDF."""
    pdf_path = Path(pdf_path)
    if not pdf_path.exists():
        raise FileNotFoundError(f"PDF file not found: {pdf_path}")
    if pdf_path.suffix.lower() != ".pdf":
        raise ValueError(f"Not a PDF file: {pdf_path}")

    # Strategy 1: structured table extraction
    transactions = _extract_from_tables(pdf_path)
    if transactions:
        return transactions

    # Strategy 2: position-based word extraction (for PDFs without table structures)
    transactions = _extract_from_words(pdf_path)
    if transactions:
        return transactions

    # Strategy 3: text-based line parsing
    return _extract_from_text(pdf_path)


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

            for table in tables:
                cleaned = _clean_table(table)
                if not cleaned:
                    continue

                num_cols = len(cleaned[0])
                logger.debug(f"Page {page_num}: table with {len(cleaned)} rows, {num_cols} cols, first row: {cleaned[0][:3]}...")

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
        headers = [re.sub(r"[^\x00-\x7F]", "", cell).replace("\n", " ").lower().strip() for cell in row]

        col_map = {}
        for i, h in enumerate(headers):
            if not h:
                continue
            if h in DATE_KEYWORDS or "date" in h:
                col_map.setdefault("date", i)
            elif h in DESC_KEYWORDS or "description" in h or "particular" in h or "narration" in h:
                col_map.setdefault("description", i)
            elif h in DEBIT_KEYWORDS or "debit" in h or "withdraw" in h or "paid out" in h:
                col_map.setdefault("debit", i)
            elif h in CREDIT_KEYWORDS or "credit" in h or "deposit" in h or "paid in" in h:
                col_map.setdefault("credit", i)
            elif h in BALANCE_KEYWORDS or "balance" in h:
                col_map.setdefault("balance", i)
            elif h in AMOUNT_KEYWORDS or "amount" in h:
                col_map.setdefault("amount", i)

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
    x-coordinate order.
    """
    if num_table_cols < 3:
        return None

    words = page.extract_words()
    if not words:
        return None

    # Group words by y-position
    lines_by_top = defaultdict(list)
    for w in words:
        lines_by_top[round(w['top'])].append(w)

    # Find a line with multiple column header keywords
    header_kws = {
        "date", "description", "particulars", "narration", "transaction",
        "withdrawal", "withdrawals", "debit", "dr",
        "deposit", "deposits", "credit", "cr",
        "balance", "amount",
    }

    best_line = None
    best_score = 0
    for top, line_words in lines_by_top.items():
        score = sum(1 for w in line_words if w['text'].lower() in header_kws)
        if score > best_score:
            best_score = score
            best_line = line_words

    if best_score < 3 or best_line is None:
        return None

    # Sort header words by x-position and map to column indices
    header_words_sorted = sorted(best_line, key=lambda w: w['x0'])
    header_roles = []
    for w in header_words_sorted:
        text = w['text'].lower()
        if text in ('date',):
            header_roles.append(('date', w['x0']))
        elif text in ('description', 'particulars', 'narration', 'transaction'):
            header_roles.append(('description', w['x0']))
        elif text in ('reference', 'ref', 'cheque', 'check'):
            header_roles.append(('skip', w['x0']))  # Skip reference/cheque columns
        elif text in ('withdrawal', 'withdrawals', 'debit'):
            header_roles.append(('debit', w['x0']))
        elif text in ('deposit', 'deposits', 'credit'):
            header_roles.append(('credit', w['x0']))
        elif text == 'balance':
            header_roles.append(('balance', w['x0']))
        elif text == 'amount':
            header_roles.append(('amount', w['x0']))
        # Skip words like "No", "No." (part of "Reference No")

    # Map header roles to table column indices by position order
    # We expect the headers to appear in the same order as table columns
    if len(header_roles) < 3:
        return None

    col_map = {"header_row_count": 0}
    col_idx = 0
    for role, _ in header_roles:
        if col_idx >= num_table_cols:
            break
        if role != 'skip':
            col_map[role] = col_idx
        col_idx += 1

    if 'date' not in col_map:
        return None
    if 'balance' not in col_map and 'debit' not in col_map and 'credit' not in col_map:
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

    date = get("date")
    if not date or not any(p.search(date) for p in DATE_PATTERNS):
        return None

    description = get("description")

    # Skip summary/total rows that happen to contain a date
    row_text = " ".join(row).lower()
    if any(phrase in row_text for phrase in SKIP_PHRASES):
        return None

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

            for top in sorted(lines_by_top.keys()):
                line_words = sorted(lines_by_top[top], key=lambda w: w['x0'])
                if not line_words:
                    continue

                first_text = line_words[0]['text']
                # Check if this line starts with a date
                if not any(p.match(first_text) for p in DATE_PATTERNS):
                    # Non-date line — accumulate as description
                    line_text = " ".join(w['text'] for w in line_words)
                    if not _is_noise_line(line_text):
                        if transactions and not pending_desc_lines:
                            # Continuation of previous transaction
                            prev = transactions[-1]
                            prev["description"] = (prev["description"] + " " + line_text).strip()
                        else:
                            pending_desc_lines.append(line_text)
                    continue

                # Date line — extract column values by x-position
                date = first_text
                description_parts = []
                debit = ""
                credit = ""
                balance = ""

                for w in line_words[1:]:  # Skip the date word
                    x = w['x0']
                    text = w['text']

                    # Skip "Cr." / "Dr." suffixes on balance
                    if text in ("Cr.", "Dr.", "CR", "DR"):
                        continue

                    if x >= col_boundaries["balance_x"] - 15:
                        balance = text
                    elif col_boundaries.get("credit_x") and x >= col_boundaries["credit_x"] - 15:
                        if AMOUNT_RE.match(text.replace(",", "")):
                            credit = text
                        else:
                            description_parts.append(text)
                    elif col_boundaries.get("debit_x") and x >= col_boundaries["debit_x"] - 15:
                        if AMOUNT_RE.match(text.replace(",", "")):
                            debit = text
                        else:
                            description_parts.append(text)
                    else:
                        description_parts.append(text)

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

                # Skip summary rows
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

    Finds the header row by looking for a line with multiple column keywords,
    then extracts x-positions from that specific line.
    """
    # Group words by y-position
    lines_by_top = defaultdict(list)
    for w in words:
        lines_by_top[round(w['top'])].append(w)

    # Find the line with the most column header keywords
    best_line = None
    best_score = 0
    header_kws = {
        "date", "description", "particulars", "narration",
        "withdrawal", "withdrawals", "debit", "dr",
        "deposit", "deposits", "credit", "cr",
        "balance", "amount",
    }

    for top, line_words in lines_by_top.items():
        score = sum(1 for w in line_words if w['text'].lower() in header_kws)
        if score > best_score:
            best_score = score
            best_line = line_words

    if best_score < 3 or best_line is None:
        return None

    header_map = {}
    for w in best_line:
        text = w['text'].lower()
        x = w['x0']
        if text == 'date' and 'date' not in header_map:
            header_map['date'] = x
        elif text in ('description', 'particulars', 'narration'):
            header_map.setdefault('desc', x)
        elif text in ('withdrawal', 'withdrawals', 'debit'):
            header_map.setdefault('debit', x)
        elif text in ('deposit', 'deposits', 'credit'):
            header_map.setdefault('credit', x)
        elif text == 'balance':
            header_map.setdefault('balance', x)
        elif text == 'amount':
            header_map.setdefault('amount', x)

    if 'date' not in header_map:
        return None
    if 'balance' not in header_map and 'debit' not in header_map and 'credit' not in header_map:
        return None

    return {
        "date_x": header_map.get('date', 0),
        "desc_x": header_map.get('desc', header_map.get('date', 0) + 60),
        "debit_x": header_map.get('debit'),
        "credit_x": header_map.get('credit'),
        "balance_x": header_map.get('balance', 500),
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
    if "statement period" in line_lower or "date issued" in line_lower:
        return True
    if "account type" in line_lower or "account number" in line_lower:
        return True
    if "current account transactions" in line_lower:
        return True
    if line_lower.startswith("iban:") or line_lower.startswith("branch:") or line_lower.startswith("currency:"):
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
    if not DATE_START_RE.match(line):
        return None

    date_match = None
    for pattern in DATE_PATTERNS:
        date_match = pattern.match(line)
        if date_match:
            break

    if not date_match:
        return None

    date = date_match.group(1)
    rest = line[date_match.end():].strip()

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
