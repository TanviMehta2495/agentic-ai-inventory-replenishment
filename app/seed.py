"""Sample data that exercises every decision path of the five method contracts."""
from datetime import datetime, timedelta

from .db import now

USERS = [
    (1, "Maria Lopez", "manager@store.example", "STORE_MANAGER"),
    (2, "Sam Carter", "warehouse@store.example", "WAREHOUSE_EMPLOYEE"),
]

# ProductID, SKU, Name, Category, Unit, CurrentStock, ReorderLevel, SafetyStock, daily demand
PRODUCTS = [
    (1, "BEV-1001", "Sparkling Water 12-pack", "Beverages", "case", 40, 120, 60, 12),   # needs reorder
    (2, "DAI-2002", "Organic Whole Milk 1 gal", "Dairy", "bottle", 18, 80, 40, 10),     # critical-ish, MOQ exclusion
    (3, "SNK-3003", "Sea Salt Chips 200 g", "Snacks", "bag", 500, 150, 80, 8),          # no replenishment
    (4, "HOM-4004", "Paper Towels 6-roll", "Household", "pack", 25, 60, 30, 5),        # no feasible supplier -> flagged
    (5, "PRD-5005", "Bananas (per lb)", "Produce", "lb", 0, 100, 50, 15),               # stock 0, critical
]

SUPPLIERS = [
    # ID, Name, Contact, Email, Phone, LeadTime, Rating, IsApproved
    (1, "FreshFlow Distributors", "Ana Ruiz", "orders@freshflow.example", "555-0101", 5, 4.6, 1),
    (2, "ValueSource Wholesale", "Tom Reed", "po@valuesource.example", "555-0102", 9, 3.8, 1),
    (3, "QuickMart Supply", "Lee Park", "sales@quickmart.example", "555-0103", 3, 4.1, 0),  # not approved
]

# ProductID, SupplierID, UnitPrice, MOQ, Availability, product LeadTime
PRODUCT_SUPPLIERS = [
    (1, 1, 6.40, 50, "AVAILABLE", None),
    (1, 2, 5.90, 100, "AVAILABLE", None),
    (1, 3, 5.50, 20, "AVAILABLE", None),          # excluded: supplier not approved
    (2, 1, 3.10, 40, "LIMITED", 2),
    (2, 2, 2.85, 600, "AVAILABLE", None),          # excluded: MOQ too high
    (3, 1, 1.75, 100, "AVAILABLE", None),
    (4, 2, 7.20, 30, "UNAVAILABLE", None),         # excluded -> no feasible supplier
    (5, 1, 0.55, 100, "AVAILABLE", 1),
    (5, 2, 0.49, 200, "AVAILABLE", None),
]


def seed(conn) -> None:
    ts = now()
    conn.executemany("INSERT INTO User VALUES (?,?,?,?,1)", USERS)
    for pid, sku, name, cat, unit, stock, rl, ss, daily in PRODUCTS:
        conn.execute("INSERT INTO Product VALUES (?,?,?,?,?,1)", (pid, sku, name, cat, unit))
        conn.execute(
            "INSERT INTO Inventory (ProductID, CurrentStock, ReorderLevel, SafetyStock, Location, LastUpdatedAt)"
            " VALUES (?,?,?,?,?,?)", (pid, stock, rl, ss, f"Aisle {pid}", ts))
        for d in range(30):  # 30 days of sales (inside the demand window) at the product's daily demand
            day = (datetime.now() - timedelta(days=d, minutes=5)).isoformat(timespec="seconds")
            conn.execute("INSERT INTO SalesHistory (ProductID, Quantity, SaleDate, SourceReference) VALUES (?,?,?,?)",
                         (pid, daily, day, f"POS-{pid}-{d}"))
    conn.executemany("INSERT INTO Supplier VALUES (?,?,?,?,?,?,?,?)", SUPPLIERS)
    conn.executemany(
        "INSERT INTO ProductSupplier (ProductID, SupplierID, UnitPrice, MinimumOrderQuantity, Availability, LeadTime, LastVerifiedAt)"
        " VALUES (?,?,?,?,?,?,?)", [(*row, ts) for row in PRODUCT_SUPPLIERS])
    conn.commit()
