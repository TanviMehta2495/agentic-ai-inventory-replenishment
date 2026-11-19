"""Phase III integration tests: each test is traced to a Phase I/II requirement or design element.
ID format: T-xx | requirement / design reference."""
import sqlite3
from datetime import date, timedelta

import pytest

from app.agent import mark_resolved
from app.services import (ApprovalController, BusinessRuleError, PurchaseOrderService, ReceivingService,
                          ReplenishmentEvaluationService, SupplierComparisonService)
from app.web import create_app
from conftest import MANAGER, WAREHOUSE, one, stock


def approve_product(conn, agent, pid=1):
    rid = agent.evaluateReplenishment(pid)
    res = ApprovalController(conn, agent).recordDecision(rid, "APPROVE", "", MANAGER)
    return rid, res


# ---------------------------------------------------------------- MC-02 calculateReorderQuantity
def test_T01_MC02_reorder_quantity_formula(conn):
    """UC-02/UC-03, MC-02: target = avg demand × longest eligible lead time + SafetyStock − stock position."""
    calc = ReplenishmentEvaluationService(conn).calculateReorderQuantity(1)
    assert calc.averageDailyDemand == 12 and calc.leadTime == 9 and calc.stockPosition == 40
    assert calc.targetLevel == 168 and calc.requiredQty == 128
    assert "168" in calc.reason  # calculation shown to the manager (6.2)


def test_T02_MC02_no_replenishment_above_reorder_level(conn):
    """UC-02 step 5: stock position above ReorderLevel returns 0."""
    calc = ReplenishmentEvaluationService(conn).calculateReorderQuantity(3)
    assert calc.requiredQty == 0 and not calc.flagged


def test_T03_MC02_no_feasible_supplier_is_flagged(conn):
    """UC-04 alternate flow: no feasible approved supplier -> flagged."""
    calc = ReplenishmentEvaluationService(conn).calculateReorderQuantity(4)
    assert calc.flagged and "No feasible approved supplier" in calc.reason


def test_T04_MC02_open_orders_reduce_requirement(conn, agent):
    """UC-02 step 3: outstanding order quantity counts toward the stock position (no double ordering)."""
    approve_product(conn, agent, 1)  # submits PO for 80 units
    calc = ReplenishmentEvaluationService(conn).calculateReorderQuantity(1)
    assert calc.openOrderQty == 80 and calc.stockPosition == 120
    rid = agent.evaluateReplenishment(1)
    assert one(conn, "SELECT Status FROM ReplenishmentRecommendation WHERE RecommendationID=?", rid)[0] \
        == "NO_REPLENISHMENT"


def test_T05_MC02_missing_inventory_is_flagged(conn):
    """Section 6 general instruction: do not invent missing information."""
    with conn:
        conn.execute("DELETE FROM Inventory WHERE ProductID=2")
    calc = ReplenishmentEvaluationService(conn).calculateReorderQuantity(2)
    assert calc.flagged and calc.reason == "Missing inventory data"


# ---------------------------------------------------------------- MC-03 rankSuppliers
def test_T06_MC03_unapproved_supplier_excluded_and_ranked(conn):
    """UC-04 / Phase I constraint: only approved suppliers are considered."""
    sc = SupplierComparisonService(conn)
    ranked, excluded = sc.rankSuppliers(sc.candidates(1), 128)
    assert [s["SupplierName"] for s in ranked] == ["FreshFlow Distributors", "ValueSource Wholesale"]
    assert excluded[0]["SupplierName"] == "QuickMart Supply" and excluded[0]["reason"] == "Supplier not approved"
    assert ranked[0]["score"] >= ranked[1]["score"]


def test_T07_MC03_moq_too_high_excluded(conn):
    sc = SupplierComparisonService(conn)
    ranked, excluded = sc.rankSuppliers(sc.candidates(2), 112)
    assert [s["SupplierID"] for s in ranked] == [1]
    assert excluded[0]["reason"] == "Minimum order quantity too high"


def test_T08_MC03_moq_raises_order_within_allowance(conn):
    sc = SupplierComparisonService(conn)
    ranked, _ = sc.rankSuppliers(sc.candidates(5), 185)
    vs = next(s for s in ranked if s["SupplierID"] == 2)
    assert vs["orderQty"] == 200 and vs["estimatedCost"] == 98.0


def test_T09_MC03_precondition_required_qty_positive(conn):
    sc = SupplierComparisonService(conn)
    with pytest.raises(BusinessRuleError):
        sc.rankSuppliers(sc.candidates(1), 0)


