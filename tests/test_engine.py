from concurrent.futures import ThreadPoolExecutor
import copy
from datetime import datetime, timedelta, timezone
from decimal import Decimal, localcontext
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import threading
import unittest
from unittest.mock import patch
from uuid import uuid4

from invoice_accelerator import GuardError, InvoiceEngine, SQLiteMockERP, TrustedCaller, load_json
from invoice_accelerator.__main__ import main
from invoice_accelerator.erp import ERPReceipt, SimulatedCrash, UnknownOutcomeError
from invoice_accelerator.model import INVOICE_FIELDS, LINE_FIELDS, check_config, check_envelope


ROOT = Path(__file__).resolve().parents[1]


class EngineTests(unittest.TestCase):
    def setUp(self):
        # Deliberately stay inside the project; no OS temporary directory is used.
        self.work = ROOT / "tests" / ".test-runs" / uuid4().hex
        self.work.mkdir(parents=True)
        self.addCleanup(shutil.rmtree, self.work)
        self.db = self.work / "local.db"
        self.erp_db = self.work / "mock-erp.db"
        self.config = load_json(ROOT / "config" / "demo.json")
        self.envelope = load_json(ROOT / "examples" / "invoice.json")
        self.preparer = TrustedCaller("demo-customer", "DEMO-ES", "preparer-demo")
        self.approver = TrustedCaller("demo-customer", "DEMO-ES", "approver-demo")
        self.now = datetime(2026, 9, 30, 12, tzinfo=timezone.utc)
        self.erp = SQLiteMockERP(self.erp_db)
        self.engine = InvoiceEngine(self.db, self.config, self.erp, clock=lambda: self.now)

    def assert_code(self, code, operation):
        with self.assertRaises(GuardError) as caught:
            operation()
        self.assertEqual(code, caught.exception.code)

    def submit(self, envelope=None):
        return self.engine.submit(self.preparer, envelope or self.envelope)

    def ready(self, envelope=None):
        status = self.submit(envelope)
        return self.engine.validate(self.preparer, status["job_id"])

    def approved(self, envelope=None):
        status = self.ready(envelope)
        return self.engine.approve(self.approver, status["job_id"],
                                   expected_version=status["invoice_version"], expected_hash=status["invoice_hash"])

    def receipt_count(self):
        connection = sqlite3.connect(self.erp_db)
        try:
            return connection.execute("SELECT COUNT(*) FROM receipts").fetchone()[0]
        finally:
            connection.close()

    def variant(self, index):
        envelope = copy.deepcopy(self.envelope)
        envelope["source"]["event_id"] = f"event-{index}"
        envelope["invoice"]["invoice_number"] = f"INV-{index}"
        envelope["invoice"]["purchase_order_id"] = f"PO-DEMO-{index:03}"
        return envelope

    def test_01_happy_path_decimal_arithmetic_and_durable_history(self):
        with localcontext() as context:
            context.prec = 2  # Validation must not inherit an inadequate ambient Decimal context.
            status = self.approved()
        posted = self.engine.post(self.preparer, status["job_id"])
        self.assertEqual("POSTED", posted["state"])
        self.assertEqual("COMMITTED", posted["posting_outcome"])
        self.assertTrue(posted["erp_id"].startswith("MOCK-ERP-"))
        self.assertEqual(Decimal("0.30"), Decimal(self.envelope["invoice"]["lines"][1]["line_amount"]))
        self.assertEqual(
            ["RECEIVED", "VALIDATED", "AWAITING_APPROVAL", "APPROVED", "POSTING", "POSTED"],
            [item["state"] for item in self.engine.history(self.preparer, status["job_id"])],
        )
        reopened = InvoiceEngine(self.db, self.config, SQLiteMockERP(self.erp_db))
        self.assertEqual(posted, reopened.status(self.preparer, status["job_id"]))
        self.assertEqual(posted, reopened.post(self.preparer, status["job_id"]))
        self.assertEqual(1, self.receipt_count())

    def test_02_transport_replay_and_altered_event_conflict(self):
        original = self.submit()
        self.assertEqual(original["job_id"], self.submit()["job_id"])
        self.assertEqual("TRANSPORT_REPLAY", self.submit()["disposition"])
        changed = copy.deepcopy(self.envelope)
        changed["invoice"]["total_amount"] = "50.31"
        self.assert_code("TRANSPORT_PAYLOAD_CONFLICT", lambda: self.submit(changed))
        self.assertEqual(2, self.engine.lineage(self.preparer, original["job_id"])[0]["replay_count"])
        self.assertEqual("RECEIVED", self.engine.status(self.preparer, original["job_id"])["state"])

    def test_03_business_duplicate_retains_new_source_lineage_without_replacing_invoice(self):
        original = self.approved()
        changed = copy.deepcopy(self.envelope)
        changed["source"]["event_id"] = "another-event"
        changed["source"]["reference"] = "synthetic://second-copy"
        changed["source"]["sha256"] = "b" * 64
        duplicate = self.submit(changed)
        self.assertEqual("DUPLICATE_BUSINESS", duplicate["disposition"])
        self.assertEqual(original["job_id"], duplicate["job_id"])
        self.assertEqual(original["invoice_hash"], duplicate["invoice_hash"])
        self.assertEqual("APPROVED", duplicate["state"])
        lineage = self.engine.lineage(self.preparer, original["job_id"])
        self.assertEqual(["PRIMARY", "DUPLICATE_BUSINESS"], [row["disposition"] for row in lineage])
        self.assertNotEqual(lineage[0]["attempt_id"], lineage[1]["attempt_id"])
        self.assertEqual("TRANSPORT_REPLAY", self.submit(changed)["disposition"])
        changed["source"]["event_id"] = "unsupported-correction"
        changed["invoice"]["total_amount"] = "90.00"
        self.assert_code("BUSINESS_PAYLOAD_CONFLICT", lambda: self.submit(changed))
        self.assert_code("BUSINESS_PAYLOAD_CONFLICT", lambda: self.submit(changed))
        lineage = self.engine.lineage(self.preparer, original["job_id"])
        self.assertEqual("BUSINESS_CONFLICT", lineage[-1]["disposition"])
        self.assertEqual(1, lineage[-1]["replay_count"])
        self.assertEqual(original, self.engine.status(self.preparer, original["job_id"]))

    def test_04_exact_business_key_preserves_punctuation_case_series_supplier_and_entity(self):
        base = self.submit()
        variations = [
            ("invoice_number", "INV0001"), ("invoice_number", "inv-0001"),
            ("invoice_series", "OTHER-SERIES"), ("supplier_id", "OTHER-SUPPLIER"),
        ]
        ids = {base["job_id"]}
        for index, (field, value) in enumerate(variations):
            variant = copy.deepcopy(self.envelope)
            variant["source"]["event_id"] = f"exact-key-{index}"
            variant["invoice"][field] = value
            result = self.submit(variant)
            self.assertEqual("PRIMARY", result["disposition"])
            ids.add(result["job_id"])
        self.assertEqual(5, len(ids))
        other_profile = copy.deepcopy(self.config["customers"]["demo-customer"]["legal_entities"]["DEMO-ES"])
        self.config["customers"]["demo-customer"]["legal_entities"]["DEMO-OTHER"] = other_profile
        engine = InvoiceEngine(self.db, self.config, self.erp)
        other = copy.deepcopy(self.envelope)
        other["legal_entity_id"] = "DEMO-OTHER"
        self.assertNotIn(engine.submit(TrustedCaller("demo-customer", "DEMO-OTHER", "preparer-demo"), other)["job_id"], ids)

    def test_05_scope_checks_cover_every_public_job_read_and_mutation(self):
        status = self.submit()
        job_id = status["job_id"]
        profile = copy.deepcopy(self.config["customers"]["demo-customer"])
        self.config["customers"]["other-customer"] = profile
        self.config["customers"]["demo-customer"]["legal_entities"]["OTHER-ENTITY"] = copy.deepcopy(
            profile["legal_entities"]["DEMO-ES"])
        engine = InvoiceEngine(self.db, self.config, self.erp)
        callers = [
            TrustedCaller("other-customer", "DEMO-ES", "preparer-demo"),
            TrustedCaller("demo-customer", "OTHER-ENTITY", "preparer-demo"),
        ]
        for caller in callers:
            for method in ("status", "history", "lineage", "validate", "post", "reconcile"):
                with self.subTest(scope=caller, method=method):
                    self.assert_code("JOB_NOT_FOUND", lambda: getattr(engine, method)(caller, job_id))
            self.assert_code("JOB_NOT_FOUND", lambda: engine.approve(
                caller, job_id, expected_version=1, expected_hash=status["invoice_hash"]))
            self.assert_code("JOB_NOT_FOUND", lambda: engine.amend(
                caller, job_id, self.envelope, expected_version=1))
            self.assert_code("SCOPE_MISMATCH", lambda: engine.submit(caller, self.envelope))
        self.assert_code("TRUSTED_LOCAL_CALLER_REQUIRED", lambda: engine.status(
            {"customer_id": "demo-customer"}, job_id))
        self.assert_code("SCOPE_NOT_ALLOWED", lambda: engine.status(
            TrustedCaller("unconfigured", "DEMO-ES", "preparer-demo"), job_id))
        self.assertEqual("RECEIVED", engine.status(self.preparer, job_id)["state"])

    def test_06_concurrent_double_submit_has_one_job_and_unique_source_attempts(self):
        with ThreadPoolExecutor(max_workers=8) as pool:
            responses = list(pool.map(lambda _: self.submit(), range(16)))
        job_id = responses[0]["job_id"]
        self.assertEqual({job_id}, {result["job_id"] for result in responses})
        self.assertEqual(1, sum(result["disposition"] == "PRIMARY" for result in responses))
        self.assertEqual(15, self.engine.lineage(self.preparer, job_id)[0]["replay_count"])

        def new_event(index):
            envelope = copy.deepcopy(self.envelope)
            envelope["source"]["event_id"] = f"concurrent-{index}"
            return self.submit(envelope)

        with ThreadPoolExecutor(max_workers=8) as pool:
            duplicates = list(pool.map(new_event, range(8)))
        self.assertEqual({job_id}, {result["job_id"] for result in duplicates})
        self.assertTrue(all(result["disposition"] == "DUPLICATE_BUSINESS" for result in duplicates))
        self.assertEqual(9, len(self.engine.lineage(self.preparer, job_id)))

    def test_07_malformed_nonfinite_float_and_unbounded_money_are_rejected(self):
        for value in ("NaN", "Infinity", "-Infinity", "1e2", "50.3", "-50.30", " 50.30",
                      "050.30", "10000000000.00", 50.3, None, True):
            with self.subTest(value=value):
                envelope = copy.deepcopy(self.envelope)
                envelope["invoice"]["total_amount"] = value
                self.assert_code("INVALID_DECIMAL", lambda: self.submit(envelope))
        envelope = copy.deepcopy(self.envelope)
        envelope["invoice"]["total_amount"] = float("nan")
        self.assert_code("INVALID_JSON", lambda: self.submit(envelope))
        envelope["invoice"]["total_amount"] = "0.00"
        self.assert_code("NON_POSITIVE_INVOICE", lambda: self.submit(envelope))

    def test_08_critical_ocr_confidence_threshold_and_complete_evidence(self):
        envelope = copy.deepcopy(self.envelope)
        envelope["evidence"]["/invoice/total_amount"]["confidence"] = "0.9799"
        submitted = self.submit(envelope)
        self.assert_code("CRITICAL_EVIDENCE_LOW_CONFIDENCE",
                         lambda: self.engine.validate(self.preparer, submitted["job_id"]))
        status = self.engine.status(self.preparer, submitted["job_id"])
        self.assertEqual("REJECTED", status["state"])
        self.assertEqual("CRITICAL_EVIDENCE_LOW_CONFIDENCE", status["reason_code"])
        envelope = self.variant(2)
        for evidence in envelope["evidence"].values():
            evidence["confidence"] = "0.98"
        self.assertEqual("AWAITING_APPROVAL", self.ready(envelope)["state"])
        envelope = self.variant(3)
        del envelope["evidence"]["/invoice/buyer_id"]
        self.assert_code("MISSING_OR_UNKNOWN_EVIDENCE", lambda: self.submit(envelope))

    def test_09_buyer_and_supplier_are_verified_against_trusted_configuration(self):
        for field, value, code in (("buyer_id", "WRONG-BUYER", "WRONG_BUYER"),
                                   ("supplier_id", "WRONG-SUPPLIER", "PO_SUPPLIER_MISMATCH"),
                                   ("purchase_order_id", "UNKNOWN-PO", "PO_NOT_FOUND")):
            envelope = self.variant({"buyer_id": 1, "supplier_id": 2, "purchase_order_id": 3}[field])
            envelope["invoice"][field] = value
            job = self.submit(envelope)
            self.assert_code(code, lambda: self.engine.validate(self.preparer, job["job_id"]))

    def test_10_totals_and_line_arithmetic_must_match_without_tolerance_or_rounding(self):
        for index, code in ((1, "TOTAL_MISMATCH"), (2, "LINE_AMOUNT_MISMATCH"), (3, "PO_PRICE_MISMATCH")):
            envelope = self.variant(index)
            if index == 1:
                envelope["invoice"]["total_amount"] = "50.31"
            elif index == 2:
                envelope["invoice"]["lines"][1]["line_amount"] = "0.31"
            else:
                envelope["invoice"]["lines"][1]["unit_price"] = "0.11"
            job = self.submit(envelope)
            self.assert_code(code, lambda: self.engine.validate(self.preparer, job["job_id"]))

    def test_11_unsupported_types_currency_tax_and_unknown_fields_fail_closed(self):
        changes = [
            ("document_type", "CREDIT_NOTE", "UNSUPPORTED_DOCUMENT_TYPE"),
            ("document_type", "NON_PO_INVOICE", "UNSUPPORTED_DOCUMENT_TYPE"),
            ("currency", "USD", "UNSUPPORTED_CURRENCY"),
            ("tax_treatment", "VAT", "UNSUPPORTED_TAX"),
            ("tax_amount", "10.00", "UNSUPPORTED_TAX"),
            ("invoice_date", "2026-02-30", "INVALID_DATE"),
            ("bank_account", "DO-NOT-ACCEPT", "UNKNOWN_OR_MISSING_FIELD"),
            ("approval_authority", "APPROVED", "UNKNOWN_OR_MISSING_FIELD"),
            ("discount_amount", "1.00", "UNKNOWN_OR_MISSING_FIELD"),
        ]
        for field, value, code in changes:
            with self.subTest(field=field, value=value):
                envelope = copy.deepcopy(self.envelope)
                envelope["invoice"][field] = value
                self.assert_code(code, lambda: self.submit(envelope))
        for section, field in (("source", "trusted"), ("invoice", "tax_lines")):
            envelope = copy.deepcopy(self.envelope)
            envelope[section][field] = []
            self.assert_code("UNKNOWN_OR_MISSING_FIELD", lambda: self.submit(envelope))
        envelope = copy.deepcopy(self.envelope)
        envelope["invoice"]["lines"][0]["tax_rate"] = "0.21"
        self.assert_code("UNKNOWN_OR_MISSING_FIELD", lambda: self.submit(envelope))
        self.assertFalse(hasattr(self.engine, "execute_payment"))
        self.assertFalse(hasattr(self.erp, "change_vendor_bank"))

    def test_12_partial_invoices_and_partial_receipts_are_unsupported(self):
        envelope = copy.deepcopy(self.envelope)
        envelope["invoice"]["lines"][0]["quantity"] = "1"
        status = self.submit(envelope)
        self.assert_code("PARTIAL_INVOICE_UNSUPPORTED", lambda: self.engine.validate(self.preparer, status["job_id"]))
        self.engine.config["customers"]["demo-customer"]["legal_entities"]["DEMO-ES"]["purchase_orders"][
            "PO-DEMO-002"]["lines"]["10"]["received_quantity"] = "1"
        status = self.submit(self.variant(2))
        self.assert_code("PARTIAL_RECEIPT_UNSUPPORTED", lambda: self.engine.validate(self.preparer, status["job_id"]))

    def test_13_self_approval_is_rejected_even_when_actor_is_a_configured_approver(self):
        self.engine.config["customers"]["demo-customer"]["legal_entities"]["DEMO-ES"]["approvers"][
            "preparer-demo"] = {"EUR": "9999.00"}
        status = self.ready()
        self.assert_code("SELF_APPROVAL", lambda: self.engine.approve(
            self.preparer, status["job_id"], expected_version=1, expected_hash=status["invoice_hash"]))
        self.assertEqual("AWAITING_APPROVAL", self.engine.status(self.preparer, status["job_id"])["state"])

    def test_14_approval_authority_is_local_scoped_config_not_model_input(self):
        status = self.ready()
        stranger = TrustedCaller("demo-customer", "DEMO-ES", "model-says-authorized")
        self.assert_code("APPROVER_NOT_AUTHORIZED", lambda: self.engine.approve(
            stranger, status["job_id"], expected_version=1, expected_hash=status["invoice_hash"]))
        profile = self.engine.config["customers"]["demo-customer"]["legal_entities"]["DEMO-ES"]
        profile["approvers"]["approver-demo"]["EUR"] = "50.29"
        self.assert_code("APPROVAL_LIMIT_EXCEEDED", lambda: self.engine.approve(
            self.approver, status["job_id"], expected_version=1, expected_hash=status["invoice_hash"]))
        self.assert_code("PREPARER_NOT_AUTHORIZED", lambda: self.engine.submit(stranger, self.variant(2)))

    def test_15_approval_binds_hash_and_version_and_amendments_are_explicitly_rejected(self):
        status = self.ready()
        for version, invoice_hash in ((2, status["invoice_hash"]), (1, "0" * 64), (True, status["invoice_hash"])):
            self.assert_code("STALE_INVOICE_VERSION", lambda: self.engine.approve(
                self.approver, status["job_id"], expected_version=version, expected_hash=invoice_hash))
        self.assert_code("AMENDMENT_NOT_SUPPORTED", lambda: self.engine.amend(
            self.preparer, status["job_id"], self.variant(2), expected_version=1))
        self.assertEqual(status["invoice_hash"], self.engine.status(self.preparer, status["job_id"])["invoice_hash"])

    def test_16_expired_approval_is_cleared_and_requires_fresh_approval(self):
        status = self.approved()
        self.now += timedelta(hours=24)
        self.assert_code("APPROVAL_EXPIRED", lambda: self.engine.post(self.preparer, status["job_id"]))
        self.assertEqual("AWAITING_APPROVAL", self.engine.status(self.preparer, status["job_id"])["state"])
        self.assertEqual(0, self.receipt_count())
        self.engine.approve(self.approver, status["job_id"], expected_version=1, expected_hash=status["invoice_hash"])
        self.assertEqual("POSTED", self.engine.post(self.preparer, status["job_id"])["state"])

    def test_17_policy_change_revokes_existing_approval_before_post(self):
        status = self.approved()
        profile = self.config["customers"]["demo-customer"]["legal_entities"]["DEMO-ES"]
        profile["approvers"]["approver-demo"]["EUR"] = "1.00"
        engine = InvoiceEngine(self.db, self.config, self.erp, clock=lambda: self.now)
        self.assert_code("APPROVAL_POLICY_CHANGED", lambda: engine.post(self.preparer, status["job_id"]))
        self.assertEqual("AWAITING_APPROVAL", engine.status(self.preparer, status["job_id"])["state"])
        self.assert_code("APPROVAL_LIMIT_EXCEEDED", lambda: engine.approve(
            self.approver, status["job_id"], expected_version=1, expected_hash=status["invoice_hash"]))
        self.assertEqual(0, self.receipt_count())

    def test_18_only_validated_approved_invoices_can_be_posted_and_po_is_consumed_once(self):
        status = self.submit()
        self.assert_code("NOT_APPROVED", lambda: self.engine.post(self.preparer, status["job_id"]))
        self.assert_code("INVALID_STATE", lambda: self.engine.approve(
            self.approver, status["job_id"], expected_version=1, expected_hash=status["invoice_hash"]))
        self.engine.validate(self.preparer, status["job_id"])
        self.assert_code("NOT_APPROVED", lambda: self.engine.post(self.preparer, status["job_id"]))
        second = self.variant(2)
        second["invoice"]["purchase_order_id"] = "PO-DEMO-001"
        other = self.submit(second)
        self.assert_code("PO_ALREADY_CLAIMED", lambda: self.engine.validate(self.preparer, other["job_id"]))
        self.assertEqual(0, self.receipt_count())

    def test_19_commit_then_lost_response_stays_unknown_until_scoped_key_reconciliation(self):
        status = self.approved()
        self.engine.erp = SQLiteMockERP(self.erp_db, fault="after_commit")
        result = self.engine.post(self.preparer, status["job_id"])
        self.assertEqual("POSTING_UNKNOWN", result["state"])
        self.assertEqual("UNKNOWN", result["posting_outcome"])
        self.assertIsNone(result["erp_id"])
        self.assertEqual(1, self.receipt_count())
        self.assert_code("OUTCOME_UNKNOWN_RECONCILE_REQUIRED", lambda: self.engine.post(self.preparer, status["job_id"]))
        self.assertIsNone(self.erp.lookup(TrustedCaller("other-customer", "DEMO-ES", "preparer-demo"),
                                         external_key=status["external_key"]))
        result = self.engine.reconcile(self.preparer, status["job_id"])
        self.assertEqual("POSTED", result["state"])
        self.assertEqual(1, result["posting_attempts"])
        self.assertEqual(result, self.engine.reconcile(self.preparer, status["job_id"]))
        self.assertEqual(1, self.receipt_count())

    def test_20_process_crash_before_or_after_commit_never_allows_blind_repost(self):
        for index, fault in ((1, "crash_before_commit"), (2, "crash_after_commit")):
            with self.subTest(fault=fault):
                status = self.approved(self.variant(index))
                self.engine.erp = SQLiteMockERP(self.erp_db, fault=fault)
                with self.assertRaises(SimulatedCrash):
                    self.engine.post(self.preparer, status["job_id"])
                recovered = InvoiceEngine(self.db, self.config, SQLiteMockERP(self.erp_db))
                result = recovered.status(self.preparer, status["job_id"])
                self.assertEqual("POSTING", result["state"])
                self.assertEqual("UNKNOWN", result["posting_outcome"])
                self.assert_code("OUTCOME_UNKNOWN_RECONCILE_REQUIRED",
                                 lambda: recovered.post(self.preparer, status["job_id"]))
                result = recovered.reconcile(self.preparer, status["job_id"])
                self.assertEqual("POSTING_UNKNOWN" if index == 1 else "POSTED", result["state"])
                if index == 1:
                    self.assertEqual("ERP_COMMIT_NOT_CONFIRMED", result["reason_code"])
                    self.assert_code("OUTCOME_UNKNOWN_RECONCILE_REQUIRED",
                                     lambda: recovered.post(self.preparer, status["job_id"]))

    def test_21_only_proven_noncommit_is_retryable_and_retries_are_bounded(self):
        status = self.approved()
        self.engine.erp = SQLiteMockERP(self.erp_db, fault="before_commit")
        failed = self.engine.post(self.preparer, status["job_id"])
        self.assertEqual(("APPROVED", "KNOWN_NONCOMMIT"), (failed["state"], failed["posting_outcome"]))
        self.engine.erp = self.erp
        posted = self.engine.post(self.preparer, status["job_id"])
        self.assertEqual(("POSTED", 2), (posted["state"], posted["posting_attempts"]))
        status = self.approved(self.variant(2))
        self.engine.erp = SQLiteMockERP(self.erp_db, fault="before_commit")
        for attempt in range(1, 4):
            result = self.engine.post(self.preparer, status["job_id"])
            self.assertEqual(attempt, result["posting_attempts"])
        self.assert_code("RETRY_LIMIT_REACHED", lambda: self.engine.post(self.preparer, status["job_id"]))
        self.assertEqual(1, self.receipt_count())

    def test_22_declared_failures_are_unknown_and_programmer_errors_propagate_with_durable_intent(self):
        class OperationalFailure:
            def __init__(self, error_type):
                self.error_type = error_type

            def post_invoice(self, caller, **kwargs):
                raise self.error_type("DO-NOT-PRINT-THIS-INVOICE-CONTENT")

            def lookup(self, caller, **kwargs):
                raise self.error_type("DO-NOT-PRINT-THIS-INVOICE-CONTENT")

        failed_jobs = []
        for index, error_type in enumerate((UnknownOutcomeError, OSError, sqlite3.OperationalError), start=1):
            with self.subTest(error=error_type.__name__):
                status = self.approved(self.variant(index))
                failed_jobs.append(status)
                self.engine.erp = OperationalFailure(error_type)
                result = self.engine.post(self.preparer, status["job_id"])
                self.assertEqual(("POSTING_UNKNOWN", "UNKNOWN", "ERP_OUTCOME_UNKNOWN"),
                                 (result["state"], result["posting_outcome"], result["reason_code"]))
                result = self.engine.reconcile(self.preparer, status["job_id"])
                self.assertEqual("ERP_RECONCILIATION_UNAVAILABLE", result["reason_code"])
                self.assertNotIn("DO-NOT-PRINT", json.dumps(result))
                self.assert_code("OUTCOME_UNKNOWN_RECONCILE_REQUIRED",
                                 lambda: self.engine.post(self.preparer, status["job_id"]))

        mock = self.erp

        class ProgrammerFailure:
            def post_invoice(self, caller, **kwargs):
                mock.post_invoice(caller, **kwargs)
                raise RuntimeError("MOCK_PROGRAMMER_ERROR")

            def lookup(self, caller, **kwargs):
                raise RuntimeError("MOCK_PROGRAMMER_ERROR")

        status = self.approved(self.variant(4))
        self.engine.erp = ProgrammerFailure()
        with self.assertRaisesRegex(RuntimeError, "MOCK_PROGRAMMER_ERROR"):
            self.engine.post(self.preparer, status["job_id"])
        recovered = InvoiceEngine(self.db, self.config, ProgrammerFailure())
        result = recovered.status(self.preparer, status["job_id"])
        self.assertEqual(("POSTING", "UNKNOWN", 1),
                         (result["state"], result["posting_outcome"], result["posting_attempts"]))
        self.assert_code("OUTCOME_UNKNOWN_RECONCILE_REQUIRED",
                         lambda: recovered.post(self.preparer, status["job_id"]))
        with self.assertRaisesRegex(RuntimeError, "MOCK_PROGRAMMER_ERROR"):
            recovered.reconcile(self.preparer, status["job_id"])
        self.assertEqual(result, recovered.status(self.preparer, status["job_id"]))
        recovered.erp = self.erp
        self.assertEqual("POSTED", recovered.reconcile(self.preparer, status["job_id"])["state"])

        class WrongReceipt:
            def post_invoice(self, caller, **kwargs):
                return ERPReceipt(kwargs["external_key"], "other-customer", caller.legal_entity_id,
                                  kwargs["invoice_hash"], "MOCK-WRONG")

            def lookup(self, caller, **kwargs):
                return self.post_invoice(caller, invoice_hash=failed_jobs[0]["invoice_hash"], **kwargs)

        self.engine.erp = WrongReceipt()
        result = self.engine.reconcile(self.preparer, failed_jobs[0]["job_id"])
        self.assertEqual(("POSTING_UNKNOWN", "ERP_RECEIPT_MISMATCH"), (result["state"], result["reason_code"]))
        self.assertEqual(1, self.receipt_count())

    def test_23_concurrent_post_has_one_intent_and_one_external_commit(self):
        entered, release = threading.Event(), threading.Event()
        mock = self.erp

        class SlowAdapter:
            def post_invoice(self, caller, **kwargs):
                entered.set()
                if not release.wait(10):
                    raise RuntimeError("TEST_RELEASE_TIMEOUT")
                return mock.post_invoice(caller, **kwargs)

            def lookup(self, caller, **kwargs):
                return mock.lookup(caller, **kwargs)

        status = self.approved()
        self.engine.erp = SlowAdapter()
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(self.engine.post, self.preparer, status["job_id"])
            try:
                self.assertTrue(entered.wait(10))
                self.assert_code("OUTCOME_UNKNOWN_RECONCILE_REQUIRED",
                                 lambda: self.engine.post(self.preparer, status["job_id"]))
                self.assertEqual("POSTING_UNKNOWN", self.engine.reconcile(self.preparer, status["job_id"])["state"])
            finally:
                release.set()
            result = first.result(timeout=10)
        self.assertEqual(("POSTED", 1), (result["state"], result["posting_attempts"]))
        self.assertEqual(1, self.receipt_count())

    def test_24_json_loading_schema_shape_and_demo_policy_are_consistent(self):
        check_config(self.config)
        check_envelope(self.envelope)
        schema = load_json(ROOT / "schemas" / "invoice.schema.json")
        self.assertFalse(schema["additionalProperties"])
        self.assertEqual(set(self.envelope), set(schema["required"]))
        invoice_schema = schema["properties"]["invoice"]
        self.assertEqual(INVOICE_FIELDS, set(invoice_schema["required"]))
        self.assertEqual(LINE_FIELDS, set(invoice_schema["properties"]["lines"]["items"]["required"]))
        self.assertEqual("EUR", invoice_schema["properties"]["currency"]["const"])
        self.assertEqual("PO_INVOICE", invoice_schema["properties"]["document_type"]["const"])
        duplicate = self.work / "duplicate.json"
        duplicate.write_text('{"customer_id":"one","customer_id":"two"}', encoding="utf-8")
        self.assert_code("DUPLICATE_JSON_FIELD", lambda: load_json(duplicate))
        duplicate.write_text('{"amount":NaN}', encoding="utf-8")
        self.assert_code("INVALID_JSON", lambda: load_json(duplicate))
        config = copy.deepcopy(self.config)
        config["demo_only"] = False
        self.assert_code("INVALID_CONFIG", lambda: check_config(config))

    def test_25_cli_demo_replays_across_processes_without_extra_postings_or_sensitive_output(self):
        with patch("invoice_accelerator.__main__.load_json", side_effect=RuntimeError("CLI_PROGRAMMER_ERROR")):
            with self.assertRaisesRegex(RuntimeError, "CLI_PROGRAMMER_ERROR"):
                main(["demo", "--db", str(self.work / "unexpected-error.db")])
        environment = {**os.environ, "PYTHONPATH": str(ROOT / "src"), "PYTHONDONTWRITEBYTECODE": "1"}
        command = [sys.executable, "-m", "invoice_accelerator", "demo", "--db", str(self.work / "cli.db")]
        first = subprocess.run(command, cwd=ROOT, env=environment, text=True, capture_output=True, timeout=30)
        self.assertEqual(0, first.returncode, first.stdout + first.stderr)
        lines = [json.loads(line) for line in first.stdout.splitlines()]
        self.assertEqual("DEMO_COMPLETE", lines[-1]["result"])
        self.assertTrue(any(line.get("state") == "POSTING_UNKNOWN" for line in lines))
        self.assertTrue(any(line.get("state") == "POSTING" for line in lines))
        self.assertTrue(any(line.get("posting_outcome") == "KNOWN_NONCOMMIT" for line in lines))
        self.assertNotIn("SUPPLIER-DEMO", first.stdout)
        self.assertNotIn("SYNTHETIC-BUYER", first.stdout)
        second = subprocess.run(command, cwd=ROOT, env=environment, text=True, capture_output=True, timeout=30)
        self.assertEqual(0, second.returncode, second.stdout + second.stderr)
        replay = [json.loads(line) for line in second.stdout.splitlines()]
        submissions = [line for line in replay if line.get("step") == "submit"]
        self.assertEqual(4, len(submissions))
        self.assertTrue(all(line["state"] == "POSTED" and line["disposition"] == "TRANSPORT_REPLAY"
                            for line in submissions))
        first_ids = {line["job_id"] for line in lines if "job_id" in line}
        self.assertEqual(first_ids, {line["job_id"] for line in submissions})
        connection = sqlite3.connect(str(self.work / "cli.db") + ".erp.db")
        try:
            self.assertEqual(4, connection.execute("SELECT COUNT(*) FROM receipts").fetchone()[0])
        finally:
            connection.close()


if __name__ == "__main__":
    unittest.main()
