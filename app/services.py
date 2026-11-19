"""In-house control classes (Phase II Section 8.5b) and the five method contracts (Section 9).

Method and class names follow the complete class diagram so each piece of code can be
traced back to the design:  MC-01 ApprovalController.recordDecision
                            MC-02 ReplenishmentEvaluationService.calculateReorderQuantity
                            MC-03 SupplierComparisonService.rankSuppliers
                            MC-04 PurchaseOrderService.validatePurchaseOrder
                            MC-05 ReceivingService.updateInventoryFromAcceptedReceipt
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from . import rules
from .db import now


class BusinessRuleError(Exception):
    """Raised when a precondition of a method contract is not met."""


OPEN_PO_STATUSES = ("SUBMITTED", "CONFIRMED", "DELAYED", "PARTIALLY_RECEIVED")


def _money(x: float) -> float:
    return round(float(x) + 1e-9, 2)


def risk_level(stock: int, reorder_level: int, safety_stock: int) -> str:
    if stock <= 0:
        return "CRITICAL"
    if stock <= safety_stock:
        return "HIGH"
    if stock <= reorder_level:
        return "MEDIUM"
    return "LOW"


def has_permission(conn, user_id: int, action: str) -> bool:
    """User.hasPermission(action)"""
    u = conn.execute("SELECT Role, IsActive FROM User WHERE UserID=?", (user_id,)).fetchone()
    if not u or not u["IsActive"]:
        return False
    allowed = {"approve": {"STORE_MANAGER"}, "receive": {"WAREHOUSE_EMPLOYEE"},
               "resolve_exception": {"STORE_MANAGER"}, "evaluate": {"STORE_MANAGER"}}
    return u["Role"] in allowed.get(action, set())


def effective_lead_time(ps_row) -> int:
    """ProductSupplier.getEffectiveLeadTime(): product lead time overrides Supplier.LeadTime."""
    return ps_row["PSLeadTime"] if ps_row["PSLeadTime"] is not None else ps_row["SupplierLeadTime"]


def write_audit(conn, agent_type, record_type, record_id, summary, review_status, confidence=None) -> int:
    """AgentDecision.recordDecision() – Section 8.4 rule 12: every AI output is audited."""
    cur = conn.execute(
        "INSERT INTO AgentDecision (AgentType, RelatedRecordType, RelatedRecordID, OutputSummary,"
        " Timestamp, ReviewStatus, ConfidenceScore) VALUES (?,?,?,?,?,?,?)",
        (agent_type, record_type, str(record_id), summary, now(), review_status, confidence))
    return cur.lastrowid


# --------------------------------------------------------------------------------------------
# SupplierComparisonService  (UC-04)
# --------------------------------------------------------------------------------------------
class SupplierComparisonService:
    def __init__(self, conn):
        self.conn = conn

    def candidates(self, product_id: int):
        return self.conn.execute(
            "SELECT ps.*, ps.LeadTime AS PSLeadTime, s.LeadTime AS SupplierLeadTime, s.Name AS SupplierName,"
            " s.PerformanceRating, s.IsApproved FROM ProductSupplier ps JOIN Supplier s USING (SupplierID)"
            " WHERE ps.ProductID=? ORDER BY ps.SupplierID", (product_id,)).fetchall()

    def findEligibleSuppliers(self, product_id: int):
        return [c for c in self.candidates(product_id)
                if c["IsApproved"] and c["Availability"] != "UNAVAILABLE"]

    # ---------------- MC-03 ----------------
    def rankSuppliers(self, candidates, required_qty: int, demand: dict | None = None):
        """demand = {averageDailyDemand, safetyStock, stockPosition}. When given, each supplier's quantity is
        sized with that supplier's OWN lead time (refinement found during implementation; the Phase II
        design used the longest eligible lead time – see Phase III Section 11.7.1):
        a fast supplier needs a smaller order than a slow one."""
        if required_qty <= 0:
            raise BusinessRuleError("requiredQty must be greater than 0")
        if len({c["ProductID"] for c in candidates}) > 1:
            raise BusinessRuleError("All candidates must be for the same product")
        w = rules.SCORING_WEIGHTS
        scored, excluded = [], []
        for c in candidates:
            base = {"SupplierID": c["SupplierID"], "SupplierName": c["SupplierName"],
                    "UnitPrice": float(c["UnitPrice"]), "MinimumOrderQuantity": c["MinimumOrderQuantity"],
                    "Availability": c["Availability"], "PerformanceRating": c["PerformanceRating"],
                    "LeadTime": effective_lead_time(c)}
            if not c["IsApproved"]:
                excluded.append({**base, "reason": "Supplier not approved"}); continue
            if c["Availability"] == "UNAVAILABLE":
                excluded.append({**base, "reason": "Product currently unavailable"}); continue
            need = required_qty
            if demand is not None:
                need = math.ceil(demand["averageDailyDemand"] * base["LeadTime"] + demand["safetyStock"]
                                 - demand["stockPosition"])
                if need <= 0:
                    excluded.append({**base, "requiredQty": need,
                                     "reason": "No order needed with this lead time"}); continue
            base["requiredQty"] = need
            order_qty = max(need, c["MinimumOrderQuantity"])
            if order_qty > need * (1 + rules.ALLOWED_OVER_ORDER_PCT):
                excluded.append({**base, "reason": "Minimum order quantity too high"}); continue
            scored.append({**base, "orderQty": order_qty,
                           "estimatedCost": _money(order_qty * float(c["UnitPrice"]))})
        if not scored:
            return [], excluded
        lowest_price = min(s["UnitPrice"] for s in scored)
        shortest_lead = min(max(s["LeadTime"], 1) for s in scored)
        for s in scored:
            price_score = lowest_price / s["UnitPrice"] if s["UnitPrice"] else 1.0
            lead_score = shortest_lead / max(s["LeadTime"], 1)
            rating_score = (s["PerformanceRating"] or 0) / 5
            avail_score = 1.0 if s["Availability"] == "AVAILABLE" else 0.5
            s["score"] = round(w["price"] * price_score + w["lead"] * lead_score
                               + w["rating"] * rating_score + w["availability"] * avail_score, 4)
        scored.sort(key=lambda s: (-s["score"], s["estimatedCost"], s["LeadTime"]))
        return scored, excluded


# --------------------------------------------------------------------------------------------
# ReplenishmentEvaluationService  (UC-02, UC-03)
# --------------------------------------------------------------------------------------------
@dataclass
class ReorderCalculation:
    requiredQty: int
    reason: str
    flagged: bool = False
    averageDailyDemand: float = 0.0
    leadTime: int = 0
    stockPosition: int = 0
    openOrderQty: int = 0
    targetLevel: float = 0.0
    inventory: dict = field(default_factory=dict)


class ReplenishmentEvaluationService:
    def __init__(self, conn):
        self.conn = conn
        self.suppliers = SupplierComparisonService(conn)

    def calculateAverageDemand(self, product_id: int, days: int) -> float:
        since = (datetime.now() - timedelta(days=days)).isoformat(timespec="seconds")
        total = self.conn.execute("SELECT COALESCE(SUM(Quantity),0) FROM SalesHistory WHERE ProductID=? AND SaleDate>=?",
                                  (product_id, since)).fetchone()[0]
        return total / days

    def openOrderQuantity(self, product_id: int, exclude_recommendation: int | None = None) -> int:
        rows = self.conn.execute(
            f"SELECT poi.POItemID, poi.Quantity FROM PurchaseOrderItem poi JOIN PurchaseOrder po USING (POID)"
            f" WHERE poi.ProductID=? AND po.Status IN ({','.join('?' * len(OPEN_PO_STATUSES))})"
            f" AND po.RecommendationID IS NOT ?",
            (product_id, *OPEN_PO_STATUSES, exclude_recommendation)).fetchall()
        total = 0
        for r in rows:
            accepted = self.conn.execute("SELECT COALESCE(SUM(AcceptedQuantity),0) FROM ReceiptItem WHERE POItemID=?",
                                         (r["POItemID"],)).fetchone()[0]
            total += max(0, r["Quantity"] - accepted)
        return total

    # ---------------- MC-02 ----------------
    def calculateReorderQuantity(self, product_id: int) -> ReorderCalculation:
        product = self.conn.execute("SELECT * FROM Product WHERE ProductID=?", (product_id,)).fetchone()
        if not product or not product["IsActive"]:
            raise BusinessRuleError("Product does not exist or is inactive")
        inv = self.conn.execute("SELECT * FROM Inventory WHERE ProductID=?", (product_id,)).fetchone()
        if inv is None or any(inv[k] is None for k in ("CurrentStock", "ReorderLevel", "SafetyStock")):
            return ReorderCalculation(0, "Missing inventory data", flagged=True)
        inv = dict(inv)
        avg = self.calculateAverageDemand(product_id, rules.DEMAND_WINDOW_DAYS)
        open_qty = self.openOrderQuantity(product_id)
        position = inv["CurrentStock"] + open_qty          # Inventory.calculateStockPosition
        if position > inv["ReorderLevel"]:
            return ReorderCalculation(0, f"Stock position {position} (CurrentStock {inv['CurrentStock']} + open orders "
                                         f"{open_qty}) is above ReorderLevel {inv['ReorderLevel']}.",
                                      averageDailyDemand=avg, stockPosition=position, openOrderQty=open_qty, inventory=inv)
        eligible = self.suppliers.findEligibleSuppliers(product_id)
        if not eligible:
            return ReorderCalculation(0, "No feasible approved supplier.", flagged=True, averageDailyDemand=avg,
                                      stockPosition=position, openOrderQty=open_qty, inventory=inv)
        lead = max(effective_lead_time(e) for e in eligible)   # conservative
        target = avg * lead + inv["SafetyStock"]
        required = max(0, math.ceil(target - position))
        reason = (f"Average daily demand {avg:.2f} over {rules.DEMAND_WINDOW_DAYS} days × lead time {lead} days "
                  f"+ SafetyStock {inv['SafetyStock']} = target level {target:.0f}. Stock position {position} "
                  f"(CurrentStock {inv['CurrentStock']} + open orders {open_qty}). Required quantity = "
                  f"{target:.0f} − {position} = {required}.")
        return ReorderCalculation(required, reason, averageDailyDemand=avg, leadTime=lead,
                                  stockPosition=position, openOrderQty=open_qty, targetLevel=target, inventory=inv)


# --------------------------------------------------------------------------------------------
# PurchaseOrderService  (UC-06, UC-07, UC-08)
# --------------------------------------------------------------------------------------------
class PurchaseOrderService:
    def __init__(self, conn, agent):
        self.conn = conn
        self.agent = agent

    def requestDraft(self, recommendation_id: int):
        return self.agent.preparePurchaseOrder(recommendation_id)

    # ---------------- MC-04 ----------------
    def validatePurchaseOrder(self, po_id: int):
        c = self.conn
        problems: list[str] = []
        po = c.execute("SELECT * FROM PurchaseOrder WHERE POID=?", (po_id,)).fetchone()
        if po is None:
            raise BusinessRuleError("Purchase order does not exist")
        items = c.execute("SELECT * FROM PurchaseOrderItem WHERE POID=?", (po_id,)).fetchall()
        req = c.execute("SELECT * FROM ReplenishmentRecommendation WHERE RecommendationID=?",
                        (po["RecommendationID"],)).fetchone()
        appr = c.execute("SELECT * FROM Approval WHERE RecommendationID=?", (po["RecommendationID"],)).fetchone()
        if po["Status"] != "DRAFT":
            problems.append("Order is not a draft")
        if appr is None or appr["Decision"] != "APPROVE" or req["Status"] != "APPROVED":
            problems.append("No valid approval for this order")
        if po["SupplierID"] != req["SupplierID"]:
            problems.append("Supplier differs from approved request")
        sup = c.execute("SELECT IsApproved FROM Supplier WHERE SupplierID=?", (po["SupplierID"],)).fetchone()
        if not sup or not sup["IsApproved"]:
            problems.append("Supplier is no longer approved")
        if not items:
            problems.append("Order has no items")
        for it in items:
            if it["ProductID"] != req["ProductID"]:
                problems.append("Product differs from approved request")
            if it["Quantity"] != req["RecommendedQuantity"]:
                problems.append("Quantity differs from approved quantity")
            terms = c.execute("SELECT * FROM ProductSupplier WHERE ProductID=? AND SupplierID=?",
                              (it["ProductID"], po["SupplierID"])).fetchone()
            if terms is None:
                problems.append("No purchasing terms for this supplier and product")
            else:
                if _money(it["UnitPrice"]) != _money(terms["UnitPrice"]):
                    problems.append("Unit price differs from supplier terms")
                if it["Quantity"] < terms["MinimumOrderQuantity"]:
                    problems.append("Below minimum order quantity")
            if _money(it["LineAmount"]) != _money(it["Quantity"] * it["UnitPrice"]):
                problems.append("Line amount is wrong")
            # MC-04 note: a price changed after approval is a difference from the approved request
            if req["EstimatedCost"] is not None and _money(it["LineAmount"]) != _money(req["EstimatedCost"]):
                problems.append("Cost differs from the approved estimated cost")
        if _money(po["TotalAmount"]) != _money(sum(i["LineAmount"] for i in items)):
            problems.append("Order total is wrong")
        if not po["ExpectedDeliveryDate"] or po["ExpectedDeliveryDate"] < po["OrderDate"]:
            problems.append("Invalid expected delivery date")
        return (len(problems) == 0), problems

    def submitToSupplier(self, po_id: int) -> None:
        """In-house system only (Section 8.4 rule 9). Sends the order through the supplier message flow."""
        po = self.conn.execute("SELECT po.*, s.Email FROM PurchaseOrder po JOIN Supplier s USING (SupplierID)"
                               " WHERE POID=?", (po_id,)).fetchone()
        with self.conn:
            self.conn.execute("UPDATE PurchaseOrder SET Status='SUBMITTED' WHERE POID=?", (po_id,))
            self.conn.execute("INSERT INTO SupplierOutbox (POID, ToEmail, Body, SentAt) VALUES (?,?,?,?)",
                              (po_id, po["Email"], f"Purchase order PO-{po_id}: total ${po['TotalAmount']:.2f}, "
                                                   f"expected delivery {po['ExpectedDeliveryDate']}.", now()))

    def hold(self, po_id: int, problems: list[str]) -> None:
        with self.conn:
            self.conn.execute("UPDATE PurchaseOrder SET Status='ON_HOLD', HoldReason=? WHERE POID=?",
                              ("; ".join(problems), po_id))


# --------------------------------------------------------------------------------------------
# ApprovalController  (UC-05, sequence diagram Figure 7.1)
# --------------------------------------------------------------------------------------------
@dataclass
class DecisionResult:
    approvalId: int
    requestStatus: str
    poId: int | None
    orderStatus: str | None
    message: str


class ApprovalController:
    def __init__(self, conn, agent):
        self.conn = conn
        self.po_service = PurchaseOrderService(conn, agent)

    def loadRequest(self, recommendation_id: int):
        req = self.conn.execute(
            "SELECT r.*, p.SKU, p.Name AS ProductName, s.Name AS SupplierName FROM ReplenishmentRecommendation r"
            " JOIN Product p USING (ProductID) LEFT JOIN Supplier s ON s.SupplierID = r.SupplierID"
            " WHERE RecommendationID=?", (recommendation_id,)).fetchone()
        if req is None:
            raise BusinessRuleError("Request not found")
        sc = SupplierComparisonService(self.conn)
        ranked, excluded = ([], [])
        if req["RecommendedQuantity"]:
            # re-run the comparison as it was at evaluation time (this request's own order not yet counted)
            ev = ReplenishmentEvaluationService(self.conn)
            inv = self.conn.execute("SELECT * FROM Inventory WHERE ProductID=?", (req["ProductID"],)).fetchone()
            demand = {"averageDailyDemand": ev.calculateAverageDemand(req["ProductID"], rules.DEMAND_WINDOW_DAYS),
                      "safetyStock": inv["SafetyStock"],
                      "stockPosition": inv["CurrentStock"] + ev.openOrderQuantity(req["ProductID"], recommendation_id)}
            ranked, excluded = sc.rankSuppliers(sc.candidates(req["ProductID"]), req["RecommendedQuantity"], demand)
        return req, ranked, excluded

    # ---------------- MC-01 ----------------
    def recordDecision(self, recommendation_id: int, decision: str, comments: str, user_id: int) -> DecisionResult:
        c = self.conn
        req = c.execute("SELECT * FROM ReplenishmentRecommendation WHERE RecommendationID=?",
                        (recommendation_id,)).fetchone()
        if req is None or req["Status"] != "PENDING_APPROVAL":
            raise BusinessRuleError("This request is not waiting for a decision")
        if not has_permission(c, user_id, "approve"):
            raise BusinessRuleError("Only the store manager can record this decision")
        if decision not in ("APPROVE", "REJECT", "REVISE"):
            raise BusinessRuleError("Unknown decision")
        if decision in ("REJECT", "REVISE") and not (comments or "").strip():
            raise BusinessRuleError("Please enter a reason for this decision")
        new_status = {"APPROVE": "APPROVED", "REJECT": "REJECTED", "REVISE": "REVISION_REQUESTED"}[decision]
        ts = now()
        with c:  # BEGIN TRANSACTION ... COMMIT
            cur = c.execute("INSERT INTO Approval (RecommendationID, ApproverUserID, Decision, DecisionDate, Comments)"
                            " VALUES (?,?,?,?,?)", (recommendation_id, user_id, decision, ts, comments or None))
            approval_id = cur.lastrowid
            c.execute("UPDATE ReplenishmentRecommendation SET Status=?, ReviewedAt=? WHERE RecommendationID=?",
                      (new_status, ts, recommendation_id))
        po_id, order_status = None, None
        if decision == "APPROVE":
            po_id = self.po_service.requestDraft(recommendation_id)
            if po_id is None:
                message = "Approved – purchase order draft flagged for review"
            else:
                ok, problems = self.po_service.validatePurchaseOrder(po_id)
                if ok:
                    self.po_service.submitToSupplier(po_id)
                    order_status, message = "SUBMITTED", "Approved – purchase order submitted to supplier"
                else:
                    self.po_service.hold(po_id, problems)
                    order_status = "ON_HOLD"
                    message = "Approved – purchase order on hold: " + "; ".join(problems)
        else:
            message = "Decision recorded"
        return DecisionResult(approval_id, new_status, po_id, order_status, message)


# --------------------------------------------------------------------------------------------
# ReceivingService  (UC-09, UC-10, UC-11)
# --------------------------------------------------------------------------------------------
@dataclass
class ReceiptResult:
    receiptId: int
    linesProcessed: int
    quantityAdded: int
    orderStatus: str
    discrepancies: list


class ReceivingService:
    def __init__(self, conn, agent):
        self.conn = conn
        self.agent = agent

    def _previously_accepted(self, po_item_id: int, exclude_receipt: int | None = None) -> int:
        q = "SELECT COALESCE(SUM(AcceptedQuantity),0) FROM ReceiptItem WHERE POItemID=?"
        args: list = [po_item_id]
        if exclude_receipt is not None:
            q += " AND ReceiptID<>?"; args.append(exclude_receipt)
        return self.conn.execute(q, args).fetchone()[0]

    def recordReceipt(self, po_id: int, lines: list[dict], user_id: int, notes: str = "") -> ReceiptResult:
        """lines: [{POItemID, ReceivedQuantity, DamagedQuantity}] as entered on the Receive Shipment screen."""
        c = self.conn
        if not has_permission(c, user_id, "receive"):
            raise BusinessRuleError("Only a warehouse employee can record a receipt")
        po = c.execute("SELECT * FROM PurchaseOrder WHERE POID=?", (po_id,)).fetchone()
        if po is None or po["Status"] not in OPEN_PO_STATUSES:
            raise BusinessRuleError("Purchase order is not open for receiving")
        if not lines:
            raise BusinessRuleError("Receipt not found or empty")
        with c:
            rid = c.execute("INSERT INTO Receipt (POID, ReceivedDate, ReceivedByUserID, Notes) VALUES (?,?,?,?)",
                            (po_id, now(), user_id, notes or None)).lastrowid
            for ln in lines:
                item = c.execute("SELECT * FROM PurchaseOrderItem WHERE POItemID=? AND POID=?",
                                 (ln["POItemID"], po_id)).fetchone()
                if item is None:
                    raise BusinessRuleError("Line does not belong to this purchase order")
                rec, dmg = int(ln["ReceivedQuantity"]), int(ln.get("DamagedQuantity", 0) or 0)
                outstanding = item["Quantity"] - self._previously_accepted(item["POItemID"])
                accepted = rules.receiving_policy_accepted(rec, dmg, outstanding) if rec >= 0 and dmg >= 0 else 0
                c.execute("INSERT INTO ReceiptItem (ReceiptID, POItemID, ReceivedQuantity, AcceptedQuantity,"
                          " DamagedQuantity) VALUES (?,?,?,?,?)", (rid, item["POItemID"], rec, accepted, dmg))
        try:
            return self.updateInventoryFromAcceptedReceipt(rid)
        except BusinessRuleError:
            with c:  # invalid receipt: nothing is kept (MC-05 postcondition)
                c.execute("DELETE FROM ReceiptItem WHERE ReceiptID=?", (rid,))
                c.execute("DELETE FROM Receipt WHERE ReceiptID=?", (rid,))
            raise

    # ---------------- MC-05 ----------------
    def updateInventoryFromAcceptedReceipt(self, receipt_id: int) -> ReceiptResult:
        c = self.conn
        receipt = c.execute("SELECT * FROM Receipt WHERE ReceiptID=?", (receipt_id,)).fetchone()
        lines = c.execute("SELECT * FROM ReceiptItem WHERE ReceiptID=?", (receipt_id,)).fetchall()
        if receipt is None or not lines:
            raise BusinessRuleError("Receipt not found or empty")
        discrepancies, added = [], 0
        with c:  # BEGIN TRANSACTION ... COMMIT (any exception rolls back every line)
            for ln in lines:
                if (ln["ReceivedQuantity"] < 0 or ln["AcceptedQuantity"] < 0 or ln["DamagedQuantity"] < 0
                        or ln["AcceptedQuantity"] + ln["DamagedQuantity"] > ln["ReceivedQuantity"]):
                    raise BusinessRuleError(f"Line quantities are invalid (receipt line {ln['ReceiptItemID']})")
                po_item = c.execute("SELECT * FROM PurchaseOrderItem WHERE POItemID=?", (ln["POItemID"],)).fetchone()
                outstanding = po_item["Quantity"] - self._previously_accepted(po_item["POItemID"], receipt_id)
                if ln["DamagedQuantity"] > 0:
                    status = "DAMAGED"
                elif ln["ReceivedQuantity"] < outstanding:
                    status = "SHORTAGE"
                elif ln["ReceivedQuantity"] > outstanding:
                    status = "OVERAGE"
                else:
                    status = "NONE"
                c.execute("UPDATE ReceiptItem SET DiscrepancyStatus=? WHERE ReceiptItemID=?", (status, ln["ReceiptItemID"]))
                if status != "NONE":
                    discrepancies.append({"ReceiptItemID": ln["ReceiptItemID"], "ProductID": po_item["ProductID"],
                                          "status": status, "outstanding": outstanding,
                                          "received": ln["ReceivedQuantity"], "damaged": ln["DamagedQuantity"]})
                if ln["AcceptedQuantity"] > 0:   # Inventory.applyAcceptedReceipt(qty)
                    c.execute("UPDATE Inventory SET CurrentStock = CurrentStock + ?, LastUpdatedAt=? WHERE ProductID=?",
                              (ln["AcceptedQuantity"], now(), po_item["ProductID"]))
                    added += ln["AcceptedQuantity"]
            fully = all(
                self._previously_accepted(it["POItemID"]) >= it["Quantity"]
                for it in c.execute("SELECT * FROM PurchaseOrderItem WHERE POID=?", (receipt["POID"],)).fetchall())
            order_status = "RECEIVED" if fully else "PARTIALLY_RECEIVED"
            c.execute("UPDATE PurchaseOrder SET Status=? WHERE POID=?", (order_status, receipt["POID"]))
        if discrepancies:
            self.raiseDiscrepancy(receipt_id, discrepancies)
        return ReceiptResult(receipt_id, len(lines), added, order_status, discrepancies)

    def raiseDiscrepancy(self, receipt_id: int, discrepancies: list) -> None:
        """Triggers Section 6.4 exception review; the agent only writes an audit record."""
        self.agent.reviewReceiptDiscrepancy(receipt_id, discrepancies)
