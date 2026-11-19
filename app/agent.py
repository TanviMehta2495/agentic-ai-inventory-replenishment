"""AIReplenishmentAgent – the Agentic AI service (Phase II Section 6, class diagram 8.5b).

One method per Section 6 prompt. The prompts are kept verbatim below so the implemented
behaviour can be checked against the design. In this partial implementation the agent's
reasoning is carried out by deterministic rules (the "supplied replenishment formula",
"purchasing criteria" and "severity rules" of Section 6), which makes every output
reproducible and testable. An LLM can be plugged in behind the same methods later;
the boundary rules below would still apply because they are enforced by the in-house
services and the database, not by the agent.

Boundary (Section 6 general instruction): the agent never approves purchases, submits
orders, sends supplier messages or updates CurrentStock. It only writes
ReplenishmentRecommendation rows, DRAFT purchase orders and AgentDecision audit records.
"""
from __future__ import annotations

from datetime import date, timedelta

from .db import now
from .services import (BusinessRuleError, ReplenishmentEvaluationService, SupplierComparisonService,
                       effective_lead_time, risk_level, write_audit)

GENERAL_INSTRUCTION = (
    "Use only authorized data and supplied business rules. Do not invent missing information. Explain "
    "recommendations and identify the supporting records. Escalate missing-data, uncertain, policy-related, "
    "and high-impact cases to the store manager. Do not approve purchases, submit orders, send supplier "
    "messages, or update transactional inventory. Return an audit summary with the process name, related "
    "record IDs, output, timestamp, and review status.")

PROMPTS = {
    "6.1 Inventory Monitoring": "Analyze current stock and sales history for each product. Compare inventory with "
        "reorder levels and safety stock, and identify unusual demand or potential stockout risks. ...",
    "6.2 Replenishment Evaluation": "Evaluate whether replenishment is needed using current stock, demand, safety "
        "stock, supplier lead time, and outstanding orders. ...",
    "6.3 Purchase Order Preparation": "Verify that the approval applies to the purchase request. Prepare "
        "purchase-order information ... Do not submit the order.",
    "6.4 Order Monitoring and Exception Review": "Review purchase orders, supplier responses, expected delivery "
        "dates, inventory conditions, and warehouse-verified receipt records. ...",
}

AGENT_ID = "AIReplenishmentAgent v1 (rules)"