def test_T10_MC03_every_candidate_in_exactly_one_list(conn):
    sc = SupplierComparisonService(conn)
    for pid in range(1, 6):
        cands = sc.candidates(pid)
        ranked, excluded = sc.rankSuppliers(cands, 100)
        assert sorted(s["SupplierID"] for s in ranked + excluded) == sorted(c["SupplierID"] for c in cands)


# ---------------------------------------------------------------- 6.2 Replenishment Evaluation (agent)
def test_T11_agent_evaluation_outcomes_and_audit(conn, agent):
    """BPMN 4.2 outcomes + Section 8.4 rule 12 (every AI output audited)."""
    statuses = {}
    for pid in range(1, 6):
        rid = agent.evaluateReplenishment(pid)
        statuses[pid] = one(conn, "SELECT Status FROM ReplenishmentRecommendation WHERE RecommendationID=?", rid)[0]
        assert one(conn, "SELECT COUNT(*) FROM AgentDecision WHERE AgentType='REPLENISHMENT_EVALUATION'"
                         " AND RelatedRecordID=?", str(rid))[0] == 1
    assert statuses == {1: "PENDING_APPROVAL", 2: "PENDING_APPROVAL", 3: "NO_REPLENISHMENT",
                        4: "FLAGGED_FOR_REVIEW", 5: "PENDING_APPROVAL"}


def test_T12_rule4_pending_request_requires_supplier_qty_cost(conn):
    """Section 8.4 rule 4 is enforced by the database itself."""
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO ReplenishmentRecommendation (ProductID, Reason, Status, CreatedAt, CreatedByAgent)"
                     " VALUES (1,'x','PENDING_APPROVAL','2026-01-01','t')")


# ---------------------------------------------------------------- MC-01 recordDecision
def test_T13_MC01_approve_creates_validated_submitted_order(conn, agent):
    """UC-05 → UC-06 → UC-07, Figure 7.1 approve path."""
    rid, res = approve_product(conn, agent, 1)
    assert res.requestStatus == "APPROVED" and res.orderStatus == "SUBMITTED"
    po = one(conn, "SELECT * FROM PurchaseOrder WHERE POID=?", res.poId)
    assert po["RecommendationID"] == rid and po["TotalAmount"] == 512.0 and po["SupplierID"] == 1
    assert one(conn, "SELECT COUNT(*) FROM SupplierOutbox WHERE POID=?", res.poId)[0] == 1
    appr = one(conn, "SELECT * FROM Approval WHERE RecommendationID=?", rid)
    assert appr["ApproverUserID"] == MANAGER and appr["Decision"] == "APPROVE"


def test_T14_MC01_reject_requires_comments(conn, agent):
    """Section 8.4 rule 5 / MC-01 precondition."""
    rid = agent.evaluateReplenishment(1)
    with pytest.raises(BusinessRuleError, match="reason"):
        ApprovalController(conn, agent).recordDecision(rid, "REJECT", "  ", MANAGER)
    assert one(conn, "SELECT COUNT(*) FROM Approval")[0] == 0
    res = ApprovalController(conn, agent).recordDecision(rid, "REJECT", "Budget freeze", MANAGER)
    assert res.requestStatus == "REJECTED" and res.poId is None


def test_T15_MC01_revise_creates_no_order(conn, agent):
    rid = agent.evaluateReplenishment(2)
    res = ApprovalController(conn, agent).recordDecision(rid, "REVISE", "Order 80 instead", MANAGER)
    assert res.requestStatus == "REVISION_REQUESTED"
    assert one(conn, "SELECT COUNT(*) FROM PurchaseOrder")[0] == 0


def test_T16_MC01_only_store_manager_can_decide(conn, agent):
    """Phase I constraint: human approval by the store manager."""
    rid = agent.evaluateReplenishment(1)
    with pytest.raises(BusinessRuleError, match="store manager"):
        ApprovalController(conn, agent).recordDecision(rid, "APPROVE", "", WAREHOUSE)


def test_T17_MC01_one_decision_per_request(conn, agent):
    rid, _ = approve_product(conn, agent, 1)
    with pytest.raises(BusinessRuleError, match="not waiting"):
        ApprovalController(conn, agent).recordDecision(rid, "REJECT", "again", MANAGER)


