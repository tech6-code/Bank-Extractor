"""FastAPI backend for the Bank Statement Converter."""

import logging
import tempfile
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, UploadFile, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from starlette.background import BackgroundTask

import pdfplumber
from extractor.pdf_parser import extract_transactions
from extractor.excel_writer import write_tables_to_excel

logging.basicConfig(level=logging.INFO)
logging.getLogger("extractor.pdf_parser").setLevel(logging.DEBUG)
logging.getLogger("pdfminer").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

TEMP_DIR = Path(tempfile.gettempdir()) / "bank-statement-extractor"
TEMP_DIR.mkdir(exist_ok=True)

FILE_TTL_SECONDS = 3600  # 1 hour


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
    allow_origins=["http://localhost:5173"],
    allow_methods=["*"],
    allow_headers=["*"],
)

COLUMNS = ["Date", "Description", "Debit", "Credit", "Balance"]

# In-memory cache: file_id -> extracted transactions
_cache: dict[str, list[dict]] = {}


@app.post("/api/extract")
async def extract_preview(file: UploadFile):
    """Upload a PDF and return extracted transaction data as JSON."""
    if not file.filename or not file.filename.lower().endswith(".pdf"):
        raise HTTPException(400, "Only PDF files are accepted.")

    file_id = uuid.uuid4().hex
    pdf_path = TEMP_DIR / f"{file_id}.pdf"

    content = await file.read()
    pdf_path.write_bytes(content)

    try:
        logger.info(f"Extracting transactions from {file.filename} ({len(content)} bytes)")
        transactions = extract_transactions(str(pdf_path))
        logger.info(f"Extracted {len(transactions)} transactions")
    except Exception as e:
        logger.exception("Extraction failed")
        pdf_path.unlink(missing_ok=True)
        raise HTTPException(500, f"Failed to extract data: {e}")

    if not transactions:
        pdf_path.unlink(missing_ok=True)
        raise HTTPException(422, "No transaction data found in the PDF. The parser could not detect structured tables or date-prefixed transaction lines.")

    _cache[file_id] = transactions

    return {
        "file_id": file_id,
        "filename": file.filename,
        "columns": COLUMNS,
        "transactions": transactions,
        "total_rows": len(transactions),
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
            transactions = extract_transactions(str(pdf_path))
            _cache[file_id] = transactions

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


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("server:app", host="0.0.0.0", port=8000, reload=True)
