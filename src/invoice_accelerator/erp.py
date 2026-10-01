"""Separate durable SQLite mock boundary. No network, bank or payment API."""

from dataclasses import dataclass
from pathlib import Path
import sqlite3
from typing import Protocol
from uuid import uuid4

from .model import TrustedCaller


@dataclass(frozen=True)
class ERPReceipt:
    external_key: str
    customer_id: str
    legal_entity_id: str
    invoice_hash: str
    erp_id: str


class KnownNonCommitError(Exception):
    """Only adapters that can prove noncommit may raise this retryable error."""


class UnknownOutcomeError(Exception):
    """A commit may have happened: reconciliation, never blind retry."""


class SimulatedCrash(BaseException):
    """Test-only process-death analogue; intentionally bypasses Exception."""


class ERPAdapter(Protocol):
    """Declare operational failures as KnownNonCommitError/UnknownOutcomeError.

    OSError and sqlite3.OperationalError are also treated as unknown outcomes.
    Unexpected implementation errors propagate, retaining the durable intent.
    """

    def post_invoice(self, caller: TrustedCaller, *, external_key: str,
                     invoice_hash: str, invoice: dict) -> ERPReceipt: ...

    def lookup(self, caller: TrustedCaller, *, external_key: str) -> ERPReceipt | None: ...


class SQLiteMockERP:
    """Durable local demo, not an ERP adapter or a financial/security engine."""

    FAULTS = {"none", "before_commit", "after_commit", "crash_before_commit", "crash_after_commit"}

    def __init__(self, path: str | Path, *, fault: str = "none"):
        if fault not in self.FAULTS:
            raise ValueError("UNSUPPORTED_MOCK_FAULT")
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.fault = fault
        connection = self._connect()
        try:
            connection.execute("""
                CREATE TABLE IF NOT EXISTS receipts (
                    external_key TEXT PRIMARY KEY,
                    customer_id TEXT NOT NULL,
                    legal_entity_id TEXT NOT NULL,
                    invoice_hash TEXT NOT NULL,
                    erp_id TEXT UNIQUE NOT NULL
                )
            """)
            connection.commit()
        finally:
            connection.close()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=15)
        connection.row_factory = sqlite3.Row
        return connection

    def lookup(self, caller: TrustedCaller, *, external_key: str) -> ERPReceipt | None:
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT * FROM receipts WHERE external_key=? AND customer_id=? AND legal_entity_id=?",
                (external_key, caller.customer_id, caller.legal_entity_id),
            ).fetchone()
            return ERPReceipt(**dict(row)) if row else None
        finally:
            connection.close()

    def post_invoice(self, caller: TrustedCaller, *, external_key: str,
                     invoice_hash: str, invoice: dict) -> ERPReceipt:
        if self.fault == "before_commit":
            raise KnownNonCommitError("MOCK_PROVEN_NONCOMMIT")
        if self.fault == "crash_before_commit":
            raise SimulatedCrash("MOCK_CRASH_BEFORE_COMMIT")
        connection = self._connect()
        try:
            with connection:
                connection.execute("BEGIN IMMEDIATE")
                row = connection.execute("SELECT * FROM receipts WHERE external_key=?", (external_key,)).fetchone()
                if row:
                    receipt = ERPReceipt(**dict(row))
                    if (receipt.customer_id, receipt.legal_entity_id, receipt.invoice_hash) != (
                        caller.customer_id, caller.legal_entity_id, invoice_hash
                    ):
                        raise UnknownOutcomeError("MOCK_KEY_CONFLICT")
                else:
                    receipt = ERPReceipt(external_key, caller.customer_id, caller.legal_entity_id,
                                         invoice_hash, "MOCK-ERP-" + uuid4().hex)
                    connection.execute("INSERT INTO receipts VALUES (?,?,?,?,?)",
                                       (receipt.external_key, receipt.customer_id, receipt.legal_entity_id,
                                        receipt.invoice_hash, receipt.erp_id))
        finally:
            connection.close()
        # The mock commit above and the local intent commit are deliberately separate.
        if self.fault == "after_commit":
            raise UnknownOutcomeError("MOCK_RESPONSE_LOST")
        if self.fault == "crash_after_commit":
            raise SimulatedCrash("MOCK_CRASH_AFTER_COMMIT")
        return receipt