class AIReplenishmentAgent:
    def __init__(self, conn):
        self.conn = conn
        self.evaluation = ReplenishmentEvaluationService(conn)
        self.suppliers = SupplierComparisonService(conn)

    # ------------------------------------------------------------------ 6.1
    def assessInventory(self, product_id: int) -> int:
        c = self.conn
        inv = c.execute("SELECT i.*, p.SKU, p.Name FROM Inventory i JOIN Product p USING (ProductID)"
                        " WHERE ProductID=?", (product_id,)).fetchone()
        with c:
            if inv is None:
                return write_audit(c, "INVENTORY_MONITORING", "PRODUCT", product_id,
                                   "Data issue: no inventory record for this product.", "FLAGGED")
            avg7 = self.evaluation.calculateAverageDemand(product_id, 7)
            avg30 = self.evaluation.calculateAverageDemand(product_id, 30)
            risk = risk_level(inv["CurrentStock"], inv["ReorderLevel"], inv["SafetyStock"])
            days_cover = inv["CurrentStock"] / avg30 if avg30 else float("inf")
            notes = []
            if avg30 and avg7 > 1.5 * avg30:
                notes.append(f"unusual demand: last 7 days {avg7:.1f}/day vs 30-day {avg30:.1f}/day")
            summary = (f"{risk} risk – CurrentStock {inv['CurrentStock']}, ReorderLevel {inv['ReorderLevel']}, "
                       f"SafetyStock {inv['SafetyStock']}; avg demand {avg30:.1f}/day "
                       f"(≈{days_cover:.1f} days of cover)." + (" " + "; ".join(notes) + "." if notes else ""))
            status = "FLAGGED" if risk in ("CRITICAL", "HIGH", "MEDIUM") or notes else "NO_ACTION"
            return write_audit(c, "INVENTORY_MONITORING", "PRODUCT", product_id, summary, status)

    def monitorAll(self) -> list[int]:
        ids = [r[0] for r in self.conn.execute("SELECT ProductID FROM Product WHERE IsActive=1")]
        return [self.assessInventory(i) for i in ids]

    # ------------------------------------------------------------------ 6.2
    def evaluateReplenishment(self, product_id: int) -> int:
        """Creates one ReplenishmentRecommendation (UC-02 → UC-03 → UC-04) and returns its ID."""
        c = self.conn
        calc = self.evaluation.calculateReorderQuantity(product_id)        # MC-02
        inv = calc.inventory or {}
        risk = risk_level(inv.get("CurrentStock", 0), inv.get("ReorderLevel", 0), inv.get("SafetyStock", 0)) \
            if inv else None
        supplier_id = qty = cost = None
        if calc.flagged:
            status, reason = "FLAGGED_FOR_REVIEW", "Escalated: " + calc.reason
        elif calc.requiredQty == 0:
            status, reason = "NO_REPLENISHMENT", calc.reason
        else:
            demand = {"averageDailyDemand": calc.averageDailyDemand, "safetyStock": inv["SafetyStock"],
                      "stockPosition": calc.stockPosition}
            ranked, excluded = self.suppliers.rankSuppliers(self.suppliers.candidates(product_id),
                                                            calc.requiredQty, demand)  # MC-03
            excl = "; ".join(f"{e['SupplierName']}: {e['reason']}" for e in excluded) or "none"
            base = (f"Average daily demand {calc.averageDailyDemand:.2f} over 30 days; SafetyStock "
                    f"{inv['SafetyStock']}; stock position {calc.stockPosition} (CurrentStock {inv['CurrentStock']} "
                    f"+ open orders {calc.openOrderQty}) is at or below ReorderLevel {inv['ReorderLevel']}.")
            not_needed = [e for e in excluded if e["reason"] == "No order needed with this lead time"]
            if not ranked and not_needed:
                status = "NO_REPLENISHMENT"
                reason = (base + " Stock on hand and on order already covers demand until "
                          f"{not_needed[0]['SupplierName']} ({not_needed[0]['LeadTime']} days) could deliver. "
                          f"Excluded – {excl}.")
            elif not ranked:
                status = "FLAGGED_FOR_REVIEW"
                reason = base + f" Escalated: no feasible approved supplier. Excluded – {excl}."
            else:
                best = ranked[0]
                supplier_id, qty, cost = best["SupplierID"], best["orderQty"], best["estimatedCost"]
                status = "PENDING_APPROVAL"
                target = calc.averageDailyDemand * best["LeadTime"] + inv["SafetyStock"]
                others = "; ".join(f"{s['SupplierName']} score {s['score']:.2f} ({s['orderQty']} units, "
                                   f"${s['estimatedCost']:.2f}, {s['LeadTime']} days)" for s in ranked[1:]) or "none"
                moq = (f" Quantity raised from {best['requiredQty']} to MinimumOrderQuantity {qty}."
                       if qty > best["requiredQty"] else "")
                reason = (base + f" Using the proposed supplier's lead time of {best['LeadTime']} days: target level "
                          f"= {calc.averageDailyDemand:.2f} × {best['LeadTime']} + {inv['SafetyStock']} = {target:.0f}; "
                          f"required quantity = {target:.0f} − {calc.stockPosition} = {best['requiredQty']}." + moq
                          + f" Proposed supplier {best['SupplierName']} (score {best['score']:.2f}: UnitPrice "
                          f"${best['UnitPrice']:.2f}, lead time {best['LeadTime']} days, rating "
                          f"{best['PerformanceRating']}, {best['Availability']}). Other options: {others}. "
                          f"Excluded: {excl}.")
        with c:
            rid = c.execute(
                "INSERT INTO ReplenishmentRecommendation (ProductID, SupplierID, RecommendedQuantity, EstimatedCost,"
                " Reason, RiskLevel, Status, CreatedAt, CreatedByAgent) VALUES (?,?,?,?,?,?,?,?,?)",
                (product_id, supplier_id, qty, cost, reason, risk, status, now(), AGENT_ID)).lastrowid
            write_audit(c, "REPLENISHMENT_EVALUATION", "RECOMMENDATION", rid, f"{status}: {reason}",
                        "FLAGGED" if status == "FLAGGED_FOR_REVIEW" else "NO_ACTION")
        return rid

    # ------------------------------------------------------------------ 6.3
    def preparePurchaseOrder(self, recommendation_id: int):
        """Returns the POID of a DRAFT order, or None when information is missing (flagged)."""
        c = self.conn
        req = c.execute("SELECT * FROM ReplenishmentRecommendation WHERE RecommendationID=?",
                        (recommendation_id,)).fetchone()
        appr = c.execute("SELECT * FROM Approval WHERE RecommendationID=?", (recommendation_id,)).fetchone()
        terms = None
        if req is not None and req["SupplierID"] is not None:
            terms = c.execute("SELECT ps.*, ps.LeadTime AS PSLeadTime, s.LeadTime AS SupplierLeadTime"
                              " FROM ProductSupplier ps JOIN Supplier s USING (SupplierID)"
                              " WHERE ps.ProductID=? AND ps.SupplierID=?",
                              (req["ProductID"], req["SupplierID"])).fetchone()
        missing = []
        if req is None:
            missing.append("purchase request not found")
        elif appr is None or appr["Decision"] != "APPROVE":
            missing.append("no approval for this request")
        if req is not None and (req["RecommendedQuantity"] is None or terms is None):
            missing.append("approved quantity or supplier terms missing")
        with c:
            if missing:
                write_audit(c, "PO_PREPARATION", "RECOMMENDATION", recommendation_id,
                            "Draft not prepared: " + "; ".join(missing), "FLAGGED")
                return None
            qty, price = req["RecommendedQuantity"], float(terms["UnitPrice"])
            line = round(qty * price, 2)
            order_date = date.today()
            expected = order_date + timedelta(days=effective_lead_time(terms))
            po_id = c.execute(
                "INSERT INTO PurchaseOrder (SupplierID, RecommendationID, OrderDate, ExpectedDeliveryDate, Status,"
                " TotalAmount, CreatedAt) VALUES (?,?,?,?, 'DRAFT', ?, ?)",
                (req["SupplierID"], recommendation_id, order_date.isoformat(), expected.isoformat(), line, now())
            ).lastrowid
            c.execute("INSERT INTO PurchaseOrderItem (POID, ProductID, Quantity, UnitPrice, LineAmount)"
                      " VALUES (?,?,?,?,?)", (po_id, req["ProductID"], qty, price, line))
            write_audit(c, "PO_PREPARATION", "PURCHASE_ORDER", po_id,
                        f"Draft PO-{po_id} for approval {appr['ApprovalID']}: {qty} × ${price:.2f} = ${line:.2f}, "
                        f"expected {expected.isoformat()}. Returned for validation; not submitted.", "NO_ACTION")
        return po_id

    # ------------------------------------------------------------------ 6.4
    def reviewOrderExceptions(self, today: date | None = None) -> list[int]:
        """Scheduled order review: identifies delayed orders. Writes audit records only."""
        c = self.conn
        today = today or date.today()
        out, overdue = [], 0
        orders = c.execute("SELECT po.*, s.Name AS SupplierName FROM PurchaseOrder po JOIN Supplier s USING"
                           " (SupplierID) WHERE po.Status IN ('SUBMITTED','CONFIRMED','DELAYED','PARTIALLY_RECEIVED')"
                           ).fetchall()
        with c:
            for po in orders:
                if po["ExpectedDeliveryDate"] and po["ExpectedDeliveryDate"] < today.isoformat():
                    overdue += 1
                    if self._open_exception_exists("PURCHASE_ORDER", po["POID"]):
                        continue
                    item = c.execute("SELECT poi.ProductID, i.CurrentStock, i.SafetyStock FROM PurchaseOrderItem poi"
                                     " JOIN Inventory i USING (ProductID) WHERE POID=?", (po["POID"],)).fetchone()
                    days = (today - date.fromisoformat(po["ExpectedDeliveryDate"])).days
                    priority = "HIGH" if item and item["CurrentStock"] <= item["SafetyStock"] else "MEDIUM"
                    out.append(write_audit(
                        c, "EXCEPTION_REVIEW", "PURCHASE_ORDER", po["POID"],
                        f"[{priority}] DELAY – PO-{po['POID']} from {po['SupplierName']} is {days} day(s) past "
                        f"ExpectedDeliveryDate {po['ExpectedDeliveryDate']} with no full receipt. Impact: CurrentStock "
                        f"{item['CurrentStock']} vs SafetyStock {item['SafetyStock']}. Recommended action: contact "
                        f"supplier for a new delivery date. Draft message: {self.draftSupplierMessage(po['POID'])}",
                        "ESCALATED"))
            if orders and not overdue:  # normal-status summary only when nothing is overdue
                out.append(write_audit(c, "EXCEPTION_REVIEW", "PURCHASE_ORDER", "ALL",
                                       f"Normal status: {len(orders)} open order(s), none overdue.", "NO_ACTION"))
        return out

    def reviewReceiptDiscrepancy(self, receipt_id: int, discrepancies: list) -> int:
        c = self.conn
        parts = []
        high = False
        for d in discrepancies:
            inv = c.execute("SELECT CurrentStock, SafetyStock FROM Inventory WHERE ProductID=?", (d["ProductID"],)).fetchone()
            short = inv["CurrentStock"] <= inv["SafetyStock"]
            high |= short and d["status"] in ("SHORTAGE", "DAMAGED")
            parts.append(f"{d['status']} on product {d['ProductID']}: received {d['received']}, damaged {d['damaged']}, "
                         f"outstanding {d['outstanding']} (CurrentStock {inv['CurrentStock']}, SafetyStock {inv['SafetyStock']})")
        priority = "HIGH" if high else "MEDIUM"
        action = ("request replacement / remaining units from supplier" if any(d["status"] in ("SHORTAGE", "DAMAGED")
                  for d in discrepancies) else "confirm with supplier whether extra units should be returned")
        with c:
            return write_audit(c, "EXCEPTION_REVIEW", "RECEIPT", receipt_id,
                               f"[{priority}] Receipt {receipt_id}: " + "; ".join(parts) + f". Recommended action: {action}.",
                               "ESCALATED")

    def draftSupplierMessage(self, po_id: int) -> str:
        po = self.conn.execute("SELECT po.*, s.ContactName FROM PurchaseOrder po JOIN Supplier s USING (SupplierID)"
                               " WHERE POID=?", (po_id,)).fetchone()
        return (f"Hello {po['ContactName'] or 'team'}, our order PO-{po_id} was expected on "
                f"{po['ExpectedDeliveryDate']} and has not arrived in full. Please confirm a new delivery date.")

    def _open_exception_exists(self, rtype: str, rid) -> bool:
        return self.conn.execute("SELECT 1 FROM AgentDecision WHERE AgentType='EXCEPTION_REVIEW' AND RelatedRecordType=?"
                                 " AND RelatedRecordID=? AND ReviewStatus='ESCALATED'", (rtype, str(rid))).fetchone() is not None


def mark_resolved(conn, decision_id: int, user_id: int, note: str = "") -> None:
    """AgentDecision.markResolved() – the store manager's decision, not the agent's."""
    from .services import has_permission
    if not has_permission(conn, user_id, "resolve_exception"):
        raise BusinessRuleError("Only the store manager can resolve exceptions")
    with conn:
        row = conn.execute("SELECT OutputSummary FROM AgentDecision WHERE DecisionID=?", (decision_id,)).fetchone()
        conn.execute("UPDATE AgentDecision SET ReviewStatus='RESOLVED', OutputSummary=? WHERE DecisionID=?",
                     (row["OutputSummary"] + (f" | Manager decision: {note}" if note else " | Resolved by manager."),
                      decision_id))
