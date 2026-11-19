"""Flask UI – the four screens of Phase II Section 7.2."""
import os

from flask import Flask, flash, redirect, render_template, request, session, url_for

from .agent import AIReplenishmentAgent, mark_resolved
from .db import connect, init_schema, now
from .seed import seed
from .services import ApprovalController, BusinessRuleError, ReceivingService, has_permission


def create_app(db_path: str | None = None) -> Flask:
    app = Flask(__name__)
    app.secret_key = os.environ.get("SECRET_KEY", "dev-only-key")
    db_path = db_path or os.environ.get("DB_PATH", "replenishment.db")
    fresh = db_path == ":memory:" or not os.path.exists(db_path)
    conn = connect(db_path)
    if fresh:
        init_schema(conn); seed(conn)
    agent = AIReplenishmentAgent(conn)
    app.config.update(CONN=conn, AGENT=agent)

    def user():
        uid = session.get("user_id", 1)
        return conn.execute("SELECT * FROM User WHERE UserID=?", (uid,)).fetchone()

    @app.context_processor
    def inject():
        return {"current_user": user(), "users": conn.execute("SELECT * FROM User").fetchall()}

    @app.post("/switch-user")
    def switch_user():
        session["user_id"] = int(request.form["user_id"])
        return redirect(url_for("receive") if user()["Role"] == "WAREHOUSE_EMPLOYEE" else url_for("dashboard"))

    # ------------------------------------------------------------- 7.2.1 Dashboard
    @app.get("/")
    def dashboard():
        latest = conn.execute(
            "SELECT p.ProductID, p.SKU, p.Name, i.CurrentStock, i.ReorderLevel, i.SafetyStock, a.OutputSummary,"
            " a.ReviewStatus, a.Timestamp FROM Product p JOIN Inventory i USING (ProductID) LEFT JOIN AgentDecision a"
            " ON a.DecisionID = (SELECT MAX(DecisionID) FROM AgentDecision WHERE AgentType='INVENTORY_MONITORING'"
            " AND RelatedRecordID = CAST(p.ProductID AS TEXT)) ORDER BY i.CurrentStock * 1.0 / MAX(i.ReorderLevel,1)"
        ).fetchall()
        pending = conn.execute("SELECT r.*, p.SKU, p.Name, s.Name AS SupplierName FROM ReplenishmentRecommendation r"
                               " JOIN Product p USING (ProductID) LEFT JOIN Supplier s ON s.SupplierID=r.SupplierID"
                               " WHERE r.Status IN ('PENDING_APPROVAL','FLAGGED_FOR_REVIEW') ORDER BY r.RecommendationID DESC"
                               ).fetchall()
        counts = {
            "flagged": sum(1 for r in latest if r["ReviewStatus"] == "FLAGGED"),
            "pending": sum(1 for r in pending if r["Status"] == "PENDING_APPROVAL"),
            "delayed": conn.execute("SELECT COUNT(*) FROM AgentDecision WHERE AgentType='EXCEPTION_REVIEW' AND"
                                    " RelatedRecordType='PURCHASE_ORDER' AND ReviewStatus='ESCALATED'").fetchone()[0],
            "discrepancies": conn.execute("SELECT COUNT(*) FROM AgentDecision WHERE AgentType='EXCEPTION_REVIEW' AND"
                                          " RelatedRecordType='RECEIPT' AND ReviewStatus='ESCALATED'").fetchone()[0],
        }
        return render_template("dashboard.html", products=latest, pending=pending, counts=counts)

    @app.post("/monitor")
    def run_monitoring():
        agent.monitorAll()
        flash("AI inventory monitoring run completed (6.1).", "ok")
        return redirect(url_for("dashboard"))

    @app.post("/evaluate/<int:product_id>")
    def evaluate(product_id):
        if not has_permission(conn, user()["UserID"], "evaluate"):
            flash("Only the store manager can request an evaluation.", "err"); return redirect(url_for("dashboard"))
        rid = agent.evaluateReplenishment(product_id)
        st = conn.execute("SELECT Status FROM ReplenishmentRecommendation WHERE RecommendationID=?", (rid,)).fetchone()[0]
        flash(f"Replenishment evaluation #{rid} recorded: {st.replace('_', ' ').title()}.", "ok")
        return redirect(url_for("review", rid=rid) if st != "NO_REPLENISHMENT" else url_for("dashboard"))

    # ------------------------------------------------------------- 7.2.2 Purchase Request Review
    ctrl = ApprovalController(conn, agent)

    @app.get("/requests/<int:rid>")
    def review(rid):
        req, ranked, excluded = ctrl.loadRequest(rid)
        inv = conn.execute("SELECT * FROM Inventory WHERE ProductID=?", (req["ProductID"],)).fetchone()
        approval = conn.execute("SELECT a.*, u.FullName FROM Approval a JOIN User u ON u.UserID=a.ApproverUserID"
                                " WHERE RecommendationID=?", (rid,)).fetchone()
        po = conn.execute("SELECT * FROM PurchaseOrder WHERE RecommendationID=?", (rid,)).fetchone()
        return render_template("review.html", req=req, inv=inv, ranked=ranked, excluded=excluded,
                               approval=approval, po=po)

    @app.post("/requests/<int:rid>/decision")
    def decide(rid):
        try:
            res = ctrl.recordDecision(rid, request.form["decision"], request.form.get("comments", ""),
                                      user()["UserID"])
            flash(res.message, "ok" if res.orderStatus != "ON_HOLD" else "warn")
        except BusinessRuleError as e:
            flash(str(e), "err")
        return redirect(url_for("review", rid=rid))

    # ------------------------------------------------------------- 7.2.3 Order Monitoring & Exceptions
    @app.get("/orders")
    def orders():
        pos = conn.execute("SELECT po.*, s.Name AS SupplierName FROM PurchaseOrder po JOIN Supplier s USING"
                           " (SupplierID) ORDER BY POID DESC").fetchall()
        exceptions = conn.execute("SELECT * FROM AgentDecision WHERE AgentType='EXCEPTION_REVIEW'"
                                  " ORDER BY CASE ReviewStatus WHEN 'ESCALATED' THEN 0 ELSE 1 END,"
                                  " CASE WHEN OutputSummary LIKE '[HIGH]%' THEN 0 ELSE 1 END, DecisionID DESC"
                                  ).fetchall()
        outbox = conn.execute("SELECT * FROM SupplierOutbox ORDER BY MessageID DESC LIMIT 10").fetchall()
        return render_template("orders.html", pos=pos, exceptions=exceptions, outbox=outbox)

    @app.post("/orders/review")
    def review_orders():
        n = len(agent.reviewOrderExceptions())
        flash(f"AI order review completed (6.4): {n} new record(s).", "ok")
        return redirect(url_for("orders"))

    @app.post("/orders/<int:po_id>/simulate-overdue")
    def simulate_overdue(po_id):
        """Demo helper: moves the expected date into the past so the delay path can be shown."""
        with conn:
            conn.execute("UPDATE PurchaseOrder SET ExpectedDeliveryDate=date('now','-3 day'),"
                         " OrderDate=date('now','-10 day') WHERE POID=?", (po_id,))
        flash(f"PO-{po_id} expected date moved 3 days into the past (demo).", "warn")
        return redirect(url_for("orders"))

    @app.post("/exceptions/<int:did>/resolve")
    def resolve(did):
        try:
            send = request.form.get("send_message")
            if send:
                row = conn.execute("SELECT RelatedRecordID FROM AgentDecision WHERE DecisionID=?", (did,)).fetchone()
                if not has_permission(conn, user()["UserID"], "resolve_exception"):
                    raise BusinessRuleError("Only the store manager can send supplier messages")
                with conn:
                    conn.execute("INSERT INTO SupplierOutbox (POID, ToEmail, Body, SentAt) SELECT POID, s.Email, ?, ?"
                                 " FROM PurchaseOrder JOIN Supplier s USING (SupplierID) WHERE POID=?",
                                 (request.form.get("message", ""), now(), int(row["RelatedRecordID"])))
            mark_resolved(conn, did, user()["UserID"], request.form.get("note", ""))
            flash("Exception resolved" + (" and message sent by the manager." if send else "."), "ok")
        except BusinessRuleError as e:
            flash(str(e), "err")
        return redirect(url_for("orders"))

    # ------------------------------------------------------------- 7.2.4 Receive Shipment
    @app.route("/receive", methods=["GET", "POST"])
    def receive():
        po_id = request.values.get("poid", type=int)
        po = items = None
        if po_id:
            po = conn.execute("SELECT po.*, s.Name AS SupplierName FROM PurchaseOrder po JOIN Supplier s USING"
                              " (SupplierID) WHERE POID=?", (po_id,)).fetchone()
            items = conn.execute(
                "SELECT poi.*, p.SKU, p.Name, (SELECT COALESCE(SUM(AcceptedQuantity),0) FROM ReceiptItem ri WHERE"
                " ri.POItemID=poi.POItemID) AS PrevAccepted FROM PurchaseOrderItem poi JOIN Product p USING (ProductID)"
                " WHERE POID=?", (po_id,)).fetchall()
        if request.method == "POST" and po:
            lines = [{"POItemID": it["POItemID"],
                      "ReceivedQuantity": request.form.get(f"rec_{it['POItemID']}", type=int, default=0),
                      "DamagedQuantity": request.form.get(f"dmg_{it['POItemID']}", type=int, default=0)}
                     for it in items]
            try:
                res = ReceivingService(conn, agent).recordReceipt(po_id, lines, user()["UserID"],
                                                                  request.form.get("notes", ""))
                msg = f"Receipt {res.receiptId} saved: {res.quantityAdded} unit(s) added to stock; order {res.orderStatus}."
                if res.discrepancies:
                    msg += " Discrepancy sent to exception review: " + ", ".join(d["status"] for d in res.discrepancies)
                flash(msg, "ok" if not res.discrepancies else "warn")
            except BusinessRuleError as e:
                flash(str(e), "err")
            return redirect(url_for("receive", poid=po_id))
        open_pos = conn.execute("SELECT POID, Status FROM PurchaseOrder WHERE Status IN"
                                " ('SUBMITTED','CONFIRMED','DELAYED','PARTIALLY_RECEIVED')").fetchall()
        return render_template("receive.html", po=po, items=items, open_pos=open_pos)

    return app


if __name__ == "__main__":
    # macOS uses port 5000 for AirPlay Receiver, so default to 5001
    create_app().run(debug=True, port=int(os.environ.get("PORT", 5001)))
