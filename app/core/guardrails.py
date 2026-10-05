"""Exact monetary grounding against this turn's structured tool results."""
import json
import re
from decimal import Decimal, InvalidOperation

PRICE = re.compile(r"\bRs\.\s*([0-9][0-9,]*(?:\.[0-9]+)?)", re.IGNORECASE)
VALID_AMOUNT = re.compile(r"(?:[0-9]+|[0-9]{1,3}(?:,[0-9]{3})+)(?:\.[0-9]{2})?\Z")
MONEY_FIELDS = {"price", "sale_price", "selling_price", "unit_price", "subtotal", "total", "amount"}


def validate_reply(reply_text, tool_results) -> bool:
    """Only monetary fields count as evidence; no arithmetic or rounding."""
    allowed = set()
    def collect(value, monetary=False):
        if hasattr(value, "content"):
            try:
                collect(json.loads(value.content))
            except (TypeError, ValueError):
                pass
        elif isinstance(value, dict):
            if "error" not in value:
                for key, child in value.items():
                    collect(child, key in MONEY_FIELDS)
        elif isinstance(value, (list, tuple)):
            for child in value:
                collect(child, monetary)
        elif monetary and value is not None and not isinstance(value, bool):
            try:
                number = Decimal(str(value))
                if number.is_finite():
                    allowed.add(number)
            except InvalidOperation:
                pass
    collect(tool_results)
    for match in PRICE.finditer(reply_text):
        # A single trailing comma is sentence punctuation, not digit grouping.
        amount = match[1].removesuffix(",")
        if not VALID_AMOUNT.fullmatch(amount) or Decimal(amount.replace(",", "")) not in allowed:
            return False
    return True