def test_T18_MC01_MC04_price_change_after_approval_puts_order_on_hold(conn, agent):
    """MC-04 note: a price changed after approval must not be sent to the supplier."""
    rid = agent.evaluateReplenishment(1)
    with conn:
        conn.execute("UPDATE ProductSupplier SET UnitPrice=7.10 WHERE ProductID=1 AND SupplierID=1")
    res = ApprovalController(conn, agent).recordDecision(rid, "APPROVE", "", MANAGER)
    assert res.requestStatus == "APPROVED" and res.orderStatus == "ON_HOLD"
    assert "approved estimated cost" in res.message
    assert one(conn, "SELECT COUNT(*) FROM SupplierOutbox")[0] == 0


# ---------------------------------------------------------------- MC-04 validatePurchaseOrder
def test_T19_MC04_detects_tampered_draft(conn, agent):
    rid = agent.evaluateReplenishment(1)
    ctrl = ApprovalController(conn, agent)
    with conn:  # record approval only, then let the agent draft
        conn.execute("INSERT INTO Approval (RecommendationID, ApproverUserID, Decision, DecisionDate) VALUES (?,?,?,?)",
                     (rid, MANAGER, "APPROVE", "2026-11-17"))
        conn.execute("UPDATE ReplenishmentRecommendation SET Status='APPROVED' WHERE RecommendationID=?", (rid,))
    po_id = agent.preparePurchaseOrder(rid)
    with conn:
        conn.execute("UPDATE PurchaseOrderItem SET Quantity=500 WHERE POID=?", (po_id,))
    ok, problems = PurchaseOrderService(conn, agent).validatePurchaseOrder(po_id)
    assert not ok
    assert "Quantity differs from approved quantity" in problems and "Line amount is wrong" in problems


def test_T20_MC04_valid_draft_passes(conn, agent):
    rid = agent.evaluateReplenishment(5)
    with conn:
        conn.execute("INSERT INTO Approval (RecommendationID, ApproverUserID, Decision, DecisionDate) VALUES (?,?,?,?)",
                     (rid, MANAGER, "APPROVE", "2026-11-17"))
        conn.execute("UPDATE ReplenishmentRecommendation SET Status='APPROVED' WHERE RecommendationID=?", (rid,))
    po_id = agent.preparePurchaseOrder(rid)
    assert PurchaseOrderService(conn, agent).validatePurchaseOrder(po_id) == (True, [])


# ---------------------------------------------------------------- MC-05 updateInventoryFromAcceptedReceipt
def receive(conn, agent, po_id, received, damaged=0, user=WAREHOUSE):
    item = one(conn, "SELECT POItemID FROM PurchaseOrderItem WHERE POID=?", po_id)[0]
    return ReceivingService(conn, agent).recordReceipt(
        po_id, [{"POItemID": item, "ReceivedQuantity": received, "DamagedQuantity": damaged}], user)


def test_T21_MC05_full_receipt_updates_stock(conn, agent):
    """UC-09/UC-11: only AcceptedQuantity updates CurrentStock."""
    _, res = approve_product(conn, agent, 1)
    r = receive(conn, agent, res.poId, 80)
    assert r.orderStatus == "RECEIVED" and r.quantityAdded == 80 and not r.discrepancies
    assert stock(conn, 1) == 40 + 80


def test_T22_MC05_shortage_partial_and_escalated(conn, agent):
    """UC-10 + BPMN 4.5: discrepancy goes to exception review."""
    _, res = approve_product(conn, agent, 1)
    r = receive(conn, agent, res.poId, 60)
    assert r.orderStatus == "PARTIALLY_RECEIVED" and r.discrepancies[0]["status"] == "SHORTAGE"
    assert stock(conn, 1) == 100
    ex = one(conn, "SELECT * FROM AgentDecision WHERE AgentType='EXCEPTION_REVIEW' AND RelatedRecordType='RECEIPT'")
    assert ex["ReviewStatus"] == "ESCALATED" and "SHORTAGE" in ex["OutputSummary"]
    r2 = receive(conn, agent, res.poId, 20)  # second delivery completes the order
    assert r2.orderStatus == "RECEIVED" and stock(conn, 1) == 120


def test_T23_MC05_damaged_units_not_added(conn, agent):
    _, res = approve_product(conn, agent, 1)
    r = receive(conn, agent, res.poId, 80, damaged=8)
    assert r.discrepancies[0]["status"] == "DAMAGED" and r.quantityAdded == 72 and stock(conn, 1) == 112


