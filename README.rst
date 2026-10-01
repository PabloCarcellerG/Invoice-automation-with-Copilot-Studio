Invoice Assistant for Copilot Studio
===================================

A reusable, goal-led solution using the new GitHub Copilot harness in Copilot
Studio: one agent and up to three composable workflows. Version 0.3.0.

Start with the customer's outcome
--------------------------------

======================= =================================== ======================
Customer goal           Enable                              Measure
======================= =================================== ======================
Faster invoice answers  Agent with read-only tools          Time spent finding status
Less manual entry       Capture and check                   Minutes per invoice
Faster approvals        Resolve and approve                 Waiting time and follow-ups
End-to-end handling     Combine all three workflows         Cost per confirmed invoice
======================= =================================== ======================

The third workflow is Register and track. It can also connect to an existing
approved-invoice process without replacing the customer's intake or approvals.
Do not deploy every module by default.

Open ``docs/index.html`` for the short solution guide, tool-icon architecture,
value/cost calculator and setup instructions. The editable diagram is
``docs/architecture.excalidraw``.

Architecture
-------------

What to reuse
-------------

Reuse the agent instructions, three workflow specifications, tool contracts and
control tests. Change the input channel, records, finance connector, approval
rules and cost budget per customer. Keep customer data and credentials separate.

``modules/catalog.json`` defines extension points and optional tool choices;
``contracts/module-contract.json`` defines module outcomes and the human-review
gate. Add a versioned PO check, country rule or approval-level module to an
existing step instead of cloning the workflow for each customer. This is a
modular design contract, not an implemented dynamic plugin loader.

Choose Dynamics 365, SAP or another ERP through the same finance contract.
Each adapter still requires customer-specific implementation, authentication
and reliable submission/outcome semantics.

Optional Azure Document Intelligence and Blob Storage can be selected as
extraction/document-storage tools. Compare total cost, quality, integration
and support before selecting them; savings are not guaranteed. Copilot Studio
remains the orchestration platform, and Dataverse remains the case-state store
in this reference design.

Every selected workflow must pause uncertain or unusual cases for an assigned
human reviewer, even when the full approval workflow is not enabled. Correct,
reject or escalate; revalidate before resuming. Material changes invalidate
approvals. Review timeouts never approve, and no reviewer can authorize a blind
retry of an unknown finance outcome.

Use extraction only when structured fields are unavailable. Invoke the agent
for useful interactions and exceptions rather than every processing step.
Track total monthly cost, handling time and confirmed outcomes. Approval and
duplicate controls remain mandatory when their operations are enabled.

Optional local developer demo
-----------------------------

Python 3.11+, no dependencies. From this repository root in PowerShell::

    $env:PYTHONPATH = "src"
    python -m invoice_accelerator demo --db "$env:LOCALAPPDATA\InvoiceAutomationDemo\demo.db"

This tests a small local state machine with synthetic data and a mock finance
system. It does not run or deploy Copilot Studio. Local caller IDs are trusted
test inputs, not authentication.

The demo supports positive, complete PO invoices in EUR with synthetic NO_TAX
data. It rejects non-PO, credits, partial receipts, other currencies/tax and
amendments. Production variants need customer-specific implementation.
No payments or supplier bank changes are executed.

Developer checks::

    python -m unittest discover -s tests -v
    python scripts\build_diagrams.py
    python scripts\check_artifacts.py

Repository contents
-------------------

* ``agents/`` - concise agent instructions and tool design.
* ``workflows/`` - three optional workflow specifications.
* ``modules/`` - versioned extension points, tool choices and module examples.
* ``config/`` - customer-goal template and separate local-demo configuration.
* ``contracts/`` - module/review and connected-operation contracts, not deployed APIs.
* ``docs/`` - English solution guide, architecture and official tool images.
* ``src/`` and ``tests/`` - optional local reference engine.

The local Git repository has no remote or published GitHub URL. Confirm owner,
visibility and distribution license before publishing. Microsoft icons are
included only for architecture/documentation under their stated usage terms;
see ``docs/assets/NOTICE.rst``.
