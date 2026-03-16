"""Balance validation for extracted bank statement transactions.

Verifies that consecutive transactions follow the balance equation:
    previous_balance ± transaction_amount = current_balance

Returns a validation summary with accuracy percentage and mismatch details.
"""

import logging
import re
from typing import Optional

logger = logging.getLogger(__name__)

# Tolerance for floating-point comparison (0.01 = 1 cent)
TOLERANCE = 0.015


def _parse_amount(value: str) -> Optional[float]:
    """Parse a monetary string into a float. Returns None if unparseable."""
    if not value or not value.strip():
        return None
    # Remove commas and whitespace
    cleaned = value.strip().replace(",", "")
    # Handle parentheses-style negatives: (500.00) -> -500.00
    m = re.match(r"^\((.+)\)$", cleaned)
    if m:
        cleaned = "-" + m.group(1)
    try:
        return float(cleaned)
    except ValueError:
        return None


def _validate_in_order(transactions: list[dict], direction: str) -> dict:
    """Run balance validation on transactions in the given chronological order.

    Args:
        transactions: Original transaction list (as extracted).
        direction: "ascending" (oldest-first) or "descending" (newest-first).
                   If descending, the list is reversed to process oldest-first.

    Returns:
        Validation result dict.
    """
    total = len(transactions)
    ordered = transactions if direction == "ascending" else list(reversed(transactions))

    validated = 0
    valid = 0
    mismatches = []
    skipped = 0
    prev_balance: Optional[float] = None

    # Per-row variance keyed by original transaction index
    row_variances: dict[int, float] = {}

    for i, txn in enumerate(ordered):
        original_idx = i if direction == "ascending" else (total - 1 - i)
        bal = _parse_amount(txn.get("balance", ""))

        if bal is None:
            skipped += 1
            continue

        if prev_balance is None:
            # First row with a balance — can't validate, just record it
            prev_balance = bal
            skipped += 1
            row_variances[original_idx] = 0.0
            continue

        # Parse debit and credit
        debit = _parse_amount(txn.get("debit", ""))
        credit = _parse_amount(txn.get("credit", ""))

        debit_val = abs(debit) if debit is not None else 0.0
        credit_val = abs(credit) if credit is not None else 0.0

        expected = round(prev_balance - debit_val + credit_val, 2)
        actual = round(bal, 2)
        difference = round(actual - expected, 2)

        validated += 1

        # Store variance for this row (0.0 if within tolerance)
        if abs(difference) <= TOLERANCE:
            valid += 1
            row_variances[original_idx] = 0.0
        else:
            row_variances[original_idx] = difference
            mismatches.append({
                "row_index": original_idx,
                "date": txn.get("date", ""),
                "description": txn.get("description", "")[:60],
                "expected_balance": f"{expected:,.2f}",
                "actual_balance": txn.get("balance", ""),
                "difference": f"{abs(difference):,.2f}",
            })

        prev_balance = bal

    accuracy = round((valid / validated) * 100, 2) if validated > 0 else 0.0

    return {
        "total_rows": total,
        "validated_rows": validated,
        "valid_rows": valid,
        "invalid_rows": len(mismatches),
        "skipped_rows": skipped,
        "accuracy_pct": accuracy,
        "direction": direction,
        "mismatches": mismatches,
        "row_variances": row_variances,
    }


def validate_balances(transactions: list[dict]) -> dict:
    """Validate balance consistency across consecutive transactions.

    Tries both ascending (oldest-first) and descending (newest-first)
    orderings and returns whichever produces higher accuracy. This avoids
    fragile direction-detection heuristics — the correct direction will
    naturally yield valid balance equations.

    Args:
        transactions: List of transaction dicts with keys:
            date, description, debit, credit, balance

    Returns:
        Dict with validation summary:
        {
            "total_rows": int,
            "validated_rows": int,
            "valid_rows": int,
            "invalid_rows": int,
            "skipped_rows": int,
            "accuracy_pct": float,
            "direction": str,
            "mismatches": [{"row_index": int, "date": str, ...}, ...]
        }
    """
    if not transactions:
        return _empty_result()

    # Try both directions, pick whichever scores higher
    asc_result = _validate_in_order(transactions, "ascending")
    desc_result = _validate_in_order(transactions, "descending")

    if asc_result["valid_rows"] >= desc_result["valid_rows"]:
        result = asc_result
    else:
        result = desc_result

    logger.info(
        f"Balance validation: {result['valid_rows']}/{result['validated_rows']} valid "
        f"({result['accuracy_pct']}%), {result['invalid_rows']} mismatches, "
        f"direction={result['direction']} "
        f"(asc={asc_result['valid_rows']}, desc={desc_result['valid_rows']})"
    )

    return result


def _empty_result() -> dict:
    return {
        "total_rows": 0,
        "validated_rows": 0,
        "valid_rows": 0,
        "invalid_rows": 0,
        "skipped_rows": 0,
        "accuracy_pct": 0.0,
        "direction": "unknown",
        "mismatches": [],
        "row_variances": {},
    }
