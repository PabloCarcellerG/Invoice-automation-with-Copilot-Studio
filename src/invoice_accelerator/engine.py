"""Transaction-safe LOCAL demo ledger, with an explicitly non-atomic ERP boundary."""

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3
from typing import Callable
from uuid import uuid4

from .erp import ERPAdapter, ERPReceipt, KnownNonCommitError, UnknownOutcomeError
from .model import (
    GuardError, TrustedCaller, canonical_json, check_config, check_envelope,
    decimal_string, digest, require, text, validate_invoice,
)


DDL = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS jobs (
    job_id TEXT PRIMARY KEY,
    customer_id TEXT NOT NULL,
    legal_entity_id TEXT NOT NULL,
    supplier_id TEXT NOT NULL,
    invoice_number TEXT NOT NULL,
    invoice_series TEXT NOT NULL,
    document_type TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    invoice_hash TEXT NOT NULL,
    invoice_version INTEGER NOT NULL DEFAULT 1 CHECK (invoice_version = 1),
    preparer_id TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN (
        'RECEIVED','VALIDATED','AWAITING_APPROVAL','APPROVED','POSTING',
        'POSTING_UNKNOWN','POSTED','REJECTED'
    )),
    reason_code TEXT,
    validated_hash TEXT,
    approved_by TEXT,
    approved_hash TEXT,
    approved_version INTEGER,
    approved_at TEXT,
    approved_policy_hash TEXT,
    external_key TEXT NOT NULL UNIQUE,
    erp_id TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(customer_id, legal_entity_id, supplier_id, invoice_number, invoice_series, document_type)
);
CREATE TABLE IF NOT EXISTS source_attempts (
    attempt_id TEXT PRIMARY KEY,
    customer_id TEXT NOT NULL,
    legal_entity_id TEXT NOT NULL,
    transport TEXT NOT NULL,
    event_id TEXT NOT NULL,
    source_reference TEXT NOT NULL,
    source_sha256 TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    job_id TEXT NOT NULL REFERENCES jobs(job_id),
    disposition TEXT NOT NULL,
    replay_count INTEGER NOT NULL DEFAULT 0,
    UNIQUE(customer_id, legal_entity_id, transport, event_id)
);
CREATE TABLE IF NOT EXISTS po_claims (
    customer_id TEXT NOT NULL,
    legal_entity_id TEXT NOT NULL,
    purchase_order_id TEXT NOT NULL,
    job_id TEXT NOT NULL UNIQUE REFERENCES jobs(job_id),
    PRIMARY KEY(customer_id, legal_entity_id, purchase_order_id)
);
CREATE TABLE IF NOT EXISTS posting_intents (
    job_id TEXT PRIMARY KEY REFERENCES jobs(job_id),
    external_key TEXT NOT NULL UNIQUE,
    invoice_hash TEXT NOT NULL,
    attempts INTEGER NOT NULL CHECK (attempts > 0),
    outcome TEXT NOT NULL CHECK (outcome IN ('UNKNOWN','KNOWN_NONCOMMIT','COMMITTED'))
);
CREATE TABLE IF NOT EXISTS transitions (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id TEXT NOT NULL REFERENCES jobs(job_id),
    state TEXT NOT NULL,
    reason_code TEXT,
    occurred_at TEXT NOT NULL
);
"""


class InvoiceEngine:
    """Trusted local callers only; scope checks are not authentication.

    Idempotency is bounded to the durable ledger and adapter contract, never
    a global exactly-once guarantee. Invoices are immutable in this demo.
    """

    def __init__(self, path: str | Path, config: dict, erp: ERPAdapter,
                 *, clock: Callable[[], datetime] | None = None):
        self.config = json.loads(canonical_json(config))
        check_config(self.config)
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.erp = erp
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        connection = self._connect()
        try:
            connection.executescript(DDL)
        finally:
            connection.close()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=15, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA synchronous=FULL")
        return connection

    @contextmanager
    def _transaction(self):
        connection = self._connect()
        try:
            with connection:
                connection.execute("BEGIN IMMEDIATE")
                yield connection
        finally:
            connection.close()

    def _now(self) -> datetime:
        now = self.clock()
        require(now.tzinfo is not None and now.utcoffset() is not None, "INVALID_CLOCK")
        return now.astimezone(timezone.utc)

    def _profile(self, caller: TrustedCaller) -> dict:
        require(isinstance(caller, TrustedCaller), "TRUSTED_LOCAL_CALLER_REQUIRED")
        for value in (caller.customer_id, caller.legal_entity_id, caller.actor_id):
            text(value)
        profile = self.config["customers"].get(caller.customer_id, {}).get("legal_entities", {}).get(caller.legal_entity_id)
        require(profile is not None, "SCOPE_NOT_ALLOWED")
        return profile

    @staticmethod
    def _job(connection: sqlite3.Connection, caller: TrustedCaller, job_id: str) -> sqlite3.Row:
        row = connection.execute(
            "SELECT * FROM jobs WHERE job_id=? AND customer_id=? AND legal_entity_id=?",
            (job_id, caller.customer_id, caller.legal_entity_id),
        ).fetchone()
        require(row is not None, "JOB_NOT_FOUND")
        return row

    def _state(self, connection: sqlite3.Connection, job_id: str, state: str,
               reason: str | None = None) -> None:
        connection.execute("UPDATE jobs SET state=?, reason_code=? WHERE job_id=?", (state, reason, job_id))
        connection.execute("INSERT INTO transitions(job_id,state,reason_code,occurred_at) VALUES (?,?,?,?)",
                           (job_id, state, reason, self._now().isoformat()))

    @staticmethod
    def _summary(connection: sqlite3.Connection, row: sqlite3.Row) -> dict:
        intent = connection.execute("SELECT * FROM posting_intents WHERE job_id=?", (row["job_id"],)).fetchone()
        return {
            "job_id": row["job_id"], "state": row["state"], "reason_code": row["reason_code"],
            "invoice_version": row["invoice_version"], "invoice_hash": row["invoice_hash"],
            "external_key": row["external_key"], "erp_id": row["erp_id"],
            "posting_attempts": intent["attempts"] if intent else 0,
            "posting_outcome": intent["outcome"] if intent else "NOT_ATTEMPTED",
        }

    def status(self, caller: TrustedCaller, job_id: str) -> dict:
        self._profile(caller)
        with self._transaction() as connection:
            return self._summary(connection, self._job(connection, caller, job_id))

    def history(self, caller: TrustedCaller, job_id: str) -> list[dict]:
        self._profile(caller)
        with self._transaction() as connection:
            self._job(connection, caller, job_id)
            return [dict(row) for row in connection.execute(
                "SELECT state,reason_code,occurred_at FROM transitions WHERE job_id=? ORDER BY sequence", (job_id,)
            )]

    def lineage(self, caller: TrustedCaller, job_id: str) -> list[dict]:
        self._profile(caller)
        with self._transaction() as connection:
            self._job(connection, caller, job_id)
            return [dict(row) for row in connection.execute(
                "SELECT attempt_id,disposition,replay_count FROM source_attempts WHERE job_id=? ORDER BY rowid", (job_id,)
            )]

    def submit(self, caller: TrustedCaller, envelope: dict) -> dict:
        profile = self._profile(caller)
        require(caller.actor_id in profile["preparers"], "PREPARER_NOT_AUTHORIZED")
        payload_json = canonical_json(envelope)
        envelope = json.loads(payload_json)
        require(isinstance(envelope, dict) and envelope.get("customer_id") == caller.customer_id
                and envelope.get("legal_entity_id") == caller.legal_entity_id, "SCOPE_MISMATCH")
        source = envelope.get("source")
        require(isinstance(source, dict), "INVALID_SOURCE")
        text(source.get("transport"))
        text(source.get("event_id"))
        payload_hash = digest(envelope)
        failure = None
        with self._transaction() as connection:
            replay = connection.execute(
                "SELECT * FROM source_attempts WHERE customer_id=? AND legal_entity_id=? AND transport=? AND event_id=?",
                (caller.customer_id, caller.legal_entity_id, source["transport"], source["event_id"]),
            ).fetchone()
            if replay:
                require(replay["payload_hash"] == payload_hash, "TRANSPORT_PAYLOAD_CONFLICT")
                connection.execute("UPDATE source_attempts SET replay_count=replay_count+1 WHERE attempt_id=?",
                                   (replay["attempt_id"],))
                result = self._summary(connection, self._job(connection, caller, replay["job_id"]))
                result = {**result, "disposition": "TRANSPORT_REPLAY", "source_attempt_id": replay["attempt_id"]}
                if replay["disposition"] == "BUSINESS_CONFLICT":
                    failure = GuardError("BUSINESS_PAYLOAD_CONFLICT")
            else:
                check_envelope(envelope)
                invoice = envelope["invoice"]
                business_key = (caller.customer_id, caller.legal_entity_id, invoice["supplier_id"],
                                invoice["invoice_number"], invoice["invoice_series"], invoice["document_type"])
                existing = connection.execute(
                    """SELECT job_id,payload_json FROM jobs WHERE customer_id=? AND legal_entity_id=? AND supplier_id=?
                       AND invoice_number=? AND invoice_series=? AND document_type=?""", business_key,
                ).fetchone()
                if existing:
                    job_id, disposition = existing["job_id"], "DUPLICATE_BUSINESS"
                    if invoice != json.loads(existing["payload_json"])["invoice"]:
                        disposition = "BUSINESS_CONFLICT"
                        failure = GuardError("BUSINESS_PAYLOAD_CONFLICT")
                else:
                    job_id, disposition = "JOB-" + uuid4().hex, "PRIMARY"
                    invoice_hash = digest({
                        "schema_version": envelope["schema_version"],
                        "customer_id": caller.customer_id, "legal_entity_id": caller.legal_entity_id,
                        "invoice": invoice, "evidence": envelope["evidence"],
                        "source_document": {"reference": source["reference"], "sha256": source["sha256"]},
                    })
                    connection.execute(
                        """INSERT INTO jobs(job_id,customer_id,legal_entity_id,supplier_id,invoice_number,
                           invoice_series,document_type,payload_json,invoice_hash,preparer_id,state,external_key,created_at)
                           VALUES (?,?,?,?,?,?,?,?,?,?,'RECEIVED',?,?)""",
                        (job_id, *business_key, payload_json, invoice_hash, caller.actor_id,
                         "INVOICE-" + uuid4().hex, self._now().isoformat()),
                    )
                    self._state(connection, job_id, "RECEIVED")
                attempt_id = "SOURCE-" + uuid4().hex
                connection.execute(
                    """INSERT INTO source_attempts(attempt_id,customer_id,legal_entity_id,transport,event_id,
                       source_reference,source_sha256,payload_hash,payload_json,job_id,disposition)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                    (attempt_id, caller.customer_id, caller.legal_entity_id, source["transport"], source["event_id"],
                     source["reference"], source["sha256"], payload_hash, payload_json, job_id, disposition),
                )
                result = self._summary(connection, self._job(connection, caller, job_id))
                result = {**result, "disposition": disposition, "source_attempt_id": attempt_id}
        if failure:
            raise failure
        return result

    def validate(self, caller: TrustedCaller, job_id: str) -> dict:
        profile = self._profile(caller)
        failure = None
        with self._transaction() as connection:
            row = self._job(connection, caller, job_id)
            require(caller.actor_id in profile["preparers"], "PREPARER_NOT_AUTHORIZED")
            require(row["state"] == "RECEIVED", "INVALID_STATE")
            envelope = json.loads(row["payload_json"])
            try:
                validate_invoice(envelope, profile)
                claimed = connection.execute(
                    "SELECT job_id FROM po_claims WHERE customer_id=? AND legal_entity_id=? AND purchase_order_id=?",
                    (caller.customer_id, caller.legal_entity_id, envelope["invoice"]["purchase_order_id"]),
                ).fetchone()
                require(claimed is None, "PO_ALREADY_CLAIMED")
            except GuardError as exc:
                failure = exc
                self._state(connection, job_id, "REJECTED", exc.code)
            else:
                connection.execute("INSERT INTO po_claims VALUES (?,?,?,?)",
                                   (caller.customer_id, caller.legal_entity_id,
                                    envelope["invoice"]["purchase_order_id"], job_id))
                connection.execute("UPDATE jobs SET validated_hash=? WHERE job_id=?", (row["invoice_hash"], job_id))
                self._state(connection, job_id, "VALIDATED")
                self._state(connection, job_id, "AWAITING_APPROVAL")
            result = self._summary(connection, self._job(connection, caller, job_id))
        if failure:
            raise failure
        return result

    @staticmethod
    def _authority(profile: dict, row: sqlite3.Row, actor: str) -> None:
        require(actor != row["preparer_id"], "SELF_APPROVAL")
        limits = profile["approvers"].get(actor)
        require(limits is not None, "APPROVER_NOT_AUTHORIZED")
        invoice = json.loads(row["payload_json"])["invoice"]
        limit = limits.get(invoice["currency"])
        require(limit is not None and decimal_string(invoice["total_amount"]) <= decimal_string(limit),
                "APPROVAL_LIMIT_EXCEEDED")

    def approve(self, caller: TrustedCaller, job_id: str, *, expected_version: int, expected_hash: str) -> dict:
        profile = self._profile(caller)
        with self._transaction() as connection:
            row = self._job(connection, caller, job_id)
            require(row["state"] == "AWAITING_APPROVAL", "INVALID_STATE")
            require(type(expected_version) is int and expected_version == row["invoice_version"]
                    and expected_hash == row["invoice_hash"], "STALE_INVOICE_VERSION")
            require(row["validated_hash"] == row["invoice_hash"], "NOT_VALIDATED")
            self._authority(profile, row, caller.actor_id)
            validate_invoice(json.loads(row["payload_json"]), profile)
            connection.execute(
                """UPDATE jobs SET approved_by=?,approved_hash=?,approved_version=?,approved_at=?,
                   approved_policy_hash=? WHERE job_id=?""",
                (caller.actor_id, expected_hash, expected_version, self._now().isoformat(), digest(profile), job_id),
            )
            self._state(connection, job_id, "APPROVED")
            return self._summary(connection, self._job(connection, caller, job_id))

    def amend(self, caller: TrustedCaller, job_id: str, replacement: dict, *, expected_version: int) -> None:
        self._profile(caller)
        with self._transaction() as connection:
            self._job(connection, caller, job_id)
            raise GuardError("AMENDMENT_NOT_SUPPORTED")

    def _check_approval(self, profile: dict, row: sqlite3.Row) -> None:
        require(row["approved_hash"] == row["invoice_hash"] == row["validated_hash"]
                and row["approved_version"] == row["invoice_version"], "STALE_APPROVAL")
        require(row["approved_policy_hash"] == digest(profile), "APPROVAL_POLICY_CHANGED")
        self._authority(profile, row, row["approved_by"])
        approved_at = datetime.fromisoformat(row["approved_at"])
        age = self._now() - approved_at
        require(timedelta(0) <= age < timedelta(hours=profile["approval_max_age_hours"]), "APPROVAL_EXPIRED")
        validate_invoice(json.loads(row["payload_json"]), profile)

    def post(self, caller: TrustedCaller, job_id: str) -> dict:
        profile = self._profile(caller)
        failure = None
        with self._transaction() as connection:
            row = self._job(connection, caller, job_id)
            require(caller.actor_id in profile["preparers"], "PREPARER_NOT_AUTHORIZED")
            if row["state"] == "POSTED":
                return self._summary(connection, row)
            require(row["state"] not in {"POSTING", "POSTING_UNKNOWN"}, "OUTCOME_UNKNOWN_RECONCILE_REQUIRED")
            require(row["state"] == "APPROVED", "NOT_APPROVED")
            try:
                self._check_approval(profile, row)
            except GuardError as exc:
                failure = exc
                connection.execute(
                    """UPDATE jobs SET approved_by=NULL,approved_hash=NULL,approved_version=NULL,
                       approved_at=NULL,approved_policy_hash=NULL WHERE job_id=?""", (job_id,),
                )
                self._state(connection, job_id, "AWAITING_APPROVAL", exc.code)
            if failure is None:
                intent = connection.execute("SELECT * FROM posting_intents WHERE job_id=?", (job_id,)).fetchone()
                require(intent is None or intent["outcome"] == "KNOWN_NONCOMMIT", "OUTCOME_UNKNOWN_RECONCILE_REQUIRED")
                require(intent is None or intent["attempts"] < self.config["max_post_attempts"], "RETRY_LIMIT_REACHED")
                connection.execute(
                    """INSERT INTO posting_intents VALUES (?,?,?,1,'UNKNOWN')
                       ON CONFLICT(job_id) DO UPDATE SET attempts=attempts+1,outcome='UNKNOWN'""",
                    (job_id, row["external_key"], row["invoice_hash"]),
                )
                self._state(connection, job_id, "POSTING", "POSTING_OUTCOME_UNCONFIRMED")
        if failure:
            raise failure
        # Do not enclose this call in the local transaction. A process can die at this boundary.
        try:
            receipt = self.erp.post_invoice(
                caller, external_key=row["external_key"], invoice_hash=row["invoice_hash"],
                invoice=json.loads(row["payload_json"])["invoice"],
            )
        except KnownNonCommitError:
            with self._transaction() as connection:
                current = self._job(connection, caller, job_id)
                # A concurrent reconciler may already have confirmed a commit.
                if current["state"] == "POSTING":
                    connection.execute("UPDATE posting_intents SET outcome='KNOWN_NONCOMMIT' WHERE job_id=?", (job_id,))
                    self._state(connection, job_id, "APPROVED", "ERP_KNOWN_NONCOMMIT")
                return self._summary(connection, self._job(connection, caller, job_id))
        except (UnknownOutcomeError, OSError, sqlite3.OperationalError):
            return self._unknown(caller, job_id, "ERP_OUTCOME_UNKNOWN")
        return self._record_receipt(caller, job_id, receipt)

    def _unknown(self, caller: TrustedCaller, job_id: str, reason: str) -> dict:
        with self._transaction() as connection:
            row = self._job(connection, caller, job_id)
            if row["state"] in {"POSTING", "POSTING_UNKNOWN"}:
                self._state(connection, job_id, "POSTING_UNKNOWN", reason)
            return self._summary(connection, self._job(connection, caller, job_id))

    def _record_receipt(self, caller: TrustedCaller, job_id: str, receipt: ERPReceipt) -> dict:
        with self._transaction() as connection:
            row = self._job(connection, caller, job_id)
            valid = (
                isinstance(receipt, ERPReceipt)
                and (receipt.customer_id, receipt.legal_entity_id, receipt.external_key, receipt.invoice_hash)
                == (caller.customer_id, caller.legal_entity_id, row["external_key"], row["invoice_hash"])
                and isinstance(receipt.erp_id, str) and 0 < len(receipt.erp_id) <= 128
                and receipt.erp_id == receipt.erp_id.strip()
                and all(32 <= ord(c) < 127 for c in receipt.erp_id)
            )
            if row["state"] == "POSTED":
                require(valid and row["erp_id"] == receipt.erp_id, "ERP_RECEIPT_CONFLICT")
                return self._summary(connection, row)
            require(row["state"] in {"POSTING", "POSTING_UNKNOWN"}, "INVALID_STATE")
            if not valid:
                self._state(connection, job_id, "POSTING_UNKNOWN", "ERP_RECEIPT_MISMATCH")
            else:
                connection.execute("UPDATE jobs SET erp_id=? WHERE job_id=?", (receipt.erp_id, job_id))
                connection.execute("UPDATE posting_intents SET outcome='COMMITTED' WHERE job_id=?", (job_id,))
                self._state(connection, job_id, "POSTED")
            return self._summary(connection, self._job(connection, caller, job_id))

    def reconcile(self, caller: TrustedCaller, job_id: str) -> dict:
        self._profile(caller)
        with self._transaction() as connection:
            row = self._job(connection, caller, job_id)
            if row["state"] == "POSTED":
                return self._summary(connection, row)
            require(row["state"] in {"POSTING", "POSTING_UNKNOWN"}, "NOT_RECONCILABLE")
        try:
            receipt = self.erp.lookup(caller, external_key=row["external_key"])
        except (UnknownOutcomeError, OSError, sqlite3.OperationalError):
            return self._unknown(caller, job_id, "ERP_RECONCILIATION_UNAVAILABLE")
        if receipt is None:
            # Absence from a query is not proof that no delayed/unknown commit can occur.
            return self._unknown(caller, job_id, "ERP_COMMIT_NOT_CONFIRMED")
        return self._record_receipt(caller, job_id, receipt)
