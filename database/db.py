"""MySQL database connection and template CRUD operations."""

import json
import logging
import os
from contextlib import contextmanager
from typing import Optional

import mysql.connector
from mysql.connector import Error

logger = logging.getLogger(__name__)

# ─── Connection Config ────────────────────────────────────────────────────────

DB_CONFIG = {
    "host": os.getenv("DB_HOST", "localhost"),
    "port": int(os.getenv("DB_PORT", "3306")),
    "user": os.getenv("DB_USER", "root"),
    "password": os.getenv("DB_PASSWORD", ""),
    "database": os.getenv("DB_NAME", "bank_extractor"),
}


@contextmanager
def get_connection():
    """Yield a MySQL connection that auto-closes on exit."""
    conn = None
    try:
        conn = mysql.connector.connect(**DB_CONFIG)
        yield conn
    except Error as e:
        logger.error("MySQL connection error: %s", e)
        raise
    finally:
        if conn and conn.is_connected():
            conn.close()


# ─── Template Operations ─────────────────────────────────────────────────────

def find_template_by_fingerprint(fingerprint: str) -> Optional[dict]:
    """Find a template by exact fingerprint match."""
    try:
        with get_connection() as conn:
            cursor = conn.cursor(dictionary=True)
            cursor.execute(
                "SELECT * FROM templates WHERE fingerprint = %s",
                (fingerprint,),
            )
            row = cursor.fetchone()
            if row:
                # Parse JSON fields
                row["col_map"] = json.loads(row["col_map"]) if isinstance(row["col_map"], str) else row["col_map"]
                row["header_texts"] = json.loads(row["header_texts"]) if isinstance(row["header_texts"], str) else row["header_texts"]
                if row.get("header_x_positions"):
                    row["header_x_positions"] = (
                        json.loads(row["header_x_positions"])
                        if isinstance(row["header_x_positions"], str)
                        else row["header_x_positions"]
                    )
            return row
    except Error as e:
        logger.warning("Failed to query template: %s", e)
        return None


def find_template_fuzzy(header_texts: list[str], column_count: int) -> Optional[dict]:
    """Find a template by fuzzy header matching when exact fingerprint fails.

    Compares normalized header texts with stored templates that have the same
    column count. Returns the best match if similarity >= 85%.
    """
    try:
        with get_connection() as conn:
            cursor = conn.cursor(dictionary=True)
            cursor.execute(
                "SELECT * FROM templates WHERE column_count = %s",
                (column_count,),
            )
            candidates = cursor.fetchall()

        if not candidates:
            return None

        normalized_input = {h.strip().lower() for h in header_texts if h.strip()}

        best_match = None
        best_score = 0.0

        for row in candidates:
            stored_headers = json.loads(row["header_texts"]) if isinstance(row["header_texts"], str) else row["header_texts"]
            normalized_stored = {h.strip().lower() for h in stored_headers if h.strip()}

            if not normalized_stored:
                continue

            # Jaccard similarity
            intersection = normalized_input & normalized_stored
            union = normalized_input | normalized_stored
            similarity = len(intersection) / len(union) if union else 0.0

            if similarity > best_score:
                best_score = similarity
                best_match = row

        if best_match and best_score >= 0.85:
            # Parse JSON fields
            best_match["col_map"] = json.loads(best_match["col_map"]) if isinstance(best_match["col_map"], str) else best_match["col_map"]
            best_match["header_texts"] = json.loads(best_match["header_texts"]) if isinstance(best_match["header_texts"], str) else best_match["header_texts"]
            if best_match.get("header_x_positions"):
                best_match["header_x_positions"] = (
                    json.loads(best_match["header_x_positions"])
                    if isinstance(best_match["header_x_positions"], str)
                    else best_match["header_x_positions"]
                )
            logger.info("Fuzzy template match found (similarity=%.2f)", best_score)
            return best_match

        return None
    except Error as e:
        logger.warning("Failed to fuzzy-match template: %s", e)
        return None


def find_templates_by_bank(bank_name: str) -> list[dict]:
    """Find all templates for a given bank name.

    Used as a third-tier fallback when exact fingerprint and fuzzy header
    matching both fail, but the bank name is detected from the PDF content.
    Returns all templates for that bank, sorted by success_count descending
    (most-used templates first).
    """
    if not bank_name:
        return []
    try:
        with get_connection() as conn:
            cursor = conn.cursor(dictionary=True)
            cursor.execute(
                "SELECT * FROM templates WHERE bank_name = %s ORDER BY success_count DESC",
                (bank_name,),
            )
            rows = cursor.fetchall()
            for row in rows:
                row["col_map"] = json.loads(row["col_map"]) if isinstance(row["col_map"], str) else row["col_map"]
                row["header_texts"] = json.loads(row["header_texts"]) if isinstance(row["header_texts"], str) else row["header_texts"]
                if row.get("header_x_positions"):
                    row["header_x_positions"] = (
                        json.loads(row["header_x_positions"])
                        if isinstance(row["header_x_positions"], str)
                        else row["header_x_positions"]
                    )
            return rows
    except Error as e:
        logger.warning("Failed to find templates by bank: %s", e)
        return []


