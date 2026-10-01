"""Run from source with PYTHONPATH=src. All identities here are trusted local inputs."""

import argparse
import copy
import hashlib
import json
from pathlib import Path
import sqlite3
import sys

from .engine import InvoiceEngine
from .erp import SQLiteMockERP, SimulatedCrash
from .model import GuardError, TrustedCaller, load_json


ROOT = Path(__file__).resolve().parents[2]


def emit(result: dict | list) -> None:
    print(json.dumps(result, sort_keys=True, ensure_ascii=True))


def demo(args: argparse.Namespace, engine: InvoiceEngine) -> None:
    """Four fixed transport events make repeated runs safe, not fresh postings."""
    preparer = TrustedCaller("demo-customer", "DEMO-ES", "preparer-demo")
    approver = TrustedCaller("demo-customer", "DEMO-ES", "approver-demo")
    sample = load_json(ROOT / "examples" / "invoice.json")
    scenarios = (
        ("happy", "none"),
        ("lost-response", "after_commit"),
        ("process-crash", "crash_after_commit"),
        ("known-noncommit", "before_commit"),
    )
    emit({"mode": "LOCAL_SYNTHETIC_DEMO_NOT_PRODUCTION", "authentication": "NONE_TRUSTED_LOCAL_CALLERS_ONLY"})
    for index, (scenario, fault) in enumerate(scenarios, start=1):
        envelope = copy.deepcopy(sample)
        envelope["source"]["event_id"] = f"demo-{scenario}-v1"
        envelope["source"]["reference"] = f"synthetic://demo-{scenario}"
        envelope["source"]["sha256"] = hashlib.sha256(f"synthetic demo {scenario}".encode()).hexdigest()
        envelope["invoice"]["invoice_number"] = f"DEMO-{scenario}-0001"
        envelope["invoice"]["purchase_order_id"] = f"PO-DEMO-{index:03}"
        result = engine.submit(preparer, envelope)
        job_id = result["job_id"]
        emit({"scenario": scenario, "step": "submit", **result})
        if result["state"] == "RECEIVED":
            result = engine.validate(preparer, job_id)
        if result["state"] == "AWAITING_APPROVAL":
            result = engine.approve(approver, job_id, expected_version=result["invoice_version"],
                                    expected_hash=result["invoice_hash"])
        if result["state"] == "APPROVED":
            engine.erp = SQLiteMockERP(args.erp_db, fault=fault)
            try:
                result = engine.post(preparer, job_id)
            except SimulatedCrash:
                engine = InvoiceEngine(args.db, engine.config, SQLiteMockERP(args.erp_db))
                result = engine.status(preparer, job_id)
            emit({"scenario": scenario, "step": "post-or-crash-recovery", **result})
        if result["state"] in {"POSTING", "POSTING_UNKNOWN"}:
            engine.erp = SQLiteMockERP(args.erp_db)
            result = engine.reconcile(preparer, job_id)
            emit({"scenario": scenario, "step": "reconcile-not-retry", **result})
        if result["state"] == "APPROVED" and result["posting_outcome"] == "KNOWN_NONCOMMIT":
            engine.erp = SQLiteMockERP(args.erp_db)
            result = engine.post(preparer, job_id)
            emit({"scenario": scenario, "step": "bounded-proven-noncommit-retry", **result})
        if result["state"] != "POSTED":
            raise GuardError("DEMO_REQUIRES_REVIEW")
    emit({"result": "DEMO_COMPLETE", "scenarios": 4, "payments_executed": 0, "vendor_bank_changes": 0})


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(
        description="LOCAL synthetic invoice/AP demo. No authentication, real ERP, payments or bank changes."
    )
    commands = result.add_subparsers(dest="command", required=True)
    for command in ("demo", "submit", "validate", "approve", "post", "reconcile", "status", "history", "lineage"):
        child = commands.add_parser(command)
        child.add_argument("--db", type=Path, default=Path(".local") / "demo.db")
        child.add_argument("--erp-db", type=Path, help="Separate durable SQLite MOCK only; defaults to <db>.erp.db")
        child.add_argument("--config", type=Path, default=ROOT / "config" / "demo.json")
        if command != "demo":
            child.add_argument("--customer", default="demo-customer", help="TRUSTED LOCAL customer, not an auth claim")
            child.add_argument("--entity", default="DEMO-ES", help="TRUSTED LOCAL legal entity")
            child.add_argument("--actor", default="approver-demo" if command == "approve" else "preparer-demo",
                               help="TRUSTED LOCAL actor; production authentication is not implemented")
        if command not in {"demo", "submit"}:
            child.add_argument("job_id")
        if command == "submit":
            child.add_argument("invoice_file", type=Path)
        if command == "approve":
            child.add_argument("--version", type=int, required=True)
            child.add_argument("--hash", required=True)
        if command == "post":
            child.add_argument("--mock-fault", choices=sorted(SQLiteMockERP.FAULTS), default="none")
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    args.erp_db = args.erp_db or Path(str(args.db) + ".erp.db")
    try:
        if args.db.resolve() == args.erp_db.resolve():
            raise GuardError("SEPARATE_MOCK_DATABASE_REQUIRED")
        config = load_json(args.config)
        erp = SQLiteMockERP(args.erp_db, fault=getattr(args, "mock_fault", "none"))
        engine = InvoiceEngine(args.db, config, erp)
        if args.command == "demo":
            demo(args, engine)
        else:
            caller = TrustedCaller(args.customer, args.entity, args.actor)
            if args.command == "submit":
                response = engine.submit(caller, load_json(args.invoice_file))
            elif args.command == "approve":
                response = engine.approve(caller, args.job_id, expected_version=args.version, expected_hash=args.hash)
            else:
                response = getattr(engine, args.command)(caller, args.job_id)
            emit(response)
        return 0
    except SimulatedCrash:
        emit({"error": "SIMULATED_PROCESS_CRASH", "next_action": "status_then_reconcile_never_blind_retry"})
        return 3
    except GuardError as exc:
        emit({"error": exc.code})
        return 2
    except (OSError, sqlite3.OperationalError):
        # Do not print paths, invoice content, SQL arguments, or exception messages.
        emit({"error": "LOCAL_OPERATION_FAILED", "next_action": "inspect_local_state_and_reconcile_if_posting"})
        return 1


if __name__ == "__main__":
    sys.exit(main())
