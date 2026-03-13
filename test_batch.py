"""Batch test: extract transactions from all PDFs in a folder and print a report."""

import sys
import os
import time
from pathlib import Path

# Allow running from any directory
sys.path.insert(0, str(Path(__file__).parent))

from extractor.pdf_parser import extract_transactions

PDF_DIR = Path(r"C:\Users\GC-IT-4\Documents\Bank Statement Module\Digital")

def run_batch(pdf_dir: Path):
    pdfs = sorted(pdf_dir.glob("*.pdf"))
    if not pdfs:
        print(f"No PDF files found in: {pdf_dir}")
        return

    print(f"\nBatch test — {len(pdfs)} PDFs in:\n  {pdf_dir}\n")
    print(f"{'#':<4} {'Status':<10} {'Txns':>6}  {'Time(s)':>7}  File")
    print("-" * 90)

    results = []
    for i, pdf in enumerate(pdfs, 1):
        t0 = time.time()
        status = "OK"
        count = 0
        error = ""
        try:
            txns = extract_transactions(str(pdf))
            count = len(txns)
            if count == 0:
                status = "EMPTY"
        except Exception as e:
            status = "ERROR"
            error = str(e)[:80]
        elapsed = time.time() - t0

        flag = ""
        if status == "EMPTY":
            flag = "  ** no transactions found"
        elif status == "ERROR":
            flag = f"  ** {error}"

        print(f"{i:<4} {status:<10} {count:>6}  {elapsed:>7.2f}  {pdf.name}{flag}")
        results.append({"file": pdf.name, "status": status, "count": count, "error": error})

    # Summary
    ok    = [r for r in results if r["status"] == "OK"]
    empty = [r for r in results if r["status"] == "EMPTY"]
    err   = [r for r in results if r["status"] == "ERROR"]

    print("\n" + "=" * 90)
    print(f"SUMMARY: {len(pdfs)} files  |  OK: {len(ok)}  |  EMPTY: {len(empty)}  |  ERROR: {len(err)}")
    print(f"Total transactions extracted: {sum(r['count'] for r in results)}")

    if empty:
        print(f"\nEMPTY files ({len(empty)}):")
        for r in empty:
            print(f"  - {r['file']}")

    if err:
        print(f"\nERROR files ({len(err)}):")
        for r in err:
            print(f"  - {r['file']}: {r['error']}")

    print()


if __name__ == "__main__":
    import logging
    # Suppress verbose extraction logs during batch run
    logging.basicConfig(level=logging.WARNING)
    logging.getLogger("extractor.pdf_parser").setLevel(logging.WARNING)
    logging.getLogger("pdfminer").setLevel(logging.WARNING)

    run_batch(PDF_DIR)
