# Agentic AI Inventory Replenishment System – Phase III Partial Implementation

Implements Step 11 of the Project Management Plan: Phase I and Phase II design
(BPMN 4.1–4.5, UC-01 to UC-13, Section 6 agent prompts, Section 7 screens,
Section 8 database, Section 9 method contracts MC-01 to MC-05).

## Run
```bash
python3 -m pip install -r requirements.txt
python3 -m app.web           # open http://127.0.0.1:5001
```
Delete `replenishment.db` to reset the sample data.

## Test
```bash
python3 -m pytest -v tests    # 33 tests, each traced to a requirement / design element
```

## Demo script (≈3 minutes)
1. **Inventory Status** → *Run AI inventory monitoring* (6.1): 4 products flagged, 1 No Action.
2. *Evaluate* BEV-1001 (6.2, MC-02, MC-03) → AI-drafted request: 80 units from FreshFlow (sized for its 5-day lead time), calculation shown, QuickMart excluded (not approved).
3. *Evaluate* HOM-4004 → Flagged for Review (no feasible approved supplier).
4. Try *Reject* with no comment → blocked (rule 5). Then *Approve* (MC-01) → PO validated (MC-04) and submitted.
5. Switch user to **Sam Carter – Warehouse Employee** → *Receive Shipment* PO 1, enter 70 received / 4 damaged (MC-05) → 66 added to stock, DAMAGED discrepancy escalated.
6. Switch back to the manager → **Orders & Exceptions** → *Simulate overdue* → *Run AI order review* (6.4) → delay escalated with a draft supplier message → *Approve & send message* / *Mark resolved*.

## Code map
| File | Design element |
|---|---|
| `app/schema.sql` | Section 8.3 tables + 8.4 constraints (CHECK / UNIQUE / FK) |
| `app/services.py` | Control classes 8.5b; MC-01 … MC-05 (Section 9) |
| `app/agent.py` | `AIReplenishmentAgent` – Section 6.1–6.4 prompts |
| `app/rules.py` | Supplied business rules (formula window, weights, receiving policy) |
| `app/web.py`, `app/templates/` | Section 7.2 screens |
| `tests/test_phase3.py` | Integration and requirement tests T-01 … T-33 |
