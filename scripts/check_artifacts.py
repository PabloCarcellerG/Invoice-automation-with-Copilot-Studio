"""Check repository design artifacts without external packages or network calls."""
from html.parser import HTMLParser
from pathlib import Path
import json
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]


class GuideParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.ids = set()
        self.anchors = []
        self.sections = 0

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if "id" in attributes:
            identifier = attributes["id"]
            assert identifier not in self.ids, f"Duplicate HTML id: {identifier}"
            self.ids.add(identifier)
        if tag == "a" and attributes.get("href", "").startswith("#"):
            self.anchors.append(attributes["href"][1:])
        if tag == "script":
            assert "src" not in attributes, "Guide must not depend on external scripts"
        if tag == "section":
            self.sections += 1


def validate_customer_design(goal, design, catalog, modules):
    """Check design consistency only; this does not grant permissions or deploy modules."""
    workflow_ids = {item["id"] for item in catalog["workflows"]}
    enabled = goal["enabledWorkflows"]
    assert goal["goal"] in design["goals"], "Unknown customer goal"
    assert len(enabled) == len(set(enabled)), "Duplicate workflow selection"
    assert set(enabled).issubset(workflow_ids), "Unknown workflow selection"
    assert goal["goal"] != "visibility" or not enabled, "Visibility must remain read-only"
    choices = goal["toolChoices"]
    assert set(choices) == set(modules["toolOptions"]), "Missing or unknown tool category"
    for key, value in choices.items():
        assert value in modules["toolOptions"][key], f"Unsupported {key} tool"
    if "register-track" in enabled:
        assert choices["finance"] != "none", "Registration requires a finance adapter"
    if any(value.startswith("azure-") for value in choices.values()):
        cost_gate = goal["optionalAzureCostGate"]
        assert cost_gate["requiredBeforeSelection"] is True, "Azure cost gate is required"
        reference = cost_gate["comparisonReference"]
        assert isinstance(reference, str) and reference.strip(), "Azure cost comparison reference is required"
    review = goal["humanReview"]
    assert review["enabled"] is True, "Human review cannot be disabled"
    assert isinstance(review["queue"], str) and review["queue"].strip(), "Human review needs an owner queue"
    assert review["missingOwner"] == "BLOCK_AND_ESCALATE", "Missing review ownership must block"
    assert review["timeoutAction"] == "ESCALATE_WITHOUT_APPROVING", "Review timeout cannot approve"
    assert review["resumePolicy"] == "AUTHORIZED_DECISION_THEN_REVALIDATE", "Review resume must revalidate"
    assert review["materialChangeInvalidatesApproval"] is True, "Material changes must invalidate approval"
    assert review["allowsUnknownOutcomeRetry"] is False, "Unknown finance outcomes cannot be blindly retried"
    slots = {slot["id"]: slot for slot in modules["slots"]}
    examples = {item["id"]: item for item in modules["extensionExamples"]}
    capabilities = set(goal["availableCapabilities"])
    selected = set()
    for extension in goal["extensions"]:
        identifier = extension["id"]
        assert identifier in examples and identifier not in selected, "Unregistered or duplicate extension"
        module = examples[identifier]
        assert extension["version"] == module["version"], "Unsupported module version"
        assert slots[module["slot"]]["workflow"] in enabled, "Extension requires its host workflow"
        assert set(module["requires"]).issubset(capabilities), "Missing module capability"
        selected.add(identifier)


def main():
    json_files = []
    for folder in ("agents", "workflows", "modules", "contracts", "config", "schemas", "examples"):
        for path in (ROOT / folder).rglob("*.json"):
            try:
                json.loads(path.read_text(encoding="utf-8"))
            except json.JSONDecodeError as error:
                raise ValueError(f"Invalid JSON in {path.relative_to(ROOT)}: {error}") from error
            json_files.append(path)
    guide = GuideParser()
    guide.feed((ROOT / "docs" / "index.html").read_text(encoding="utf-8"))
    assert guide.sections == 4, f"Expected 4 guide sections, got {guide.sections}"
    assert not set(guide.anchors) - guide.ids, "Broken guide anchor"
    catalog = json.loads((ROOT / "workflows" / "catalog.json").read_text(encoding="utf-8"))
    assert catalog["artifactKind"] == "DESIGN_ONLY_NOT_IMPORTABLE"
    assert [item["id"] for item in catalog["workflows"]] == [
        "capture-check", "resolve-approve", "register-track"
    ]
    for workflow in catalog["workflows"]:
        assert all(workflow[key] for key in ("name", "trigger", "steps", "persists", "errors", "owner"))
    drawing = json.loads((ROOT / "docs" / "architecture.excalidraw").read_text(encoding="utf-8"))
    assert drawing["type"] == "excalidraw" and drawing["version"] == 2
    elements = drawing["elements"]
    assert len({element["id"] for element in elements}) == len(elements)
    for element in elements:
        assert element["width"] >= 0 and element["height"] >= 0
        if element["type"] == "text":
            assert element["width"] > 0 and element["height"] > 0
            assert element["strokeColor"] == "#000000"
        if element["type"] == "image":
            assert element["fileId"] in drawing["files"]
            assert drawing["files"][element["fileId"]]["dataURL"].startswith("data:image/svg+xml;base64,")
    assert set(drawing["files"]) == {
        "copilot-studio", "mail", "documents", "finance", "ai-builder", "dataverse",
        "human-review", "erp-adapter", "azure-document-intelligence", "azure-blob"
    }
    goal = json.loads((ROOT / "config" / "customer-goal.example.json").read_text(encoding="utf-8"))
    design = json.loads((ROOT / "agents" / "design.json").read_text(encoding="utf-8"))
    modules = json.loads((ROOT / "modules" / "catalog.json").read_text(encoding="utf-8"))
    validate_customer_design(goal, design, catalog, modules)
    slot_ids = {slot["id"]: slot for slot in modules["slots"]}
    for workflow in catalog["workflows"]:
        assert workflow["humanReviewGate"] == "shared"
        for slot in workflow["extensionPoints"]:
            assert slot_ids[slot]["workflow"] == workflow["id"]
    erp = json.loads((ROOT / "contracts" / "erp-adapter.json").read_text(encoding="utf-8"))
    assert set(modules["toolOptions"]["finance"]) - {"none"} == {adapter["id"] for adapter in erp["adapterChoices"]}
    for selection in design["goals"].values():
        assert set(selection["workflows"]).issubset({item["id"] for item in catalog["workflows"]})
    ET.parse(ROOT / "docs" / "architecture.svg")
    api = json.loads((ROOT / "contracts" / "service.openapi.json").read_text(encoding="utf-8"))
    assert api["openapi"] == "3.0.3"
    assert api["security"] == [{"entraBearer": []}]
    assert all("pay" not in path and "post-erp" not in path for path in api["paths"])
    print(f"Design artifacts OK: {len(json_files)} JSON files, {guide.sections} sections, "
          f"{len(catalog['workflows'])} workflows, {len(elements)} diagram elements.")


if __name__ == "__main__":
    main()
