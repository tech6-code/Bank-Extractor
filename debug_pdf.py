"""Debug script: drop a PDF path as argument to see raw pdfplumber output."""
import sys
import pdfplumber

pdf_path = sys.argv[1]
with pdfplumber.open(pdf_path) as pdf:
    for i, page in enumerate(pdf.pages[:3]):  # first 3 pages only
        print(f"\n{'='*60}")
        print(f"PAGE {i+1}")
        print(f"{'='*60}")

        tables = page.extract_tables()
        if tables:
            for ti, table in enumerate(tables):
                print(f"\n--- Table {ti+1} ({len(table)} rows) ---")
                for ri, row in enumerate(table[:5]):  # first 5 rows
                    print(f"  Row {ri}: {row}")
                if len(table) > 5:
                    print(f"  ... ({len(table) - 5} more rows)")
        else:
            print("\n--- No tables found, raw text: ---")
            text = page.extract_text()
            if text:
                for line in text.split("\n")[:15]:
                    print(f"  {line}")
            else:
                print("  (no text)")
