"""FastAPI backend for the Bank Statement Converter."""

from io import BytesIO
from dotenv import load_dotenv
load_dotenv()

import asyncio
import logging
import os
import tempfile
import time
import uuid
from collections import OrderedDict
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, UploadFile, HTTPException, Form, Body
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from starlette.background import BackgroundTask

import fitz
import pdfplumber
from extractor.pdf_parser import extract_transactions
from extractor.excel_writer import write_tables_to_excel_bytes
from extractor.template_engine import (
    match_template,
    save_extraction_template_detailed,
    detect_bank_name,
)
from extractor.balance_validator import validate_balances

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(level=getattr(logging, LOG_LEVEL, logging.INFO))
logging.getLogger("extractor.pdf_parser").setLevel(getattr(logging, LOG_LEVEL, logging.INFO))
logging.getLogger("pdfminer").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

APP_BASE_DIR = Path(__file__).resolve().parent
UPLOAD_TMP_ROOT = Path(os.getenv("BANK_EXTRACTOR_TMP_DIR", APP_BASE_DIR / ".tmp"))
UPLOAD_TMP_ROOT.mkdir(parents=True, exist_ok=True)

# Force Python/Starlette multipart temp files onto the workspace drive instead of
# the system temp directory. This avoids 400 "error parsing the body" failures
# when C:\ is full, because request-body spooling happens before route handlers run.
tempfile.tempdir = str(UPLOAD_TMP_ROOT)
os.environ["TMPDIR"] = str(UPLOAD_TMP_ROOT)
os.environ["TEMP"] = str(UPLOAD_TMP_ROOT)
os.environ["TMP"] = str(UPLOAD_TMP_ROOT)

TEMP_DIR = UPLOAD_TMP_ROOT / "bank-statement-extractor"
TEMP_DIR.mkdir(parents=True, exist_ok=True)

FILE_TTL_SECONDS = 3600  # 1 hour
CACHE_MAX_ENTRIES = int(os.getenv("CACHE_MAX_ENTRIES", "50"))
# Soft cap on the cumulative approximate byte size of cached extractions.
# Each entry's size is estimated cheaply (row count × per-row constant); when
# the total exceeds this we evict LRU entries until under cap.
CACHE_MAX_BYTES = int(os.getenv("CACHE_MAX_BYTES", str(256 * 1024 * 1024)))  # 256 MB
CORS_ORIGINS = os.getenv("CORS_ORIGINS", "http://localhost:5173").split(",")
MAX_FILE_SIZE = int(os.getenv("MAX_FILE_SIZE", str(100 * 1024 * 1024)))  # 100 MB
# Max seconds a single extraction can run before the request is failed with 504.
# Prevents a runaway PDF from holding a worker indefinitely. Default 10 min —
# enough for very large PDFs while still bounded; override in production via env.
EXTRACTION_TIMEOUT_SECONDS = int(os.getenv("EXTRACTION_TIMEOUT_SECONDS", "600"))


def _purge_stale_files() -> None:
    """Delete temp files older than FILE_TTL_SECONDS (runs on startup)."""
    cutoff = time.time() - FILE_TTL_SECONDS
    removed = sum(
        1 for f in TEMP_DIR.iterdir()
        if f.is_file() and f.stat().st_mtime < cutoff and not f.unlink()
    )
    if removed:
        logger.info("Purged %d stale temp file(s) on startup", removed)


def _cleanup_after_download(file_id: str) -> None:
    """Delete the PDF and XLSX for a file_id and evict from cache."""
    global _cache_total_bytes
    for suffix in (".pdf", "_unlocked.pdf", ".xlsx"):
        (TEMP_DIR / f"{file_id}{suffix}").unlink(missing_ok=True)
    if _cache.pop(file_id, None) is not None:
        _cache_total_bytes -= _cache_sizes.pop(file_id, 0)


@asynccontextmanager
async def lifespan(_: FastAPI):
    _purge_stale_files()
    yield


