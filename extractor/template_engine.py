"""Template fingerprinting, matching, and saving for bank statement PDFs.

This module bridges the extraction pipeline (pdf_parser.py) with the database
(database/db.py). It generates layout fingerprints from PDF structure, checks
for matching templates, and saves new templates after successful extraction.
"""

import hashlib
import logging
import re
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# Try to import DB functions — template features are disabled if MySQL is unavailable
try:
    from database.db import (
        find_template_by_fingerprint,
        find_template_fuzzy,
        save_template,
        save_column_aliases,
        increment_success_count,
    )
    DB_AVAILABLE = True
except Exception:
    DB_AVAILABLE = False
    logger.info("Database not available — template matching disabled")


def generate_fingerprint(
    header_texts: list[str],
    column_count: int,
    page_width: float,
) -> str:
    """Generate a SHA-256 fingerprint from the PDF layout characteristics.

    The fingerprint is based on:
    - Normalized, sorted column header texts (lowercase, stripped)
    - Number of columns
    - Page width (rounded to int, to handle minor float differences)

    Two PDFs from the same bank will produce the same fingerprint because
    banks use identical column headers and page dimensions.
    """
    # Normalize: lowercase, strip whitespace, remove non-ASCII, sort
    normalized = sorted(
        re.sub(r"[^\x00-\x7F]", "", h).strip().lower()
        for h in header_texts
        if h and h.strip()
    )

    raw = f"{column_count}|{int(page_width)}|{'|'.join(normalized)}"
    return hashlib.sha256(raw.encode()).hexdigest()


def extract_layout_info(pdf_path: str | Path) -> Optional[dict]:
    """Extract layout information from the first page of a PDF.

    Returns a dict with:
    - header_texts: list of detected header strings
    - header_x_positions: list of x-coordinates for each header
    - column_count: number of columns detected
    - page_width: page width in points
    - page_height: page height in points
    - raw_table: first table data (for fallback column detection)

    Returns None if no table structure is found on the first page.
    """
    # Try PyMuPDF first (visual table detection)
    layout = _extract_layout_pymupdf(pdf_path)
    if layout:
        return layout

    # Fallback to pdfplumber (lines strategy)
    layout = _extract_layout_pdfplumber(pdf_path)
    if layout:
        return layout

    # Last resort: pdfplumber text strategy (for PDFs with no ruling lines)
    layout = _extract_layout_pdfplumber_text(pdf_path)
    if layout:
        return layout

    return None


def _extract_layout_pymupdf(pdf_path: str | Path) -> Optional[dict]:
    """Extract layout info using PyMuPDF's visual table finder."""
    try:
        import fitz
    except ImportError:
        return None

    try:
        doc = fitz.open(str(pdf_path))
        if len(doc) == 0:
            doc.close()
            return None

        page = doc[0]
        page_width = page.rect.width
        page_height = page.rect.height

        tabs = page.find_tables()
        if not tabs or len(tabs.tables) == 0:
            doc.close()
            return None

        # Use the largest table on the first page
        best_table = max(tabs.tables, key=lambda t: len(t.extract()))
        table_data = best_table.extract()

        if not table_data or len(table_data) < 2:
            doc.close()
            return None

        column_count = len(table_data[0])

        # Extract headers from first few rows
        header_texts = []
        header_row_idx = 0
        for idx in range(min(3, len(table_data))):
            row = table_data[idx]
            cleaned = [
                re.sub(r"[^\x00-\x7F]", "", str(cell or "")).replace("\n", " ").strip()
                for cell in row
            ]
            non_empty = sum(1 for c in cleaned if c)
            if non_empty >= 2:
                header_texts = cleaned
                header_row_idx = idx
                break

        if not header_texts:
            doc.close()
            return None

        # Get x-positions from table column boundaries
        header_x_positions = []
        if hasattr(best_table, 'cells') and best_table.cells:
            seen_cols = {}
            for cell in best_table.cells:
                # cell is (x0, y0, x1, y1, row, col, ...)
                if len(cell) >= 6:
                    col_idx = cell[5] if len(cell) > 5 else cell[4]
                    if col_idx not in seen_cols:
                        seen_cols[col_idx] = cell[0]
            header_x_positions = [seen_cols.get(i, 0) for i in range(column_count)]

        if not header_x_positions:
            # Fallback: evenly spaced based on page width
            header_x_positions = [
                (page_width / column_count) * i for i in range(column_count)
            ]

        doc.close()

        return {
            "header_texts": header_texts,
            "header_x_positions": header_x_positions,
            "column_count": column_count,
            "page_width": page_width,
            "page_height": page_height,
            "header_row_index": header_row_idx,
            "source": "pymupdf",
        }
    except Exception as e:
        logger.debug("PyMuPDF layout extraction failed: %s", e)
        return None


