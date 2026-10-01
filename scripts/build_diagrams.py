"""Build the English solution guide and icon architecture from local sources."""
from pathlib import Path
import base64
import html
import json

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "docs"
ASSETS = OUT / "assets"
WIDTH, HEIGHT = 1600, 1280
elements, files = [], {}
svg = [
    f'<svg xmlns="http://www.w3.org/2000/svg" width="{WIDTH}" height="{HEIGHT}" viewBox="0 0 {WIDTH} {HEIGHT}" role="img" aria-labelledby="title desc">',
    '<title id="title">Invoice Assistant: Copilot Studio new harness</title>',
    '<desc id="desc">One agent, modular workflows, human review, optional Azure tools and Dynamics 365, SAP or other ERP adapters.</desc>',
    '<rect width="100%" height="100%" fill="#ffffff"/>',
    '<defs><marker id="arrow" markerWidth="10" markerHeight="10" refX="9" refY="3" orient="auto"><path d="M0,0 L0,6 L9,3 z" fill="#5c5c5c"/></marker></defs>',
]


def common(identifier, kind, x, y, width, height):
    return dict(id=identifier, type=kind, x=x, y=y, width=width, height=height,
                angle=0, strokeColor="#919191", backgroundColor="transparent",
                fillStyle="solid", strokeWidth=2, strokeStyle="solid",
                roughness=0, opacity=100, groupIds=[], frameId=None,
                roundness=None, seed=len(elements) + 1, version=1,
                versionNonce=len(elements) + 10, isDeleted=False, boundElements=[],
                updated=0, link=None, locked=False)


def label(identifier, x, y, text, size=20, width=500):
    lines = text.split("\n")
    el = common(identifier, "text", x, y, width, size * 2.5 * len(lines))
    el.update(text=text, originalText=text, fontSize=size, fontFamily=2,
              strokeColor="#000000", textAlign="left", verticalAlign="top",
              containerId=None, autoResize=True, lineHeight=1.25)
    elements.append(el)
    for index, line in enumerate(lines):
        svg.append(f'<text x="{x}" y="{y + size + index * size * 1.4}" font-family="Segoe UI,Arial,sans-serif" font-size="{size}" fill="#000000">{html.escape(line)}</text>')


def box(identifier, x, y, width, height, container=False, accent=False):
    stroke = "#b11f4b" if accent else "#919191"
    fill = "transparent" if container else "#fcfbf8"
    el = common(identifier, "rectangle", x, y, width, height)
    el.update(strokeColor=stroke, backgroundColor=fill, roundness={"type": 3})
    elements.append(el)
    svg.append(f'<rect x="{x}" y="{y}" width="{width}" height="{height}" rx="12" fill="{"none" if container else fill}" stroke="{stroke}" stroke-width="2"/>')


def icon(identifier, filename, x, y, size=64):
    raw = (ASSETS / filename).read_bytes()
    url = "data:image/svg+xml;base64," + base64.b64encode(raw).decode("ascii")
    file_id = filename.removesuffix(".svg")
    files[file_id] = {"id":file_id,"mimeType":"image/svg+xml","dataURL":url,"created":0,"lastRetrieved":0}
    el = common(identifier, "image", x, y, size, size)
    el.update(fileId=file_id, status="saved", scale=[1, 1], crop=None)
    elements.append(el)
    svg.append(f'<image x="{x}" y="{y}" width="{size}" height="{size}" href="{url}" preserveAspectRatio="xMidYMid meet"/>')


def arrow(identifier, x1, y1, x2, y2, dashed=False):
    el = common(identifier, "arrow", x1, y1, abs(x2-x1), abs(y2-y1))
    el.update(points=[[0, 0], [x2-x1, y2-y1]], startArrowhead=None,
              endArrowhead="arrow", startBinding=None, endBinding=None,
              elbowed=False, strokeStyle="dashed" if dashed else "solid",
              strokeColor="#5c5c5c")
    elements.append(el)
    dash = ' stroke-dasharray="7,5"' if dashed else ""
    svg.append(f'<line x1="{x1}" y1="{y1}" x2="{x2}" y2="{y2}" stroke="#5c5c5c" stroke-width="2"{dash} marker-end="url(#arrow)"/>')


label("heading", 40, 20, "Invoice Assistant | Start with the customer goal", 32, 1500)
label("subheading", 40, 80, "Choose the workflows, tools and rules each customer needs. Pause unusual cases for a person.", 22, 1500)

box("harness", 320, 150, 950, 760, container=True, accent=True)
icon("studio-icon", "copilot-studio.svg", 345, 170, 66)
label("studio-title", 430, 170, "Microsoft Copilot Studio", 28, 780)
label("harness-name", 430, 215, "New GitHub Copilot harness", 20, 780)

box("agent", 355, 280, 880, 110, accent=True)
label("agent-name", 380, 295, "ONE INVOICE AGENT", 22, 800)
label("agent-role", 380, 335, "Answers questions  |  Explains blockers  |  Requests the right next action", 20, 820)
label("optional", 355, 410, "OPTIONAL WORKFLOWS - enable independently or combine", 19, 880)

