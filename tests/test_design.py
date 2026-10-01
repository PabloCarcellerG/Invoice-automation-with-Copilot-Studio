"""Configuration consistency for the modular design, not runtime authorization."""
import copy
import json
from pathlib import Path
import unittest

from scripts.check_artifacts import validate_customer_design

ROOT = Path(__file__).resolve().parents[1]


def read(relative_path):
    return json.loads((ROOT / relative_path).read_text(encoding="utf-8"))


class DesignConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.goal = read(Path("config") / "customer-goal.example.json")
        self.design = read(Path("agents") / "design.json")
        self.catalog = read(Path("workflows") / "catalog.json")
        self.modules = read(Path("modules") / "catalog.json")

    def validate(self, goal=None):
        validate_customer_design(
            self.goal if goal is None else goal, self.design, self.catalog, self.modules
        )

    def test_default_capture_design(self):
        self.validate()

    def test_visibility_remains_read_only(self):
        self.goal.update(goal="visibility", enabledWorkflows=[])
        self.validate()
        self.goal["enabledWorkflows"] = ["capture-check"]
        with self.assertRaisesRegex(AssertionError, "read-only"):
            self.validate()

    def test_customer_can_combine_modules_without_using_goal_defaults(self):
        self.goal["enabledWorkflows"] = ["capture-check", "resolve-approve"]
        self.validate()

    def test_sap_and_other_erp_are_optional_registration_adapters(self):
        self.goal.update(goal="end_to_end", enabledWorkflows=["register-track"])
        with self.assertRaisesRegex(AssertionError, "finance adapter"):
            self.validate()
        for adapter in ("dynamics-365", "sap", "other-erp"):
            with self.subTest(adapter=adapter):
                self.goal["toolChoices"]["finance"] = adapter
                self.validate()

    def test_azure_tools_require_recorded_cost_comparison(self):
        self.goal["toolChoices"]["extraction"] = "azure-document-intelligence"
        self.goal["toolChoices"]["documentStorage"] = "azure-blob"
        with self.assertRaisesRegex(AssertionError, "cost comparison"):
            self.validate()
        self.goal["optionalAzureCostGate"]["comparisonReference"] = "customer-assessed-case-v1"
        self.validate()

    def test_extensions_require_declared_version_dependencies_and_host(self):
        self.goal["extensions"] = [{"id": "po-match", "version": "1.0"}]
        with self.assertRaisesRegex(AssertionError, "capability"):
            self.validate()
        self.goal["availableCapabilities"] = ["finance.GetPurchaseOrder", "finance.GetReceipts"]
        self.validate()
        self.goal["extensions"][0]["version"] = "2.0"
        with self.assertRaisesRegex(AssertionError, "version"):
            self.validate()
        self.goal["extensions"][0]["version"] = "1.0"
        self.goal["enabledWorkflows"] = ["resolve-approve"]
        with self.assertRaisesRegex(AssertionError, "host workflow"):
            self.validate()

    def test_human_review_cannot_fail_open(self):
        for key, value in (
            ("enabled", False), ("queue", ""), ("missingOwner", "CONTINUE"),
            ("timeoutAction", "APPROVE"), ("resumePolicy", "SKIP_VALIDATION"),
            ("materialChangeInvalidatesApproval", False), ("allowsUnknownOutcomeRetry", True),
        ):
            with self.subTest(key=key):
                goal = copy.deepcopy(self.goal)
                goal["humanReview"][key] = value
                with self.assertRaises(AssertionError):
                    self.validate(goal)

    def test_unknown_or_duplicate_extensions_are_rejected(self):
        self.goal["extensions"] = [{"id": "unknown-module", "version": "1.0"}]
        with self.assertRaisesRegex(AssertionError, "Unregistered"):
            self.validate()
        self.goal["availableCapabilities"] = ["finance.GetPurchaseOrder", "finance.GetReceipts"]
        self.goal["extensions"] = [{"id": "po-match", "version": "1.0"}] * 2
        with self.assertRaisesRegex(AssertionError, "duplicate"):
            self.validate()


if __name__ == "__main__":
    unittest.main()
