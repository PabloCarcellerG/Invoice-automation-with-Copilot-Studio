"""Strict, intentionally narrow invoice contract and deterministic guards."""

from dataclasses import dataclass
from datetime import date
from decimal import Decimal, localcontext
import hashlib
import json
from pathlib import Path
import re
from typing import Any


class GuardError(Exception):
    """Only a stable reason code is exposed; never echo invoice contents."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class TrustedCaller:
    """Explicitly trusted LOCAL caller, not an authenticated tenant claim."""

    customer_id: str
    legal_entity_id: str
    actor_id: str


MONEY = r"(?:0|[1-9][0-9]{0,9})\.[0-9]{2}"
QUANTITY = r"(?:0|[1-9][0-9]{0,5})(?:\.[0-9]{1,3})?"
CONFIDENCE = r"(?:0(?:\.[0-9]{1,4})?|1(?:\.0{1,4})?)"
INVOICE_FIELDS = {
    "supplier_id", "buyer_id", "invoice_number", "invoice_series",
    "document_type", "currency", "invoice_date", "purchase_order_id",
    "tax_treatment", "tax_amount", "subtotal_amount", "total_amount", "lines",
}
LINE_FIELDS = {"line_number", "quantity", "unit_price", "line_amount"}


def require(condition: bool, code: str) -> None:
    if not condition:
        raise GuardError(code)


def exact_keys(value: Any, keys: set[str], code: str = "UNKNOWN_OR_MISSING_FIELD") -> None:
    require(isinstance(value, dict) and set(value) == keys, code)


def text(value: Any, *, empty: bool = False, maximum: int = 128) -> None:
    require(
        isinstance(value, str)
        and (empty or bool(value))
        and len(value) <= maximum
        and value == value.strip()
        and not any(ord(c) < 32 or ord(c) == 127 for c in value),
        "INVALID_TEXT",
    )


def decimal_string(value: Any, pattern: str = MONEY) -> Decimal:
    require(isinstance(value, str) and re.fullmatch(pattern, value) is not None, "INVALID_DECIMAL")
    result = Decimal(value)
    require(result.is_finite(), "INVALID_DECIMAL")
    return result


def canonical_json(value: Any) -> str:
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)
    except (TypeError, ValueError, RecursionError) as exc:
        raise GuardError("INVALID_JSON") from exc


def digest(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def load_json(path: str | Path) -> dict:
    def unique(pairs: list) -> dict:
        result = {}
        for key, value in pairs:
            require(key not in result, "DUPLICATE_JSON_FIELD")
            result[key] = value
        return result

    def invalid_constant(_: str) -> None:
        raise GuardError("INVALID_JSON")

    try:
        with Path(path).open(encoding="utf-8") as stream:
            value = json.load(stream, object_pairs_hook=unique, parse_constant=invalid_constant)
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise GuardError("INVALID_JSON") from exc
    require(isinstance(value, dict), "INVALID_JSON")
    return value


def check_envelope(envelope: dict) -> None:
    exact_keys(envelope, {"schema_version", "customer_id", "legal_entity_id", "source", "invoice", "evidence"})
    require(envelope["schema_version"] == "1.0", "UNSUPPORTED_SCHEMA_VERSION")
    for key in ("customer_id", "legal_entity_id"):
        text(envelope[key])
    source = envelope["source"]
    exact_keys(source, {"transport", "event_id", "reference", "sha256"})
    for key in ("transport", "event_id", "reference"):
        text(source[key], maximum=512 if key == "reference" else 128)
    require(isinstance(source["sha256"], str) and re.fullmatch(r"[0-9a-f]{64}", source["sha256"]) is not None,
            "INVALID_SOURCE_HASH")
    invoice = envelope["invoice"]
    exact_keys(invoice, INVOICE_FIELDS)
    for key in ("supplier_id", "buyer_id", "invoice_number", "purchase_order_id"):
        text(invoice[key])
    text(invoice["invoice_series"], empty=True)
    require(invoice["document_type"] == "PO_INVOICE", "UNSUPPORTED_DOCUMENT_TYPE")
    require(invoice["currency"] == "EUR", "UNSUPPORTED_CURRENCY")
    require(invoice["tax_treatment"] == "NO_TAX", "UNSUPPORTED_TAX")
    require(invoice["tax_amount"] == "0.00", "UNSUPPORTED_TAX")
    require(isinstance(invoice["invoice_date"], str)
            and re.fullmatch(r"\d{4}-\d{2}-\d{2}", invoice["invoice_date"]) is not None, "INVALID_DATE")
    try:
        date.fromisoformat(invoice["invoice_date"])
    except ValueError as exc:
        raise GuardError("INVALID_DATE") from exc
    for key in ("subtotal_amount", "total_amount"):
        require(decimal_string(invoice[key]) > 0, "NON_POSITIVE_INVOICE")
    lines = invoice["lines"]
    require(isinstance(lines, list) and 1 <= len(lines) <= 50, "INVALID_LINES")
    line_numbers = set()
    critical = {f"/invoice/{key}" for key in INVOICE_FIELDS - {"lines"}}
    for index, line in enumerate(lines):
        exact_keys(line, LINE_FIELDS)
        text(line["line_number"])
        require(line["line_number"] not in line_numbers, "DUPLICATE_LINE")
        line_numbers.add(line["line_number"])
        require(decimal_string(line["quantity"], QUANTITY) > 0, "NON_POSITIVE_QUANTITY")
        for key in ("unit_price", "line_amount"):
            require(decimal_string(line[key]) > 0, "NON_POSITIVE_LINE_AMOUNT")
        critical.update(f"/invoice/lines/{index}/{key}" for key in LINE_FIELDS)
    evidence = envelope["evidence"]
    exact_keys(evidence, critical, "MISSING_OR_UNKNOWN_EVIDENCE")
    for item in evidence.values():
        exact_keys(item, {"confidence", "page", "source_span"})
        decimal_string(item["confidence"], CONFIDENCE)
        require(type(item["page"]) is int and 1 <= item["page"] <= 10000, "INVALID_EVIDENCE_PAGE")
        text(item["source_span"], maximum=256)


def check_config(config: dict) -> None:
    """Config is trusted local policy/master data, not extracted invoice data."""
    exact_keys(config, {"schema_version", "demo_only", "max_post_attempts", "customers"}, "INVALID_CONFIG")
    require(config["schema_version"] == "1.0" and config["demo_only"] is True, "INVALID_CONFIG")
    require(type(config["max_post_attempts"]) is int and 1 <= config["max_post_attempts"] <= 5, "INVALID_CONFIG")
    require(isinstance(config["customers"], dict) and bool(config["customers"]), "INVALID_CONFIG")
    for customer, customer_config in config["customers"].items():
        text(customer)
        exact_keys(customer_config, {"legal_entities"}, "INVALID_CONFIG")
        entities = customer_config["legal_entities"]
        require(isinstance(entities, dict) and bool(entities), "INVALID_CONFIG")
        for entity, profile in entities.items():
            text(entity)
            exact_keys(profile, {"buyer_id", "currency", "critical_confidence_threshold", "approval_max_age_hours",
                                 "preparers", "approvers", "purchase_orders"}, "INVALID_CONFIG")
            text(profile["buyer_id"])
            require(profile["currency"] == "EUR", "INVALID_CONFIG")
            require(decimal_string(profile["critical_confidence_threshold"], CONFIDENCE) > 0, "INVALID_CONFIG")
            hours = profile["approval_max_age_hours"]
            require(type(hours) is int and 1 <= hours <= 168, "INVALID_CONFIG")
            require(isinstance(profile["preparers"], list) and bool(profile["preparers"]), "INVALID_CONFIG")
            for actor in profile["preparers"]:
                text(actor)
            require(isinstance(profile["approvers"], dict) and bool(profile["approvers"]), "INVALID_CONFIG")
            for actor, limits in profile["approvers"].items():
                text(actor)
                exact_keys(limits, {"EUR"}, "INVALID_CONFIG")
                require(decimal_string(limits["EUR"]) > 0, "INVALID_CONFIG")
            require(isinstance(profile["purchase_orders"], dict) and bool(profile["purchase_orders"]), "INVALID_CONFIG")
            for po_id, po in profile["purchase_orders"].items():
                text(po_id)
                exact_keys(po, {"supplier_id", "currency", "lines"}, "INVALID_CONFIG")
                text(po["supplier_id"])
                require(po["currency"] == "EUR" and isinstance(po["lines"], dict)
                        and 1 <= len(po["lines"]) <= 50, "INVALID_CONFIG")
                for number, line in po["lines"].items():
                    text(number)
                    exact_keys(line, {"quantity", "received_quantity", "unit_price"}, "INVALID_CONFIG")
                    require(decimal_string(line["quantity"], QUANTITY) > 0, "INVALID_CONFIG")
                    decimal_string(line["received_quantity"], QUANTITY)
                    require(decimal_string(line["unit_price"]) > 0, "INVALID_CONFIG")


def validate_invoice(envelope: dict, profile: dict) -> None:
    check_envelope(envelope)
    invoice = envelope["invoice"]
    require(invoice["buyer_id"] == profile["buyer_id"], "WRONG_BUYER")
    require(invoice["currency"] == profile["currency"], "UNSUPPORTED_CURRENCY")
    threshold = decimal_string(profile["critical_confidence_threshold"], CONFIDENCE)
    require(all(decimal_string(e["confidence"], CONFIDENCE) >= threshold for e in envelope["evidence"].values()),
            "CRITICAL_EVIDENCE_LOW_CONFIDENCE")
    po = profile["purchase_orders"].get(invoice["purchase_order_id"])
    require(po is not None, "PO_NOT_FOUND")
    require(invoice["supplier_id"] == po["supplier_id"], "PO_SUPPLIER_MISMATCH")
    require(invoice["currency"] == po["currency"], "PO_CURRENCY_MISMATCH")
    require({line["line_number"] for line in invoice["lines"]} == set(po["lines"]), "PARTIAL_INVOICE_UNSUPPORTED")
    with localcontext() as context:
        context.prec = 40
        total = Decimal("0.00")
        for line in invoice["lines"]:
            order = po["lines"][line["line_number"]]
            quantity = decimal_string(line["quantity"], QUANTITY)
            require(quantity == decimal_string(order["quantity"], QUANTITY), "PARTIAL_INVOICE_UNSUPPORTED")
            require(quantity == decimal_string(order["received_quantity"], QUANTITY), "PARTIAL_RECEIPT_UNSUPPORTED")
            price = decimal_string(line["unit_price"])
            require(price == decimal_string(order["unit_price"]), "PO_PRICE_MISMATCH")
            amount = decimal_string(line["line_amount"])
            require(quantity * price == amount, "LINE_AMOUNT_MISMATCH")
            total += amount
        require(total == decimal_string(invoice["subtotal_amount"])
                and total == decimal_string(invoice["total_amount"]), "TOTAL_MISMATCH")
