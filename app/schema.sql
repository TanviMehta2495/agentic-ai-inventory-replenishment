-- Phase II Section 8.3 table definitions (SQLite dialect).
-- Status / code values are upper-case codes mapped from the Phase I values.
PRAGMA foreign_keys = ON;

CREATE TABLE User (
  UserID    INTEGER PRIMARY KEY,
  FullName  VARCHAR(120) NOT NULL,
  Email     VARCHAR(254) NOT NULL UNIQUE,
  Role      VARCHAR(20)  NOT NULL CHECK (Role IN ('STORE_MANAGER','WAREHOUSE_EMPLOYEE','ADMIN')),
  IsActive  BOOLEAN NOT NULL DEFAULT 1
);

CREATE TABLE Product (
  ProductID INTEGER PRIMARY KEY,
  SKU       VARCHAR(40)  NOT NULL UNIQUE,
  Name      VARCHAR(150) NOT NULL,
  Category  VARCHAR(80),
  Unit      VARCHAR(20)  NOT NULL,
  IsActive  BOOLEAN NOT NULL DEFAULT 1
);

CREATE TABLE Inventory (
  InventoryID   INTEGER PRIMARY KEY,
  ProductID     INTEGER NOT NULL UNIQUE REFERENCES Product(ProductID),
  CurrentStock  INTEGER NOT NULL CHECK (CurrentStock >= 0),
  ReorderLevel  INTEGER NOT NULL CHECK (ReorderLevel >= 0),
  SafetyStock   INTEGER NOT NULL CHECK (SafetyStock >= 0),
  Location      VARCHAR(100) NOT NULL,
  LastUpdatedAt TIMESTAMP NOT NULL
);

CREATE TABLE SalesHistory (
  SaleID          INTEGER PRIMARY KEY,
  ProductID       INTEGER NOT NULL REFERENCES Product(ProductID),
  Quantity        INTEGER NOT NULL CHECK (Quantity > 0),
  SaleDate        TIMESTAMP NOT NULL,
  SourceReference VARCHAR(100)
);

CREATE TABLE Supplier (
  SupplierID        INTEGER PRIMARY KEY,
  Name              VARCHAR(150) NOT NULL,
  ContactName       VARCHAR(100),
  Email             VARCHAR(254),
  Phone             VARCHAR(30),
  LeadTime          INTEGER NOT NULL CHECK (LeadTime >= 0),
  PerformanceRating DECIMAL(3,2) CHECK (PerformanceRating BETWEEN 0 AND 5),
  IsApproved        BOOLEAN NOT NULL DEFAULT 0
);

CREATE TABLE ProductSupplier (
  ProductSupplierID    INTEGER PRIMARY KEY,
  ProductID            INTEGER NOT NULL REFERENCES Product(ProductID),
  SupplierID           INTEGER NOT NULL REFERENCES Supplier(SupplierID),
  UnitPrice            DECIMAL(12,2) NOT NULL CHECK (UnitPrice >= 0),
  MinimumOrderQuantity INTEGER NOT NULL CHECK (MinimumOrderQuantity > 0),
  Availability         VARCHAR(20) CHECK (Availability IN ('AVAILABLE','LIMITED','UNAVAILABLE')),
  LeadTime             INTEGER CHECK (LeadTime >= 0),
  LastVerifiedAt       TIMESTAMP,
  UNIQUE (ProductID, SupplierID)
);

CREATE TABLE ReplenishmentRecommendation (
  RecommendationID    INTEGER PRIMARY KEY,
  ProductID           INTEGER NOT NULL REFERENCES Product(ProductID),
  SupplierID          INTEGER REFERENCES Supplier(SupplierID),
  RecommendedQuantity INTEGER CHECK (RecommendedQuantity > 0),
  EstimatedCost       DECIMAL(14,2) CHECK (EstimatedCost >= 0),
  Reason              TEXT NOT NULL,
  RiskLevel           VARCHAR(15) CHECK (RiskLevel IN ('LOW','MEDIUM','HIGH','CRITICAL')),
  Status              VARCHAR(25) NOT NULL CHECK (Status IN ('NO_REPLENISHMENT','FLAGGED_FOR_REVIEW','PENDING_APPROVAL','APPROVED','REJECTED','REVISION_REQUESTED')),
  CreatedAt           TIMESTAMP NOT NULL,
  CreatedByAgent      VARCHAR(80) NOT NULL,
  ReviewedAt          TIMESTAMP,
  -- Section 8.4 rule 4
  CHECK (Status NOT IN ('PENDING_APPROVAL','APPROVED')
         OR (SupplierID IS NOT NULL AND RecommendedQuantity IS NOT NULL AND EstimatedCost IS NOT NULL))
);

