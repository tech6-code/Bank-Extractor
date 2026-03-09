from openpyxl import Workbook
from openpyxl.styles import Font, Alignment, Border, Side, PatternFill
from pathlib import Path


def write_tables_to_excel(
    tables: list[list[list[str]]],
    output_path: str,
    sheet_name: str = "Bank Statement",
) -> str:
    """Write extracted tables to an Excel file with formatting.

    Args:
        tables: List of tables (each table is a list of rows).
        output_path: Path for the output Excel file.
        sheet_name: Name of the worksheet.

    Returns:
        The absolute path of the created Excel file.
    """
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    wb = Workbook()
    ws = wb.active
    ws.title = sheet_name

    header_font = Font(bold=True, size=11)
    header_fill = PatternFill(start_color="4472C4", end_color="4472C4", fill_type="solid")
    header_font_white = Font(bold=True, size=11, color="FFFFFF")
    thin_border = Border(
        left=Side(style="thin"),
        right=Side(style="thin"),
        top=Side(style="thin"),
        bottom=Side(style="thin"),
    )

    current_row = 1
    for table_idx, table in enumerate(tables):
        if not table:
            continue

        for row_idx, row_data in enumerate(table):
            for col_idx, value in enumerate(row_data):
                cell = ws.cell(row=current_row, column=col_idx + 1, value=value)
                cell.border = thin_border
                cell.alignment = Alignment(wrap_text=True, vertical="top")

                # Style the first row of each table as a header
                if row_idx == 0:
                    cell.font = header_font_white
                    cell.fill = header_fill
                else:
                    cell.font = Font(size=11)

            current_row += 1

        # Add a blank row between tables
        current_row += 1

    # Auto-adjust column widths
    for col in ws.columns:
        max_length = 0
        col_letter = col[0].column_letter
        for cell in col:
            if cell.value:
                max_length = max(max_length, len(str(cell.value)))
        ws.column_dimensions[col_letter].width = min(max_length + 4, 50)

    wb.save(output_path)
    return str(output_path.resolve())