app = FastAPI(title="Bank Statement Converter API", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

COLUMNS = ["Date", "Description", "Debit", "Credit", "Balance"]

# In-memory LRU cache: file_id -> extracted transactions.
# Evicts oldest entries when either the entry count exceeds CACHE_MAX_ENTRIES
# or the approximate cumulative byte size exceeds CACHE_MAX_BYTES.
_cache: OrderedDict[str, list[dict]] = OrderedDict()
_cache_sizes: dict[str, int] = {}
_cache_total_bytes = 0
# Rough per-row memory estimate: 5 string fields × ~80 chars average + dict
# overhead. Cheap to compute and good enough for cache pressure decisions.
_PER_ROW_BYTES = 500


def _estimate_cache_entry_size(transactions: list[dict]) -> int:
    return max(len(transactions), 1) * _PER_ROW_BYTES


def _cache_put(file_id: str, transactions: list[dict]) -> None:
    """Add entry to cache, evicting oldest if over the count or byte limit."""
    global _cache_total_bytes
    # If replacing an existing entry, subtract its previous size first.
    if file_id in _cache_sizes:
        _cache_total_bytes -= _cache_sizes.pop(file_id)
    entry_size = _estimate_cache_entry_size(transactions)
    _cache[file_id] = transactions
    _cache.move_to_end(file_id)
    _cache_sizes[file_id] = entry_size
    _cache_total_bytes += entry_size
    while _cache and (
        len(_cache) > CACHE_MAX_ENTRIES or _cache_total_bytes > CACHE_MAX_BYTES
    ):
        evicted_id, _evicted_txns = _cache.popitem(last=False)
        _cache_total_bytes -= _cache_sizes.pop(evicted_id, 0)
        logger.debug("Cache evicted: %s", evicted_id)


def _separate_meta(transactions: list[dict]) -> tuple[list[dict], dict | None]:
    """Separate extraction metadata from transaction list.

    The extraction pipeline appends a metadata dict (with key "_extraction_meta")
    as the last element. This separates it from the actual transactions.
    """
    if not transactions:
        return transactions, None

    last = transactions[-1]
    if isinstance(last, dict) and "_extraction_meta" in last:
        return transactions[:-1], last["_extraction_meta"]
    return transactions, None


def _get_working_pdf_path(file_id: str) -> Path:
    """Return the unlocked temp PDF when available, otherwise the original upload."""
    unlocked_path = TEMP_DIR / f"{file_id}_unlocked.pdf"
    if unlocked_path.exists():
        return unlocked_path
    return TEMP_DIR / f"{file_id}.pdf"


def _normalize_export_rows(rows: list[dict]) -> list[dict]:
    """Keep only the exportable transaction fields as strings."""
    normalized = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        normalized.append({
            "date": str(row.get("date", "") or ""),
            "description": str(row.get("description", "") or ""),
            "debit": str(row.get("debit", "") or ""),
            "credit": str(row.get("credit", "") or ""),
            "balance": str(row.get("balance", "") or ""),
        })
    return normalized


def _prepare_working_pdf(pdf_path: Path, password: str | None, file_id: str) -> Path:
    """Return a readable PDF path, creating an unlocked working copy if needed."""
    unlocked_path = TEMP_DIR / f"{file_id}_unlocked.pdf"
    doc = fitz.open(str(pdf_path))
    try:
        if not doc.needs_pass:
            return pdf_path

        if not password:
            raise HTTPException(422, "This PDF is password protected. Please enter the PDF password.")

        if doc.authenticate(password) <= 0:
            raise HTTPException(422, "Incorrect PDF password.")

        unlocked_doc = fitz.open()
        try:
            unlocked_doc.insert_pdf(doc)
            unlocked_doc.save(str(unlocked_path))
        finally:
            unlocked_doc.close()

        return unlocked_path
    finally:
        doc.close()


@app.post("/api/extract")
async def extract_preview(file: UploadFile, password: str | None = Form(default=None)):
    """Upload a PDF and return extracted transaction data as JSON."""
    if not file.filename or not file.filename.lower().endswith(".pdf"):
        raise HTTPException(400, "Only PDF files are accepted.")

    file_id = uuid.uuid4().hex
    pdf_path = TEMP_DIR / f"{file_id}.pdf"
    TEMP_DIR.mkdir(exist_ok=True)

    # Stream the upload to disk in chunks. We never hold the entire payload in
    # memory and we abort as soon as the running total exceeds MAX_FILE_SIZE,
    # so an oversized upload cannot OOM the worker on the way in.
    total_bytes = 0
    chunk_size = 1024 * 1024  # 1 MB
    try:
        with pdf_path.open("wb") as out:
            while True:
                chunk = await file.read(chunk_size)
                if not chunk:
                    break
                total_bytes += len(chunk)
                if total_bytes > MAX_FILE_SIZE:
                    out.close()
                    pdf_path.unlink(missing_ok=True)
                    raise HTTPException(
                        413,
                        f"File too large. Maximum allowed size is {MAX_FILE_SIZE // (1024 * 1024)} MB.",
                    )
                out.write(chunk)
    except HTTPException:
        raise
    except Exception as e:
        pdf_path.unlink(missing_ok=True)
        raise HTTPException(500, f"Failed to receive upload: {e}")

    working_pdf_path = pdf_path

    try:
        logger.info(f"Extracting transactions from {file.filename} ({total_bytes} bytes)")
        working_pdf_path = _prepare_working_pdf(pdf_path, password, file_id)

        # ── Template matching ─────────────────────────────────────────────
        template = None
        try:
            template = match_template(str(working_pdf_path))
        except Exception as e:
            logger.warning(f"Template matching failed (continuing without): {e}")

        # ── Extraction ────────────────────────────────────────────────────
        # Run the synchronous CPU-heavy extractor in a worker thread so the
        # FastAPI event loop stays responsive to other requests, and bound it
        # with a hard timeout so a runaway PDF cannot block a worker forever.
        try:
            raw_transactions = await asyncio.wait_for(
                asyncio.to_thread(extract_transactions, str(working_pdf_path), template=template),
                timeout=EXTRACTION_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError:
            logger.error(f"Extraction timed out after {EXTRACTION_TIMEOUT_SECONDS}s for {file.filename}")
            raise HTTPException(
                504,
                f"Extraction took longer than {EXTRACTION_TIMEOUT_SECONDS}s and was cancelled. "
                "The PDF may be too large or complex.",
            )
        transactions, meta = _separate_meta(raw_transactions)
        logger.info(f"Extracted {len(transactions)} transactions")
    except HTTPException:
        logger.exception("Extraction failed")
        pdf_path.unlink(missing_ok=True)
        if working_pdf_path != pdf_path:
            working_pdf_path.unlink(missing_ok=True)
        raise
    except Exception as e:
        logger.exception("Extraction failed")
        pdf_path.unlink(missing_ok=True)
        if working_pdf_path != pdf_path:
            working_pdf_path.unlink(missing_ok=True)
        raise HTTPException(500, f"Failed to extract data: {e}")

    if not transactions:
        pdf_path.unlink(missing_ok=True)
        if working_pdf_path != pdf_path:
            working_pdf_path.unlink(missing_ok=True)
        raise HTTPException(422, "No transaction data found in the PDF. The parser could not detect structured tables or date-prefixed transaction lines.")

    # ── Save template if this was a new layout ────────────────────────────
    template_info = {}
    if meta:
        template_info["strategy"] = meta.get("strategy", "unknown")
        template_info["template_matched"] = meta.get("template_matched", False)

        if meta.get("template_matched"):
            template_info["template_id"] = meta.get("template_id")
            template_info["template_bank_name"] = meta.get("template_bank_name")
        else:
            # New layout — save as template for future reuse
            try:
                col_map = meta.get("col_map", {})
                strategy = meta.get("strategy", "unknown")
                save_result = save_extraction_template_detailed(
                    pdf_path=str(working_pdf_path),
                    col_map=col_map,
                    strategy=strategy,
                    transactions_count=len(transactions),
                )
                template_id = save_result.get("template_id")
                if template_id:
                    template_info["template_id"] = template_id
                    template_info["template_saved"] = True
                    bank_name = save_result.get("bank_name") or detect_bank_name(str(working_pdf_path))
                    if bank_name:
                        template_info["template_bank_name"] = bank_name
                    logger.info(f"New template saved (id={template_id}, bank={bank_name or 'unknown'})")
                else:
                    template_info["template_saved"] = False
                    reason = save_result.get("reason") or "unknown"
                    template_info["template_save_reason"] = reason
                    if save_result.get("headers"):
                        template_info["template_save_headers"] = save_result["headers"]
                    logger.info(f"Template not saved — reason: {reason}")
            except Exception as e:
                logger.warning(f"Template saving failed (non-critical): {e}")
                template_info["template_saved"] = False
                template_info["template_save_reason"] = "exception"

    # ── Balance validation ───────────────────────────────────────────────
    validation = {}
    try:
        validation = validate_balances(transactions)
    except Exception as e:
        logger.warning(f"Balance validation failed (non-critical): {e}")

    _cache_put(file_id, transactions)

    return {
        "file_id": file_id,
        "filename": file.filename,
        "columns": COLUMNS,
        "transactions": transactions,
        "total_rows": len(transactions),
        "template": template_info,
        "validation": validation,
    }


@app.post("/api/debug")
async def debug_pdf(file: UploadFile, password: str | None = Form(default=None)):
    """Upload a PDF and return raw extraction info for debugging."""
    if not file.filename or not file.filename.lower().endswith(".pdf"):
        raise HTTPException(400, "Only PDF files are accepted.")

    content = await file.read()
    debug_id = f"debug_{uuid.uuid4().hex}"
    tmp_path = TEMP_DIR / f"{debug_id}.pdf"
    tmp_path.write_bytes(content)
    working_pdf_path = tmp_path

    result = {"filename": file.filename, "size_bytes": len(content), "pages": []}

    try:
        working_pdf_path = _prepare_working_pdf(tmp_path, password, debug_id)
        with pdfplumber.open(working_pdf_path) as pdf:
            total_pages = len(pdf.pages)
            result["total_pages"] = total_pages

            # Inspect first 3 pages
            for i, page in enumerate(pdf.pages[:3]):
                page_info = {"page": i + 1, "tables": [], "text_lines": []}

                tables = page.extract_tables()
                if tables:
                    for ti, table in enumerate(tables):
                        rows_preview = []
                        for row in table[:5]:
                            rows_preview.append(
                                [(cell[:80] if cell else "") for cell in row]
                            )
                        page_info["tables"].append({
                            "table_index": ti,
                            "total_rows": len(table),
                            "num_columns": len(table[0]) if table else 0,
                            "first_5_rows": rows_preview,
                        })
                else:
                    text = page.extract_text()
                    if text:
                        lines = text.split("\n")
                        page_info["text_lines"] = lines[:15]
                    else:
                        page_info["text_lines"] = ["(no text extracted)"]

                result["pages"].append(page_info)
    except Exception as e:
        result["error"] = str(e)
    finally:
        tmp_path.unlink(missing_ok=True)
        if working_pdf_path != tmp_path:
            working_pdf_path.unlink(missing_ok=True)

    return result


@app.post("/api/download/{file_id}")
async def download_excel(
    file_id: str,
    payload: dict | None = Body(default=None),
    swap_row_indices: str = "",
):
    """Convert a previously uploaded PDF to Excel and return the file."""
    try:
        export_rows = payload.get("rows") if isinstance(payload, dict) else None
        if isinstance(export_rows, list):
            export_transactions = _normalize_export_rows(export_rows)
        else:
            transactions = _cache.get(file_id)
            if transactions is None:
            # Cache miss (e.g. server restarted) — re-extract from the saved PDF
                pdf_path = _get_working_pdf_path(file_id)
                if not pdf_path.exists():
                    raise HTTPException(404, "File not found. Please re-upload.")
                raw = extract_transactions(str(pdf_path))
                transactions, _ = _separate_meta(raw)
                _cache_put(file_id, transactions)

            swap_indices = {
                int(raw_idx)
                for raw_idx in swap_row_indices.split(",")
                if raw_idx.strip().isdigit()
            }
            export_transactions = [
                {
                    **txn,
                    "debit": txn["credit"],
                    "credit": txn["debit"],
                } if idx in swap_indices else txn
                for idx, txn in enumerate(transactions)
            ]

        # Build a single table: header row + data rows
        table = [COLUMNS] + [
            [txn["date"], txn["description"], txn["debit"], txn["credit"], txn["balance"]]
            for txn in export_transactions
        ]
        xlsx_bytes = write_tables_to_excel_bytes([table])
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, f"Failed to generate Excel: {e}")

    return StreamingResponse(
        BytesIO(xlsx_bytes),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": 'attachment; filename="bank_statement.xlsx"'},
        background=BackgroundTask(_cleanup_after_download, file_id),
    )


# ── Template Management Endpoints ────────────────────────────────────────────

@app.get("/api/templates")
async def list_templates():
    """List all saved templates."""
    try:
        from database.db import get_all_templates
        templates = get_all_templates()
        return {"templates": templates, "total": len(templates)}
    except Exception as e:
        logger.warning(f"Failed to list templates: {e}")
        return {"templates": [], "total": 0, "error": str(e)}


@app.delete("/api/templates/{template_id}")
async def remove_template(template_id: int):
    """Delete a template by ID."""
    try:
        from database.db import delete_template
        deleted = delete_template(template_id)
        if not deleted:
            raise HTTPException(404, "Template not found.")
        return {"deleted": True}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, f"Failed to delete template: {e}")


@app.patch("/api/templates/{template_id}")
async def update_template(template_id: int, body: dict):
    """Update a template's bank name."""
    bank_name = body.get("bank_name")
    if not bank_name:
        raise HTTPException(400, "bank_name is required.")
    try:
        from database.db import update_template_bank_name
        updated = update_template_bank_name(template_id, bank_name)
        if not updated:
            raise HTTPException(404, "Template not found.")
        return {"updated": True}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, f"Failed to update template: {e}")


if __name__ == "__main__":
    import uvicorn
    # Dev:  python server.py                    (or set APP_ENV=dev)
    # Prod: APP_ENV=prod APP_WORKERS=4 python server.py
    # On Windows, multiple workers are supported via uvicorn directly.
    is_dev = os.getenv("APP_ENV", "dev").lower() != "prod"
    workers = int(os.getenv("APP_WORKERS", "1"))
    uvicorn.run(
        "server:app",
        host=os.getenv("APP_HOST", "0.0.0.0"),
        port=int(os.getenv("APP_PORT", "8000")),
        reload=is_dev,
        workers=workers if not is_dev else None,
        timeout_keep_alive=int(os.getenv("APP_KEEPALIVE", "30")),
    )