def _extract_layout_pdfplumber(pdf_path: str | Path) -> Optional[dict]:
    """Extract layout info using pdfplumber."""
    import pdfplumber

    try:
        with pdfplumber.open(str(pdf_path)) as pdf:
            if not pdf.pages:
                return None

            page = pdf.pages[0]
            page_width = page.width
            page_height = page.height

            tables = page.extract_tables()
            if not tables:
                return None

            # Use the largest table
            table = max(tables, key=len)
            if len(table) < 2:
                return None

            column_count = len(table[0])

            # Extract headers
            header_texts = []
            for idx in range(min(3, len(table))):
                row = table[idx]
                cleaned = [
                    re.sub(r"[^\x00-\x7F]", "", str(cell or "")).replace("\n", " ").strip()
                    for cell in row
                ]
                non_empty = sum(1 for c in cleaned if c)
                if non_empty >= 2:
                    header_texts = cleaned
                    break

            if not header_texts:
                return None

            # Get x-positions from words on the page
            words = page.extract_words()
            header_x_positions = []
            for h_text in header_texts:
                if not h_text:
                    header_x_positions.append(0)
                    continue
                # Find the word that best matches this header
                first_word = h_text.split()[0].lower() if h_text.split() else ""
                matched_x = 0
                for w in words:
                    if w["text"].lower().startswith(first_word[:4]) if len(first_word) >= 4 else w["text"].lower() == first_word:
                        matched_x = w["x0"]
                        break
                header_x_positions.append(matched_x)

            return {
                "header_texts": header_texts,
                "header_x_positions": header_x_positions,
                "column_count": column_count,
                "page_width": page_width,
                "page_height": page_height,
                "header_row_index": 0,
                "source": "pdfplumber",
            }
    except Exception as e:
        logger.debug("pdfplumber layout extraction failed: %s", e)
        return None


def _extract_layout_pdfplumber_text(pdf_path: str | Path) -> Optional[dict]:
    """Extract layout info using pdfplumber text strategy.

    For PDFs with no ruling lines (e.g. Wio Bank), the default pdfplumber
    strategy fails. This uses the text strategy which detects column
    boundaries from character positions. Scans all pages to find one
    with recognizable column headers.
    """
    import pdfplumber
    from collections import defaultdict

    try:
        with pdfplumber.open(str(pdf_path)) as pdf:
            if not pdf.pages:
                return None

            page_width = pdf.pages[0].width
            page_height = pdf.pages[0].height

            # Scan pages to find column headers from word positions
            for page in pdf.pages[:5]:
                words = page.extract_words()
                if not words:
                    continue

                # Group words by y-position, merge nearby lines
                lines_by_top: dict[int, list] = defaultdict(list)
                for w in words:
                    lines_by_top[round(w["top"])].append(w)

                sorted_tops = sorted(lines_by_top.keys())
                merged_bands: list[list] = []
                for top in sorted_tops:
                    if merged_bands and abs(top - merged_bands[-1][0][0]) <= 5:
                        merged_bands[-1].extend(
                            [(top, w) for w in lines_by_top[top]]
                        )
                    else:
                        merged_bands.append(
                            [(top, w) for w in lines_by_top[top]]
                        )

                # Import scoring function
                from extractor.pdf_parser import _score_header_line, classify_column_header

                for band in merged_bands:
                    all_words = [w for _, w in band]
                    sorted_lw = sorted(all_words, key=lambda w: w["x0"])
                    score, col_roles = _score_header_line(sorted_lw)

                    if score < 2:
                        continue

                    unique_roles = {r for r, _ in col_roles if r not in ("skip", "unknown")}
                    if "date" not in unique_roles:
                        continue

                    # Build header texts from word positions (group nearby)
                    header_texts = []
                    header_x_positions = []
                    for w in sorted_lw:
                        if not header_x_positions or abs(w["x0"] - header_x_positions[-1]) > 15:
                            header_texts.append(w["text"])
                            header_x_positions.append(w["x0"])
                        else:
                            # Merge with previous (e.g. "Ref." + "Number")
                            header_texts[-1] += " " + w["text"]

                    if len(header_texts) < 3:
                        continue

                    return {
                        "header_texts": header_texts,
                        "header_x_positions": header_x_positions,
                        "column_count": len(header_texts),
                        "page_width": page_width,
                        "page_height": page_height,
                        "header_row_index": 0,
                        "source": "pdfplumber_text",
                    }

        return None
    except Exception as e:
        logger.debug("pdfplumber text layout extraction failed: %s", e)
        return None