for identifier, x, title, subtitle in [
    ("capture", 355, "1. Capture and check", "Less manual entry\nRead fields; check duplicates\nCreate ready invoice or review"),
    ("approve", 655, "2. Resolve and approve", "Faster decisions\nRoute to the right person\nRecord authorized approval"),
    ("register", 955, "3. Register and track", "Less finance hand-off\nSubmit through approved tools\nConfirm the finance result"),
]:
    box(identifier, x, 455, 280, 175)
    label(identifier+"-title", x+14, 475, title, 20, 254)
    label(identifier+"-detail", x+14, 520, subtitle, 16, 254)
arrow("capture-to-approve", 636, 548, 653, 548, True)
arrow("approve-to-register", 936, 548, 953, 548, True)
for index, x in enumerate((495, 795, 1095)):
    arrow(f"review-branch-{index}", x, 632, x, 660, True)
arrow("review-join", 495, 660, 1095, 660, True)
arrow("review-gate", 795, 662, 795, 708, True)
label("review-stage", 1110, 649, "Any stage", 16, 120)
box("human-review", 355, 710, 880, 140, accent=True)
icon("human-review-icon", "human-review.svg", 375, 750, 62)
label("human-review-title", 460, 725, "HUMAN IN THE LOOP", 22, 735)
label("human-review-detail", 460, 768, "Uncertain fields, unusual amounts, duplicate / fraud flags\nPause > assigned reviewer > correct / reject / escalate > revalidate", 17, 735)
label("no-mandatory-chain", 355, 875, "Plug in customer rules and tools; keep the same three business workflows.", 17, 900)

box("sources", 35, 280, 235, 350)
label("sources-title", 55, 298, "Invoice sources", 22, 195)
icon("mail-icon", "mail.svg", 62, 355, 55)
label("mail-label", 62, 420, "Email attachments", 18, 190)
icon("document-icon", "documents.svg", 62, 475, 55)
label("doc-label", 62, 540, "SharePoint / upload\nExisting invoice records", 17, 190)
arrow("source-to-studio", 272, 505, 353, 505)

box("finance", 1320, 280, 245, 350)
label("finance-title", 1340, 298, "Customer finance", 22, 205)
icon("finance-icon", "finance.svg", 1340, 348, 48)
label("finance-name", 1400, 350, "Dynamics 365\nFinance", 17, 145)
icon("erp-adapter-icon", "erp-adapter.svg", 1340, 420, 42)
label("erp-options", 1395, 417, "SAP / other ERPs\nOptional adapters", 16, 150)
label("finance-detail", 1340, 500, "Choose the customer's ERP\nApproved API / connector\nRegistration and status\nNo payment execution", 16, 210)
arrow("studio-to-finance", 1237, 505, 1318, 505)

box("tools", 320, 960, 1245, 205, container=True)
label("tools-title", 345, 975, "CONNECTED TOOLS - select for the customer's needs and total cost", 19, 1160)
icon("ai-icon", "ai-builder.svg", 345, 1030, 58)
label("ai-name", 420, 1030, "AI Builder", 20, 195)
label("ai-detail", 420, 1066, "Extraction", 16, 195)
icon("dataverse-icon", "dataverse.svg", 635, 1030, 58)
label("data-name", 710, 1030, "Dataverse", 20, 200)
label("data-detail", 710, 1066, "Invoice case state", 16, 200)
icon("azure-extraction-icon", "azure-document-intelligence.svg", 950, 1030, 52)
icon("azure-storage-icon", "azure-blob.svg", 1010, 1030, 52)
label("azure-name", 1080, 1023, "Optional Azure tools", 20, 450)
label("azure-detail", 1080, 1060, "Document Intelligence / Blob Storage", 16, 450)
label("cost-gate", 345, 1120, "Choose Azure only if processing, storage, review and support total cost is lower; savings are not guaranteed.", 17, 1190)
arrow("tool-access", 800, 912, 800, 958)

label("value-footer", 40, 1200, "Modular by design: sources | extraction | rules | approvals | finance | archive", 21, 1510)
label("platform-footer", 40, 1240, "Copilot Studio keeps orchestration. New customer requirements extend selected steps, not duplicate the solution.", 18, 1510)
svg_text = "\n".join(svg + ["</svg>"])
OUT.mkdir(parents=True, exist_ok=True)
(OUT / "architecture.svg").write_text(svg_text, encoding="utf-8")
(OUT / "architecture.excalidraw").write_text(json.dumps({
    "type":"excalidraw", "version":2, "source":"invoice-automation-copilot-studio",
    "elements":elements, "appState":{"viewBackgroundColor":"#ffffff","gridSize":None},
    "files":files
}, ensure_ascii=True, indent=2), encoding="utf-8")
template = (OUT / "solution.html.in").read_text(encoding="utf-8")
assert template.count("@@ARCHITECTURE@@") == 1
guide = template.replace("@@ARCHITECTURE@@", base64.b64encode(svg_text.encode()).decode("ascii"))
(OUT / "index.html").write_text(guide, encoding="utf-8")
print(f"Generated English guide, SVG and editable architecture with {len(files)} tool images.")