def save_template(
    fingerprint: str,
    column_count: int,
    col_map: dict,
    header_texts: list[str],
    strategy: str,
    header_row_count: int = 1,
    header_x_positions: list[float] | None = None,
    page_width: float | None = None,
    page_height: float | None = None,
    date_format: str | None = None,
    bank_name: str | None = None,
) -> Optional[int]:
    """Save a new template after successful extraction. Returns the template ID."""
    try:
        with get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """INSERT INTO templates
                   (fingerprint, bank_name, column_count, col_map, header_texts,
                    header_x_positions, strategy, header_row_count,
                    page_width, page_height, date_format)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                   ON DUPLICATE KEY UPDATE
                    success_count = success_count + 1,
                    updated_at = CURRENT_TIMESTAMP""",
                (
                    fingerprint,
                    bank_name,
                    column_count,
                    json.dumps(col_map),
                    json.dumps(header_texts),
                    json.dumps(header_x_positions) if header_x_positions else None,
                    strategy,
                    header_row_count,
                    page_width,
                    page_height,
                    date_format,
                ),
            )
            conn.commit()
            template_id = cursor.lastrowid
            logger.info("Template saved (id=%s, fingerprint=%s)", template_id, fingerprint[:16])
            return template_id
    except Error as e:
        logger.warning("Failed to save template: %s", e)
        return None


def increment_success_count(template_id: int) -> None:
    """Increment the success_count when a template is reused successfully."""
    try:
        with get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "UPDATE templates SET success_count = success_count + 1 WHERE id = %s",
                (template_id,),
            )
            conn.commit()
    except Error as e:
        logger.warning("Failed to increment success count: %s", e)


def save_column_aliases(template_id: int, aliases: list[dict]) -> None:
    """Save column alias mappings for a template.

    aliases: [{"col_index": 0, "header_text": "Paid Out", "role": "debit", "confidence": 1.0}, ...]
    """
    try:
        with get_connection() as conn:
            cursor = conn.cursor()
            for alias in aliases:
                cursor.execute(
                    """INSERT INTO column_aliases (template_id, col_index, header_text, role, confidence)
                       VALUES (%s, %s, %s, %s, %s)
                       ON DUPLICATE KEY UPDATE
                        header_text = VALUES(header_text),
                        role = VALUES(role),
                        confidence = VALUES(confidence)""",
                    (
                        template_id,
                        alias["col_index"],
                        alias["header_text"],
                        alias["role"],
                        alias.get("confidence", 1.0),
                    ),
                )
            conn.commit()
    except Error as e:
        logger.warning("Failed to save column aliases: %s", e)


def get_all_templates() -> list[dict]:
    """Get all templates (for management UI)."""
    try:
        with get_connection() as conn:
            cursor = conn.cursor(dictionary=True)
            cursor.execute(
                "SELECT id, fingerprint, bank_name, column_count, strategy, "
                "header_texts, success_count, created_at, updated_at "
                "FROM templates ORDER BY updated_at DESC"
            )
            rows = cursor.fetchall()
            for row in rows:
                row["header_texts"] = json.loads(row["header_texts"]) if isinstance(row["header_texts"], str) else row["header_texts"]
                # Convert datetime to string for JSON serialization
                row["created_at"] = row["created_at"].isoformat() if row["created_at"] else None
                row["updated_at"] = row["updated_at"].isoformat() if row["updated_at"] else None
            return rows
    except Error as e:
        logger.warning("Failed to fetch templates: %s", e)
        return []


def delete_template(template_id: int) -> bool:
    """Delete a template by ID (cascades to column_aliases)."""
    try:
        with get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM templates WHERE id = %s", (template_id,))
            conn.commit()
            return cursor.rowcount > 0
    except Error as e:
        logger.warning("Failed to delete template: %s", e)
        return False


def update_template_bank_name(template_id: int, bank_name: str) -> bool:
    """Update the bank_name label for a template."""
    try:
        with get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "UPDATE templates SET bank_name = %s WHERE id = %s",
                (bank_name, template_id),
            )
            conn.commit()
            return cursor.rowcount > 0
    except Error as e:
        logger.warning("Failed to update bank name: %s", e)
        return False


def update_template_fingerprint(template_id: int, new_fingerprint: str) -> bool:
    """Update a template's fingerprint (used to migrate legacy fingerprints).

    Silently returns False if another row already has the new fingerprint
    (duplicate-key) — the caller should keep using the existing match.
    """
    try:
        with get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "UPDATE templates SET fingerprint = %s WHERE id = %s",
                (new_fingerprint, template_id),
            )
            conn.commit()
            return cursor.rowcount > 0
    except Error as e:
        logger.info("Fingerprint migration skipped for template %s: %s", template_id, e)
        return False