CREATE TABLE Approval (
  ApprovalID       INTEGER PRIMARY KEY,
  RecommendationID INTEGER NOT NULL UNIQUE REFERENCES ReplenishmentRecommendation(RecommendationID),
  ApproverUserID   INTEGER NOT NULL REFERENCES User(UserID),
  Decision         VARCHAR(10) NOT NULL CHECK (Decision IN ('APPROVE','REJECT','REVISE')),
  DecisionDate     TIMESTAMP NOT NULL,
  Comments         TEXT,
  -- Section 8.4 rule 5
  CHECK (Decision = 'APPROVE' OR (Comments IS NOT NULL AND TRIM(Comments) <> ''))
);

CREATE TABLE PurchaseOrder (
  POID                 INTEGER PRIMARY KEY,
  SupplierID           INTEGER NOT NULL REFERENCES Supplier(SupplierID),
  RecommendationID     INTEGER NOT NULL UNIQUE REFERENCES ReplenishmentRecommendation(RecommendationID),
  OrderDate            DATE NOT NULL,
  ExpectedDeliveryDate DATE,
  Status               VARCHAR(20) NOT NULL CHECK (Status IN ('DRAFT','ON_HOLD','SUBMITTED','CONFIRMED','DELAYED','PARTIALLY_RECEIVED','RECEIVED','CLOSED')),
  TotalAmount          DECIMAL(14,2) NOT NULL CHECK (TotalAmount >= 0),
  CreatedAt            TIMESTAMP NOT NULL,
  HoldReason           TEXT  -- implementation note: stores MC-04 problems when ON_HOLD
);

CREATE TABLE PurchaseOrderItem (
  POItemID   INTEGER PRIMARY KEY,
  POID       INTEGER NOT NULL REFERENCES PurchaseOrder(POID),
  ProductID  INTEGER NOT NULL REFERENCES Product(ProductID),
  Quantity   INTEGER NOT NULL CHECK (Quantity > 0),
  UnitPrice  DECIMAL(12,2) NOT NULL CHECK (UnitPrice >= 0),
  LineAmount DECIMAL(14,2) NOT NULL CHECK (LineAmount >= 0),
  UNIQUE (POID, ProductID)
);

CREATE TABLE Receipt (
  ReceiptID        INTEGER PRIMARY KEY,
  POID             INTEGER NOT NULL REFERENCES PurchaseOrder(POID),
  ReceivedDate     TIMESTAMP NOT NULL,
  ReceivedByUserID INTEGER NOT NULL REFERENCES User(UserID),
  Notes            TEXT
);

CREATE TABLE ReceiptItem (
  ReceiptItemID        INTEGER PRIMARY KEY,
  ReceiptID            INTEGER NOT NULL REFERENCES Receipt(ReceiptID),
  POItemID             INTEGER NOT NULL REFERENCES PurchaseOrderItem(POItemID),
  ReceivedQuantity     INTEGER NOT NULL CHECK (ReceivedQuantity >= 0),
  AcceptedQuantity     INTEGER NOT NULL CHECK (AcceptedQuantity >= 0),
  DamagedQuantity      INTEGER NOT NULL DEFAULT 0 CHECK (DamagedQuantity >= 0),
  DiscrepancyStatus    VARCHAR(15) CHECK (DiscrepancyStatus IN ('NONE','SHORTAGE','OVERAGE','DAMAGED','RESOLVED')),
  LineDiscrepancyNotes TEXT
);

CREATE TABLE AgentDecision (
  DecisionID        INTEGER PRIMARY KEY,
  AgentType         VARCHAR(30) NOT NULL CHECK (AgentType IN ('INVENTORY_MONITORING','REPLENISHMENT_EVALUATION','PO_PREPARATION','EXCEPTION_REVIEW')),
  RelatedRecordType VARCHAR(30) NOT NULL CHECK (RelatedRecordType IN ('PRODUCT','RECOMMENDATION','PURCHASE_ORDER','RECEIPT')),
  RelatedRecordID   VARCHAR(40) NOT NULL,
  OutputSummary     TEXT NOT NULL,
  Timestamp         TIMESTAMP NOT NULL,
  ReviewStatus      VARCHAR(15) NOT NULL CHECK (ReviewStatus IN ('NO_ACTION','FLAGGED','ESCALATED','RESOLVED')),
  ConfidenceScore   DECIMAL(4,3) CHECK (ConfidenceScore BETWEEN 0 AND 1)
);

-- Simulated outbound channel to the Supplier pool (message flow in BPMN 4.3).
CREATE TABLE SupplierOutbox (
  MessageID  INTEGER PRIMARY KEY,
  POID       INTEGER NOT NULL REFERENCES PurchaseOrder(POID),
  ToEmail    VARCHAR(254),
  Body       TEXT NOT NULL,
  SentAt     TIMESTAMP NOT NULL
);
