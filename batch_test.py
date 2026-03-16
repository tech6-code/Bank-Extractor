"""
Batch test all PDFs in a directory and produce a summary report.
Usage: python batch_test.py [directory]
"""

import sys
import os
import time
import traceback
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent))

from extractor.pdf_parser import extract_transactions
from extractor.balance_validator import validate_balances

import pdfplumber


def count_pages(pdf_path: str) -> int:
    """Count total pages in a PDF."""
    try:
        with pdfplumber.open(pdf_path) as pdf:
            return len(pdf.pages)
    except Exception:
        try:
            import fitz
            doc = fitz.open(pdf_path)
            count = len(doc)
            doc.close()
            return count
        except Exception:
            return -1


def test_pdf(pdf_path: str) -> dict:
    """Test extraction on a single PDF and return results."""
    result = {
        "file": Path(pdf_path).name,
        "pages": 0,
        "transactions": 0,
        "strategy": "N/A",
        "debits": 0,
        "credits": 0,
        "has_balance": 0,
        "balance_check": "N/A",
        "balance_pct": None,
        "mismatches": 0,
        "error": None,
        "time_sec": 0,
    }

    start = time.time()
    try:
        # Count pages
        result["pages"] = count_pages(pdf_path)

        # Extract
        raw = extract_transactions(pdf_path)
        txns = [t for t in raw if "_extraction_meta" not in t]
        meta = [t for t in raw if "_extraction_meta" in t]

        result["transactions"] = len(txns)
        if meta:
            m = meta[0]["_extraction_meta"]
            result["strategy"] = m.get("strategy", "unknown")

        result["debits"] = sum(1 for t in txns if t.get("debit"))
        result["credits"] = sum(1 for t in txns if t.get("credit"))
        result["has_balance"] = sum(1 for t in txns if t.get("balance"))

        # Balance validation
        if txns:
            v = validate_balances(txns)
            if v["validated_rows"] > 0:
                result["balance_check"] = f'{v["accuracy_pct"]}%'
                result["balance_pct"] = v["accuracy_pct"]
                result["mismatches"] = v["invalid_rows"]
            else:
                result["balance_check"] = "No balance data"

    except Exception as e:
        result["error"] = str(e)[:80]
        traceback.print_exc()

    result["time_sec"] = round(time.time() - start, 1)
    return result


def main():
    directory = sys.argv[1] if len(sys.argv) > 1 else r"C:\Users\GC-IT-4\Documents\Bank Statement Module\Digital"

    pdf_files = sorted(Path(directory).glob("*.[pP][dD][fF]"))
    print(f"Found {len(pdf_files)} PDF files in {directory}\n")

    results = []
    for i, pdf_path in enumerate(pdf_files, 1):
        print(f"[{i:3d}/{len(pdf_files)}] Testing: {pdf_path.name}...", end=" ", flush=True)
        r = test_pdf(str(pdf_path))
        results.append(r)

        status = "OK" if not r["error"] else f"ERROR: {r['error'][:40]}"
        print(f"{r['transactions']} txns, {r['pages']} pages, {r['strategy']}, "
              f"bal={r['balance_check']}, {r['time_sec']}s — {status}")

    # ── Summary ──────────────────────────────────────────────────────────
    print("\n" + "=" * 120)
    print("BATCH TEST RESULTS")
    print("=" * 120)

    # Table header
    print(f"{'#':>3} {'File':<45} {'Pages':>5} {'Txns':>6} {'Strategy':<12} "
          f"{'Debit':>6} {'Credit':>6} {'Bal':>5} {'Balance Check':>14} {'Mis':>4} {'Time':>5} {'Status':<10}")
    print("-" * 120)

    total_txns = 0
    total_errors = 0
    total_no_txns = 0
    total_perfect_balance = 0
    total_with_balance = 0
    strategies = {}

    for i, r in enumerate(results, 1):
        status = "OK" if not r["error"] and r["transactions"] > 0 else ("ERROR" if r["error"] else "NO DATA")
        if r["error"]:
            total_errors += 1
        if r["transactions"] == 0 and not r["error"]:
            total_no_txns += 1

        total_txns += r["transactions"]
        strategies[r["strategy"]] = strategies.get(r["strategy"], 0) + 1

        if r["balance_pct"] is not None:
            total_with_balance += 1
            if r["balance_pct"] == 100.0:
                total_perfect_balance += 1

        fname = r["file"][:44]
        print(f"{i:3d} {fname:<45} {r['pages']:>5} {r['transactions']:>6} {r['strategy']:<12} "
              f"{r['debits']:>6} {r['credits']:>6} {r['has_balance']:>5} {r['balance_check']:>14} "
              f"{r['mismatches']:>4} {r['time_sec']:>4.1f}s {status:<10}")

    print("-" * 120)
    print(f"\nSUMMARY:")
    print(f"  Total PDFs:              {len(results)}")
    print(f"  Total transactions:      {total_txns}")
    print(f"  Successful extractions:  {len(results) - total_errors - total_no_txns}")
    print(f"  No data extracted:       {total_no_txns}")
    print(f"  Errors:                  {total_errors}")
    print(f"  With balance validation: {total_with_balance}")
    print(f"  Perfect balance (100%):  {total_perfect_balance}")
    print(f"  Strategies used:         {strategies}")


if __name__ == "__main__":
    main()
