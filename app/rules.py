"""Supplied business rules (Phase II Section 8.8: held by the application, not stored in tables)."""

# MC-02: replenishment formula
DEMAND_WINDOW_DAYS = 30

# MC-03: purchasing criteria (weights must add up to 1)
SCORING_WEIGHTS = {"price": 0.40, "lead": 0.20, "rating": 0.25, "availability": 0.15}
ALLOWED_OVER_ORDER_PCT = 0.50   # MOQ may push the order up to 50 % above the required quantity

# 6.1 / 6.4: severity rules
RISK_RULES = "CRITICAL: stock 0 | HIGH: stock <= SafetyStock | MEDIUM: stock <= ReorderLevel | LOW otherwise"

# Receiving policy (7.2.4): accept good units up to what is still outstanding on the order line.
def receiving_policy_accepted(received_qty: int, damaged_qty: int, outstanding_qty: int) -> int:
    return max(0, min(received_qty - damaged_qty, outstanding_qty))

assert abs(sum(SCORING_WEIGHTS.values()) - 1.0) < 1e-9
