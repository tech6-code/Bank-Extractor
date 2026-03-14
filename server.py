"""FastAPI backend for the Bank Statement Converter."""

from dotenv import load_dotenv
load_dotenv()

import logging
import os
import tempfile
import time
import uuid
from collections import OrderedDict
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, UploadFile, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from starlette.background import BackgroundTask

import pdfplumber
from extractor.pdf_parser import extract_transactions
from extractor.excel_writer import write_tables_to_excel
from extractor.template_engine import match_template, save_extraction_template

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(level=getattr(logging, LOG_LEVEL, logging.INFO))
logging.getLogger("extractor.pdf_parser").setLevel(getattr(logging, LOG_LEVEL, logging.INFO))
logging.getLogger("pdfminer").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

TEMP_DIR = Path(tempfile.gettempdir()) / "bank-statement-extractor"
TEMP_DIR.mkdir(exist_ok=True)

FILE_TTL_SECONDS = 3600  # 1 hour
CACHE_MAX_ENTRIES = int(os.getenv("CACHE_MAX_ENTRIES", "50"))
CORS_ORIGINS = os.getenv("CORS_ORIGINS", "http://localhost:5173").split(",")
MAX_FILE_SIZE = int(os.getenv("MAX_FILE_SIZE", str(100 * 1024 * 1024)))  # 100 MB


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
    for ext in (".pdf", ".xlsx"):
        (TEMP_DIR / f"{file_id}{ext}").unlink(missing_ok=True)
    _cache.pop(file_id, None)


@asynccontextmanager
async def lifespan(_: FastAPI):
    _purge_stale_files()
    yield


app = FastAPI(title="Bank Statement Converter API", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_methods=["*"],
    allow_headers=["*"],
)

COLUMNS = ["Date", "Description", "Debit", "Credit", "Balance"]

# In-memory LRU cache: file_id -> extracted transactions (evicts oldest when full)
_cache: OrderedDict[str, list[dict]] = OrderedDict()


def _cache_put(file_id: str, transactions: list[dict]) -> None:
    """Add entry to cache, evicting oldest if over limit."""
    _cache[file_id] = transactions
    _cache.move_to_end(file_id)
    while len(_cache) > CACHE_MAX_ENTRIES:
        evicted_id, _ = _cache.popitem(last=False)
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


@app.post("/api/extract")
async def extract_preview(file: UploadFile):
    """Upload a PDF and return extracted transaction data as JSON."""
    if not file.filename or not file.filename.lower().endswith(".pdf"):
        raise HTTPException(400, "Only PDF files are accepted.")

    file_id = uuid.uuid4().hex
    pdf_path = TEMP_DIR / f"{file_id}.pdf"

    content = await file.read()
    if len(content) > MAX_FILE_SIZE:
        raise HTTPException(413, f"File too large. Maximum allowed size is {MAX_FILE_SIZE // (1024 * 1024)} MB.")
    pdf_path.write_bytes(content)

    try:
        logger.info(f"Extracting transactions from {file.filename} ({len(content)} bytes)")

        # ── Template matching ─────────────────────────────────────────────
        template = None
        try:
            template = match_template(str(pdf_path))
        except Exception as e:
            logger.warning(f"Template matching failed (continuing without): {e}")

        # ── Extraction ────────────────────────────────────────────────────
        raw_transactions = extract_transactions(str(pdf_path), template=template)
        transactions, meta = _separate_meta(raw_transactions)
        logger.info(f"Extracted {len(transactions)} transactions")
    except Exception as e:
        logger.exception("Extraction failed")
        pdf_path.unlink(missing_ok=True)
        raise HTTPException(500, f"Failed to extract data: {e}")

    if not transactions:
        pdf_path.unlink(missing_ok=True)
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
                template_id = save_extraction_template(
                    pdf_path=str(pdf_path),
                    col_map=col_map,
                    strategy=strategy,
                    transactions_count=len(transactions),
                )
                if template_id:
                    template_info["template_id"] = template_id
                    template_info["template_saved"] = True
                    logger.info(f"New template saved (id={template_id})")
            except Exception as e:
                logger.warning(f"Template saving failed (non-critical): {e}")

    _cache_put(file_id, transactions)

    return {
        "file_id": file_id,
        "filename": file.filename,
        "columns": COLUMNS,
        "transactions": transactions,
        "total_rows": len(transactions),
        "template": template_info,
    }


@app.post("/api/debug")
async def debug_pdf(file: UploadFile):
    """Upload a PDF and return raw extraction info for debugging."""
    if not file.filename or not file.filename.lower().endswith(".pdf"):
        raise HTTPException(400, "Only PDF files are accepted.")

    content = await file.read()
    tmp_path = TEMP_DIR / f"debug_{uuid.uuid4().hex}.pdf"
    tmp_path.write_bytes(content)

    result = {"filename": file.filename, "size_bytes": len(content), "pages": []}

    try:
        with pdfplumber.open(tmp_path) as pdf:
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

    return result


@app.post("/api/download/{file_id}")
async def download_excel(file_id: str):
    """Convert a previously uploaded PDF to Excel and return the file."""
    xlsx_path = TEMP_DIR / f"{file_id}.xlsx"

    try:
        transactions = _cache.get(file_id)
        if transactions is None:
            # Cache miss (e.g. server restarted) — re-extract from the saved PDF
            pdf_path = TEMP_DIR / f"{file_id}.pdf"
            if not pdf_path.exists():
                raise HTTPException(404, "File not found. Please re-upload.")
            raw = extract_transactions(str(pdf_path))
            transactions, _ = _separate_meta(raw)
            _cache_put(file_id, transactions)

        # Build a single table: header row + data rows
        table = [COLUMNS] + [
            [txn["date"], txn["description"], txn["debit"], txn["credit"], txn["balance"]]
            for txn in transactions
        ]
        write_tables_to_excel([table], str(xlsx_path))
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, f"Failed to generate Excel: {e}")

    return FileResponse(
        path=str(xlsx_path),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        filename="bank_statement.xlsx",
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
    uvicorn.run("server:app", host="0.0.0.0", port=8000, reload=True)