def match_template(pdf_path: str | Path) -> Optional[dict]:
    """Try to find a matching template for the given PDF.

    Returns the template dict if found, None otherwise.
    The template dict includes 'col_map', 'strategy', 'header_row_count', etc.
    """
    if not DB_AVAILABLE:
        return None

    layout = extract_layout_info(pdf_path)
    if not layout:
        return None

    fingerprint = generate_fingerprint(
        layout["header_texts"],
        layout["column_count"],
        layout["page_width"],
    )

    # Try exact match first
    template = find_template_by_fingerprint(fingerprint)
    if template:
        logger.info(
            "Template matched (exact, id=%s, bank=%s, uses=%d)",
            template["id"],
            template.get("bank_name", "unknown"),
            template["success_count"],
        )
        increment_success_count(template["id"])
        return template

    # Try fuzzy match
    template = find_template_fuzzy(layout["header_texts"], layout["column_count"])
    if template:
        logger.info(
            "Template matched (fuzzy, id=%s, bank=%s)",
            template["id"],
            template.get("bank_name", "unknown"),
        )
        increment_success_count(template["id"])
        return template

    logger.info("No matching template found — will run full extraction pipeline")
    return None


def save_extraction_template(
    pdf_path: str | Path,
    col_map: dict,
    strategy: str,
    transactions_count: int,
) -> Optional[int]:
    """Save a template after a successful extraction.

    Called by the extraction pipeline when no template was matched but
    extraction succeeded. The layout is fingerprinted and stored for
    future reuse.

    Returns the template ID if saved, None otherwise.
    """
    if not DB_AVAILABLE:
        return None

    # Only save if we got a meaningful number of transactions
    if transactions_count < 3:
        logger.debug("Too few transactions (%d) to save template", transactions_count)
        return None

    layout = extract_layout_info(pdf_path)
    if not layout:
        logger.debug("Could not extract layout info for template saving")
        return None

    fingerprint = generate_fingerprint(
        layout["header_texts"],
        layout["column_count"],
        layout["page_width"],
    )

    # Clean col_map for storage (remove non-serializable items)
    clean_col_map = {
        k: v for k, v in col_map.items()
        if k in ("date", "description", "debit", "credit", "balance", "amount",
                  "header_row_count", "unsigned_is_debit")
    }

    template_id = save_template(
        fingerprint=fingerprint,
        column_count=layout["column_count"],
        col_map=clean_col_map,
        header_texts=layout["header_texts"],
        strategy=strategy,
        header_row_count=clean_col_map.get("header_row_count", 1),
        header_x_positions=layout.get("header_x_positions"),
        page_width=layout.get("page_width"),
        page_height=layout.get("page_height"),
    )

    if template_id:
        # Save column aliases for enriching future header classification
        aliases = []
        for i, h_text in enumerate(layout["header_texts"]):
            if h_text and h_text.strip():
                # Find what role this column was assigned
                for role, col_idx in clean_col_map.items():
                    if isinstance(col_idx, int) and col_idx == i:
                        aliases.append({
                            "col_index": i,
                            "header_text": h_text.strip(),
                            "role": role,
                            "confidence": 1.0,
                        })
                        break
        if aliases:
            save_column_aliases(template_id, aliases)

        logger.info(
            "New template saved (id=%s, cols=%d, strategy=%s, headers=%s)",
            template_id,
            layout["column_count"],
            strategy,
            layout["header_texts"],
        )

    return template_id