def test_T24_MC05_overage_capped_by_receiving_policy(conn, agent):
    _, res = approve_product(conn, agent, 1)
    r = receive(conn, agent, res.poId, 90)
    assert r.discrepancies[0]["status"] == "OVERAGE" and r.quantityAdded == 80


def test_T25_MC05_invalid_line_rolls_back_everything(conn, agent):
    """MC-05 postcondition: if any line is invalid, nothing is changed."""
    _, res = approve_product(conn, agent, 1)
    with pytest.raises(BusinessRuleError, match="invalid"):
        receive(conn, agent, res.poId, 5, damaged=10)
    assert stock(conn, 1) == 40
    assert one(conn, "SELECT COUNT(*) FROM Receipt")[0] == 0
    assert one(conn, "SELECT Status FROM PurchaseOrder WHERE POID=?", res.poId)[0] == "SUBMITTED"


def test_T26_MC05_only_warehouse_employee_records_receipts(conn, agent):
    _, res = approve_product(conn, agent, 1)
    with pytest.raises(BusinessRuleError):
        receive(conn, agent, res.poId, 80, user=MANAGER)


# ---------------------------------------------------------------- Agentic AI boundary (Phase I 4.6, Section 12)
def test_T27_agent_only_drafts_orders(conn, agent):
    """Section 8.4 rule 9: the agent creates orders only with Status = DRAFT."""
    rid = agent.evaluateReplenishment(1)
    assert agent.preparePurchaseOrder(rid) is None  # no approval yet -> flagged, nothing created
    assert one(conn, "SELECT COUNT(*) FROM PurchaseOrder")[0] == 0
    with conn:
        conn.execute("INSERT INTO Approval (RecommendationID, ApproverUserID, Decision, DecisionDate) VALUES (?,?,?,?)",
                     (rid, MANAGER, "APPROVE", "2026-11-17"))
        conn.execute("UPDATE ReplenishmentRecommendation SET Status='APPROVED' WHERE RecommendationID=?", (rid,))
    po_id = agent.preparePurchaseOrder(rid)
    assert one(conn, "SELECT Status FROM PurchaseOrder WHERE POID=?", po_id)[0] == "DRAFT"
    assert one(conn, "SELECT COUNT(*) FROM SupplierOutbox")[0] == 0


def test_T28_agent_never_changes_current_stock(conn, agent):
    """Section 8.4 rule 11: AI outputs never update CurrentStock."""
    before = [r[0] for r in conn.execute("SELECT CurrentStock FROM Inventory ORDER BY ProductID")]
    agent.monitorAll()
    for pid in range(1, 6):
        agent.evaluateReplenishment(pid)
    agent.reviewOrderExceptions()
    after = [r[0] for r in conn.execute("SELECT CurrentStock FROM Inventory ORDER BY ProductID")]
    assert before == after


def test_T29_monitoring_flags_risk_without_purchasing(conn, agent):
    """UC-01 / BPMN 4.1: monitoring does not create a purchase request or order."""
    agent.monitorAll()
    rows = {r["RelatedRecordID"]: r["ReviewStatus"] for r in
            conn.execute("SELECT * FROM AgentDecision WHERE AgentType='INVENTORY_MONITORING'")}
    assert rows == {"1": "FLAGGED", "2": "FLAGGED", "3": "NO_ACTION", "4": "FLAGGED", "5": "FLAGGED"}
    assert one(conn, "SELECT COUNT(*) FROM ReplenishmentRecommendation")[0] == 0
    assert one(conn, "SELECT COUNT(*) FROM PurchaseOrder")[0] == 0


# ---------------------------------------------------------------- 6.4 Order Monitoring and Exception Review
def test_T30_delay_detected_escalated_without_changing_order(conn, agent):
    """UC-12: delays identified; agent does not change records or send messages."""
    _, res = approve_product(conn, agent, 1)
    later = date.today() + timedelta(days=10)
    ids = agent.reviewOrderExceptions(today=later)
    ex = one(conn, "SELECT * FROM AgentDecision WHERE DecisionID=?", ids[0])
    assert ex["ReviewStatus"] == "ESCALATED" and "DELAY" in ex["OutputSummary"] and "Draft message" in ex["OutputSummary"]
    assert one(conn, "SELECT Status FROM PurchaseOrder WHERE POID=?", res.poId)[0] == "SUBMITTED"
    assert one(conn, "SELECT COUNT(*) FROM SupplierOutbox")[0] == 1  # only the original PO message
    assert agent.reviewOrderExceptions(today=later) == []  # no duplicate escalation, no false "normal" summary
    assert one(conn, "SELECT COUNT(*) FROM AgentDecision WHERE AgentType='EXCEPTION_REVIEW'")[0] == 1


