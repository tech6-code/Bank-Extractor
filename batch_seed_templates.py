"""
Batch seed templates into MySQL from all PDFs in a directory.

For each PDF:
1. Run extraction to get col_map + strategy
2. Generate layout fingerprint
3. Save template (skips duplicates via ON DUPLICATE KEY UPDATE)

Usage: python batch_seed_templates.py [directory]
"""

import sys
import time
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from extractor.pdf_parser import extract_transactions
from extractor.template_engine import (
    extract_layout_info,
    generate_fingerprint,
    save_extraction_template,
    DB_AVAILABLE,
)
from database.db import find_template_by_fingerprint


def seed_pdf(pdf_path: str) -> dict:
    """Process one PDF and attempt to save its template."""
    result = {
        "file": Path(pdf_path).name,
        "status": "skipped",
        "fingerprint": None,
        "strategy": None,
        "transactions": 0,
        "template_id": None,
        "error": None,
    }

    try:
        # Step 1: Extract transactions to get col_map + strategy
        raw = extract_transactions(pdf_path)
        txns = [t for t in raw if "_extraction_meta" not in t]
        meta = [t for t in raw if "_extraction_meta" in t]

        result["transactions"] = len(txns)

        if not txns or len(txns) < 3:
            result["status"] = "skipped (too few transactions)"
            return result

        if not meta:
            result["status"] = "skipped (no metadata)"
            return result

        m = meta[0]["_extraction_meta"]
        strategy = m.get("strategy", "unknown")
        col_map = m.get("col_map", {})
        result["strategy"] = strategy

        if not col_map:
            result["status"] = "skipped (no col_map)"
            return result

        # Step 2: Check if layout can be extracted
        layout = extract_layout_info(pdf_path)
        if not layout:
            result["status"] = "skipped (no layout detected)"
            return result

        fingerprint = generate_fingerprint(
            layout["header_texts"],
            layout["column_count"],
            layout["page_width"],
        )
        result["fingerprint"] = fingerprint[:16] + "..."

        # Step 3: Check if already exists
        existing = find_template_by_fingerprint(fingerprint)
        if existing:
            result["status"] = f"duplicate (id={existing['id']})"
            result["template_id"] = existing["id"]
            return result

        # Step 4: Save new template
        template_id = save_extraction_template(
            pdf_path=pdf_path,
            col_map=col_map,
            strategy=strategy,
            transactions_count=len(txns),
        )

        if template_id:
            result["status"] = "saved"
            result["template_id"] = template_id
        else:
            result["status"] = "save failed"

    except Exception as e:
        result["status"] = "error"
        result["error"] = str(e)[:80]
        traceback.print_exc()

    return result


def main():
    if not DB_AVAILABLE:
        print("ERROR: Database not available. Check MySQL/XAMPP is running and .env is configured.")
        sys.exit(1)

    directory = sys.argv[1] if len(sys.argv) > 1 else r"C:\Users\GC-IT-4\Documents\Bank Statement Module\Digital"

    pdf_files = sorted(Path(directory).glob("*.[pP][dD][fF]"))
    print(f"Found {len(pdf_files)} PDF files in {directory}")
    print(f"Database connected: {DB_AVAILABLE}\n")

    results = []
    saved = 0
    duplicates = 0
    skipped = 0
    errors = 0

    start_total = time.time()

    for i, pdf_path in enumerate(pdf_files, 1):
        start = time.time()
        print(f"[{i:3d}/{len(pdf_files)}] {pdf_path.name}...", end=" ", flush=True)

        r = seed_pdf(str(pdf_path))
        elapsed = round(time.time() - start, 1)
        results.append(r)

        if r["status"] == "saved":
            saved += 1
            print(f"SAVED (id={r['template_id']}, {r['strategy']}, {r['transactions']} txns) {elapsed}s")
        elif r["status"].startswith("duplicate"):
            duplicates += 1
            print(f"DUPLICATE {r['status']} {elapsed}s")
        elif r["status"] == "error":
            errors += 1
            print(f"ERROR: {r['error']} {elapsed}s")
        else:
            skipped += 1
            print(f"{r['status']} {elapsed}s")

    total_time = round(time.time() - start_total, 1)

    # Summary
    print(f"\n{'=' * 80}")
    print("TEMPLATE SEEDING RESULTS")
    print(f"{'=' * 80}")
    print(f"  Total PDFs processed: {len(results)}")
    print(f"  New templates saved:  {saved}")
    print(f"  Duplicates (already exist): {duplicates}")
    print(f"  Skipped:              {skipped}")
    print(f"  Errors:               {errors}")
    print(f"  Total time:           {total_time}s")

    # List saved templates
    if saved > 0:
        print(f"\nNew templates saved:")
        for r in results:
            if r["status"] == "saved":
                print(f"  ID={r['template_id']:>3} | {r['strategy']:<12} | {r['transactions']:>5} txns | {r['file']}")

    # List unique fingerprints found
    unique_fps = set()
    for r in results:
        if r["fingerprint"]:
            unique_fps.add(r["fingerprint"])
    print(f"\nUnique layout fingerprints found: {len(unique_fps)}")


if __name__ == "__main__":
    main()