def test_T31_normal_status_summary(conn, agent):
    approve_product(conn, agent, 1)
    ids = agent.reviewOrderExceptions()
    ex = one(conn, "SELECT * FROM AgentDecision WHERE DecisionID=?", ids[0])
    assert ex["ReviewStatus"] == "NO_ACTION" and ex["OutputSummary"].startswith("Normal status")


def test_T32_manager_resolves_exception(conn, agent):
    _, res = approve_product(conn, agent, 1)
    did = agent.reviewOrderExceptions(today=date.today() + timedelta(days=10))[0]
    with pytest.raises(BusinessRuleError):
        mark_resolved(conn, did, WAREHOUSE)
    mark_resolved(conn, did, MANAGER, "Supplier promised delivery Friday")
    ex = one(conn, "SELECT * FROM AgentDecision WHERE DecisionID=?", did)
    assert ex["ReviewStatus"] == "RESOLVED" and "Friday" in ex["OutputSummary"]


# ---------------------------------------------------------------- Integration through the UI (Section 7.2)
def test_T33_end_to_end_through_screens():
    """All five BPMN processes driven through the four Section 7.2 screens."""
    app = create_app(":memory:")
    c = app.test_client()
    assert c.post("/monitor", follow_redirects=True).status_code == 200                    # 4.1
    page = c.post("/evaluate/1", follow_redirects=True).get_data(as_text=True)             # 4.2
    assert "AI-drafted · needs your approval" in page and "FreshFlow Distributors" in page
    page = c.post("/requests/1/decision", data={"decision": "APPROVE"}, follow_redirects=True).get_data(as_text=True)
    assert "submitted to supplier" in page                                                 # 4.3
    c.post("/switch-user", data={"user_id": WAREHOUSE})
    page = c.post("/receive?poid=1", data={"poid": 1, "rec_1": 60, "dmg_1": 0},
                  follow_redirects=True).get_data(as_text=True)
    assert "SHORTAGE" in page                                                               # 4.4
    c.post("/switch-user", data={"user_id": MANAGER})
    page = c.get("/orders").get_data(as_text=True)
    assert "ESCALATED".title() in page and "Mark resolved" in page                          # 4.5
    assert "Receipt discrepancies" in c.get("/").get_data(as_text=True)


# ---------------------------------------------------------------- Phase III refinement: lead time of the chosen supplier
def test_T34_quantity_uses_each_suppliers_own_lead_time(conn):
    """11.7 refinement: a faster supplier needs a smaller order (demand × its own lead time + SafetyStock − position)."""
    sc = SupplierComparisonService(conn)
    demand = {"averageDailyDemand": 12, "safetyStock": 60, "stockPosition": 40}
    ranked, _ = sc.rankSuppliers(sc.candidates(1), 128, demand)
    qty = {s["SupplierName"]: (s["LeadTime"], s["requiredQty"], s["orderQty"]) for s in ranked}
    assert qty["FreshFlow Distributors"] == (5, 80, 80)       # 12×5 + 60 − 40
    assert qty["ValueSource Wholesale"] == (9, 128, 128)      # 12×9 + 60 − 40


def test_T35_milk_request_sized_for_fast_supplier(conn, agent):
    """The case found during review: milk was sized for 9 days although the chosen supplier delivers in 2."""
    rid = agent.evaluateReplenishment(2)
    r = one(conn, "SELECT * FROM ReplenishmentRecommendation WHERE RecommendationID=?", rid)
    assert (r["Status"], r["SupplierID"], r["RecommendedQuantity"], r["EstimatedCost"]) == \
        ("PENDING_APPROVAL", 1, 42, 130.2)                    # 10×2 + 40 − 18 = 42
    assert "lead time of 2 days" in r["Reason"]


def test_T36_supplier_not_needed_when_stock_covers_its_lead_time(conn):
    sc = SupplierComparisonService(conn)
    demand = {"averageDailyDemand": 12, "safetyStock": 60, "stockPosition": 120}
    ranked, excluded = sc.rankSuppliers(sc.candidates(1), 48, demand)
    assert any(e["SupplierName"] == "FreshFlow Distributors" and e["reason"] == "No order needed with this lead time"
               for e in excluded)
