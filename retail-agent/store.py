"""
store.py — the domain layer ("the brain").

Everything the agent can do to the store lives here as a plain Python method on
`Store`. The LLM never does arithmetic or applies business rules; it only decides
*which* method to call. All rules from DATA_DICTIONARY.md (costing, discount
proration, refunds, supplier selection, promotion windows, margin, velocity) are
enforced in this file, so every answer is deterministic.

Design notes
- Backing store is an in-memory SQLite database seeded fresh from data/*.csv on
  every start. Within a session all mutations persist; every session starts from
  the same known-good state, which makes testing reproducible.
- Money is stored as integer cents everywhere (exact arithmetic). The single
  rounding rule — round each discounted per-unit price to the cent, half-up —
  is implemented once in `_round_pct`.
- The raw `products.csv` is split into two entities: `products` (the conceptual
  product, carrying its frozen Northwind unit cost per rule 1) and `skus` (the
  sellable variant). A no-variant product is simply a product with one sku whose
  color/size are NULL — no special case.
"""

import csv
import sqlite3
from pathlib import Path

TODAY = "2026-06-19"  # frozen "today" for the assignment

SCHEMA = """
CREATE TABLE products (
  product_id   TEXT PRIMARY KEY,
  product_name TEXT NOT NULL,
  category     TEXT NOT NULL,
  unit_cost    INTEGER NOT NULL          -- cents; frozen Northwind cost (rule 1)
);
CREATE TABLE skus (
  sku          TEXT PRIMARY KEY,
  product_id   TEXT NOT NULL REFERENCES products(product_id),
  color        TEXT,                     -- NULL for no-variant products
  size         TEXT,
  retail_price INTEGER NOT NULL          -- cents, list price
);
CREATE TABLE customers (
  customer_id TEXT PRIMARY KEY,
  name        TEXT NOT NULL,
  email       TEXT,
  joined_date TEXT
);
CREATE TABLE suppliers (
  supplier_id   TEXT PRIMARY KEY,
  supplier_name TEXT NOT NULL
);
CREATE TABLE supplier_catalog (
  supplier_id    TEXT REFERENCES suppliers(supplier_id),
  product_id     TEXT REFERENCES products(product_id),
  unit_cost      INTEGER NOT NULL,       -- cents
  lead_time_days INTEGER NOT NULL,
  PRIMARY KEY (supplier_id, product_id)
);
CREATE TABLE inventory (
  sku           TEXT PRIMARY KEY REFERENCES skus(sku),
  on_hand_qty   INTEGER NOT NULL,
  reorder_point INTEGER NOT NULL,
  reorder_qty   INTEGER NOT NULL
);
CREATE TABLE orders (
  order_id           TEXT PRIMARY KEY,
  order_date         TEXT NOT NULL,
  customer_id        TEXT REFERENCES customers(customer_id),  -- NULL = walk-in
  order_discount_pct INTEGER NOT NULL DEFAULT 0,
  payment_method     TEXT NOT NULL CHECK (payment_method IN ('cash','card'))
);
CREATE TABLE order_lines (
  order_id   TEXT REFERENCES orders(order_id),
  line_no    INTEGER,
  sku        TEXT REFERENCES skus(sku),
  quantity   INTEGER NOT NULL CHECK (quantity > 0),
  unit_price INTEGER NOT NULL,           -- cents; promo applied, order discount NOT
  PRIMARY KEY (order_id, line_no)
);
CREATE TABLE returns (
  return_id     TEXT PRIMARY KEY,
  return_date   TEXT NOT NULL,
  order_id      TEXT REFERENCES orders(order_id),
  sku           TEXT REFERENCES skus(sku),
  quantity      INTEGER NOT NULL CHECK (quantity > 0),
  condition     TEXT NOT NULL CHECK (condition IN ('good','damaged')),
  refund_amount INTEGER NOT NULL         -- cents
);
CREATE TABLE promotions (
  promo_id    TEXT PRIMARY KEY,
  description TEXT,
  type        TEXT NOT NULL CHECK (type = 'percent_off'),
  value       INTEGER NOT NULL,
  scope_type  TEXT NOT NULL CHECK (scope_type IN ('product','category')),
  scope_ref   TEXT NOT NULL,
  start_date  TEXT NOT NULL,
  end_date    TEXT NOT NULL
);
CREATE TABLE purchase_orders (
  po_id       TEXT PRIMARY KEY,
  supplier_id TEXT REFERENCES suppliers(supplier_id),
  order_date  TEXT NOT NULL,
  status      TEXT NOT NULL DEFAULT 'open'   -- open | partially_received | received
);
CREATE TABLE po_lines (
  po_id        TEXT REFERENCES purchase_orders(po_id),
  line_no      INTEGER,
  sku          TEXT REFERENCES skus(sku),
  qty_ordered  INTEGER NOT NULL CHECK (qty_ordered > 0),
  qty_received INTEGER NOT NULL DEFAULT 0,
  unit_cost    INTEGER NOT NULL,         -- cents, locked at order time
  PRIMARY KEY (po_id, line_no)
);
"""

# ---------------------------------------------------------------- money helpers

def cents(s):
    """'25.00' -> 2500"""
    d, _, c = str(s).strip().partition(".")
    return int(d) * 100 + int((c + "00")[:2])

def usd(c):
    """2500 -> '25.00'"""
    sign = "-" if c < 0 else ""
    c = abs(c)
    return f"{sign}{c // 100}.{c % 100:02d}"

def _round_pct(amount_cents, pct_off):
    """amount × (1 − pct/100), rounded to the cent, half-up (rules 2 & 5)."""
    q, r = divmod(amount_cents * (100 - pct_off), 100)
    return q + (1 if r * 2 >= 100 else 0)


class StoreError(Exception):
    """Raised for any rule violation; surfaced to the agent as {'error': ...}."""


class Store:
    def __init__(self, data_dir="data"):
        self.db = sqlite3.connect(":memory:")
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        self._seed(Path(data_dir))

    # ------------------------------------------------------------------ seeding

    def _seed(self, d):
        def rows(name):
            with open(d / name, newline="") as f:
                return list(csv.DictReader(f))

        prods = rows("products.csv")
        cat = rows("supplier_catalog.csv")
        nw_cost = {r["product_id"]: cents(r["unit_cost"])
                   for r in cat if r["supplier_id"] == "SUP-NW"}  # rule 1
        seen = set()
        for r in prods:
            if r["product_id"] not in seen:
                seen.add(r["product_id"])
                self.db.execute("INSERT INTO products VALUES (?,?,?,?)",
                                (r["product_id"], r["product_name"], r["category"],
                                 nw_cost[r["product_id"]]))
            self.db.execute("INSERT INTO skus VALUES (?,?,?,?,?)",
                            (r["sku"], r["product_id"], r["color"] or None,
                             r["size"] or None, cents(r["retail_price"])))
        for r in rows("customers.csv"):
            self.db.execute("INSERT INTO customers VALUES (?,?,?,?)",
                            (r["customer_id"], r["name"], r["email"], r["joined_date"]))
        for r in rows("suppliers.csv"):
            self.db.execute("INSERT INTO suppliers VALUES (?,?)",
                            (r["supplier_id"], r["supplier_name"]))
        for r in cat:
            self.db.execute("INSERT INTO supplier_catalog VALUES (?,?,?,?)",
                            (r["supplier_id"], r["product_id"], cents(r["unit_cost"]),
                             int(r["lead_time_days"])))
        for r in rows("inventory.csv"):
            self.db.execute("INSERT INTO inventory VALUES (?,?,?,?)",
                            (r["sku"], int(r["on_hand_qty"]), int(r["reorder_point"]),
                             int(r["reorder_qty"])))
        for r in rows("orders.csv"):
            self.db.execute("INSERT INTO orders VALUES (?,?,?,?,?)",
                            (r["order_id"], r["order_date"], r["customer_id"] or None,
                             int(r["order_discount_pct"]), r["payment_method"]))
        for r in rows("order_lines.csv"):
            self.db.execute("INSERT INTO order_lines VALUES (?,?,?,?,?)",
                            (r["order_id"], int(r["line_no"]), r["sku"],
                             int(r["quantity"]), cents(r["unit_price"])))
        for r in rows("returns.csv"):
            self.db.execute("INSERT INTO returns VALUES (?,?,?,?,?,?,?)",
                            (r["return_id"], r["return_date"], r["order_id"], r["sku"],
                             int(r["quantity"]), r["condition"], cents(r["refund_amount"])))
        for r in rows("promotions.csv"):
            self.db.execute("INSERT INTO promotions VALUES (?,?,?,?,?,?,?,?)",
                            (r["promo_id"], r["description"], r["type"], int(r["value"]),
                             r["scope_type"], r["scope_ref"], r["start_date"], r["end_date"]))
        self.db.commit()

    # ------------------------------------------------------------ small helpers

    def _q(self, sql, args=()):
        return [dict(r) for r in self.db.execute(sql, args)]

    def _one(self, sql, args=()):
        r = self.db.execute(sql, args).fetchone()
        return dict(r) if r else None

    def _next_id(self, table, col, prefix, start):
        n = self.db.execute(
            f"SELECT MAX(CAST(SUBSTR({col}, ?) AS INTEGER)) FROM {table}",
            (len(prefix) + 1,)).fetchone()[0]
        return f"{prefix}{(n or start - 1) + 1}"

    def _sku(self, sku):
        r = self._one("""SELECT s.*, p.product_name, p.category, p.unit_cost
                         FROM skus s JOIN products p USING (product_id)
                         WHERE s.sku = ?""", (sku,))
        if not r:
            raise StoreError(f"Unknown SKU '{sku}'. Use get_catalog to see valid SKUs.")
        return r

    def effective_unit_price(self, sku, on_date):
        """List price with the best active promotion applied (rule 5: inclusive
        window, lowest price wins, no stacking). Returns (price_cents, promo_id)."""
        s = self._sku(sku)
        best, best_promo = s["retail_price"], None
        for p in self._q("""SELECT * FROM promotions
                            WHERE type='percent_off' AND start_date<=? AND end_date>=?
                              AND ((scope_type='product'  AND scope_ref=?)
                                OR (scope_type='category' AND scope_ref=?))""",
                         (on_date, on_date, s["product_id"], s["category"])):
            price = _round_pct(s["retail_price"], p["value"])
            if price < best:
                best, best_promo = price, p["promo_id"]
        return best, best_promo

    def _paid_per_unit(self, unit_price, order_discount_pct):
        """Rule 2: actual price paid per unit, rounded to the cent half-up."""
        return _round_pct(unit_price, order_discount_pct)

    # ============================================================ TOOLS: lookups

    def get_catalog(self):
        """Every sellable SKU with variant info, prices, and current stock."""
        return self._q("""
            SELECT s.sku, s.product_id, p.product_name, p.category, s.color, s.size,
                   s.retail_price, i.on_hand_qty, i.reorder_point, i.reorder_qty
            FROM skus s JOIN products p USING (product_id)
                        JOIN inventory i USING (sku)
            ORDER BY s.product_id, s.sku""") | _money("retail_price")

    def list_customers(self):
        return self._q("SELECT * FROM customers ORDER BY customer_id")

    def list_promotions(self):
        return self._q("SELECT * FROM promotions ORDER BY promo_id")

    def get_order(self, order_id):
        """Order header, lines (with actual paid price per rule 2), and any
        returns already recorded against it."""
        o = self._one("SELECT * FROM orders WHERE order_id = ?", (order_id,))
        if not o:
            raise StoreError(f"No such order '{order_id}'.")
        lines = self._q("""SELECT l.line_no, l.sku, p.product_name, s.color, s.size,
                                  l.quantity, l.unit_price
                           FROM order_lines l JOIN skus s USING (sku)
                                JOIN products p USING (product_id)
                           WHERE l.order_id = ? ORDER BY l.line_no""", (order_id,))
        for l in lines:
            l["paid_per_unit"] = self._paid_per_unit(l["unit_price"], o["order_discount_pct"])
            l["line_total_paid"] = l["paid_per_unit"] * l["quantity"]
        o["order_total_paid"] = usd(sum(l["line_total_paid"] for l in lines))
        o["lines"] = lines | _money("unit_price", "paid_per_unit", "line_total_paid")
        o["returns"] = self._q("SELECT * FROM returns WHERE order_id = ?",
                               (order_id,)) | _money("refund_amount")
        return o

    # ============================================================= TOOLS: sales

    def create_sale(self, date, lines, customer_id=None, order_discount_pct=0,
                    payment_method="card"):
        """Ring up a sale. `lines` = [{sku, quantity}]. Prices are computed here:
        list price -> best active promo on `date` (rule 5) -> order discount
        prorated per unit (rule 2). Decrements stock; fails if insufficient."""
        if customer_id and not self._one(
                "SELECT 1 FROM customers WHERE customer_id=?", (customer_id,)):
            raise StoreError(f"Unknown customer '{customer_id}'. Use list_customers.")
        if payment_method not in ("cash", "card"):
            raise StoreError("payment_method must be 'cash' or 'card'.")
        if not lines:
            raise StoreError("A sale needs at least one line.")

        priced = []
        for l in lines:
            s = self._sku(l["sku"])
            qty = int(l["quantity"])
            if qty <= 0:
                raise StoreError(f"Quantity for {l['sku']} must be positive.")
            inv = self._one("SELECT on_hand_qty FROM inventory WHERE sku=?", (l["sku"],))
            if inv["on_hand_qty"] < qty:
                raise StoreError(
                    f"Insufficient stock for {l['sku']} ({s['product_name']}): "
                    f"requested {qty}, only {inv['on_hand_qty']} on hand. "
                    f"No part of this sale was recorded.")
            unit_price, promo = self.effective_unit_price(l["sku"], date)
            priced.append((s, qty, unit_price, promo))

        order_id = self._next_id("orders", "order_id", "O-", 1001)
        self.db.execute("INSERT INTO orders VALUES (?,?,?,?,?)",
                        (order_id, date, customer_id, int(order_discount_pct),
                         payment_method))
        out_lines, total = [], 0
        for i, (s, qty, unit_price, promo) in enumerate(priced, 1):
            self.db.execute("INSERT INTO order_lines VALUES (?,?,?,?,?)",
                            (order_id, i, s["sku"], qty, unit_price))
            self.db.execute("UPDATE inventory SET on_hand_qty = on_hand_qty - ? "
                            "WHERE sku = ?", (qty, s["sku"]))
            paid = self._paid_per_unit(unit_price, int(order_discount_pct))
            total += paid * qty
            out_lines.append({"line_no": i, "sku": s["sku"],
                              "product_name": s["product_name"], "color": s["color"],
                              "size": s["size"], "quantity": qty,
                              "unit_price": unit_price, "promo_applied": promo,
                              "paid_per_unit": paid, "line_total_paid": paid * qty})
        self.db.commit()
        return {"order_id": order_id, "order_date": date,
                "customer_id": customer_id or "(walk-in)",
                "order_discount_pct": int(order_discount_pct),
                "payment_method": payment_method,
                "lines": out_lines | _money("unit_price", "paid_per_unit",
                                            "line_total_paid"),
                "order_total_paid": usd(total)}

    def create_return(self, date, order_id, sku, quantity, condition):
        """Return units against an original sale. Refund = price actually paid
        (rule 3), never list/current price. 'good' units go back to stock;
        'damaged' do not. Rejects returning more than was bought (net of
        earlier returns)."""
        if condition not in ("good", "damaged"):
            raise StoreError("condition must be 'good' or 'damaged'.")
        quantity = int(quantity)
        if quantity <= 0:
            raise StoreError("Return quantity must be a positive whole number.")
        o = self._one("SELECT * FROM orders WHERE order_id=?", (order_id,))
        if not o:
            raise StoreError(f"No such order '{order_id}'.")
        line = self._one("""SELECT SUM(quantity) qty, unit_price FROM order_lines
                            WHERE order_id=? AND sku=?""", (order_id, sku))
        if not line or not line["qty"]:
            raise StoreError(f"Order {order_id} has no line for SKU {sku}.")
        already = self._one("""SELECT COALESCE(SUM(quantity),0) q FROM returns
                               WHERE order_id=? AND sku=?""", (order_id, sku))["q"]
        if quantity > line["qty"] - already:
            raise StoreError(
                f"Order {order_id} bought {line['qty']} × {sku}; {already} already "
                f"returned; cannot return {quantity} more.")
        paid = self._paid_per_unit(line["unit_price"], o["order_discount_pct"])
        refund = paid * quantity
        return_id = self._next_id("returns", "return_id", "R-", 2001)
        self.db.execute("INSERT INTO returns VALUES (?,?,?,?,?,?,?)",
                        (return_id, date, order_id, sku, quantity, condition, refund))
        restocked = condition == "good"
        if restocked:
            self.db.execute("UPDATE inventory SET on_hand_qty = on_hand_qty + ? "
                            "WHERE sku=?", (quantity, sku))
        self.db.commit()
        return {"return_id": return_id, "return_date": date, "order_id": order_id,
                "sku": sku, "quantity": quantity, "condition": condition,
                "paid_per_unit": usd(paid), "refund_amount": usd(refund),
                "restocked": restocked}

    # ======================================================== TOOLS: promotions

    def create_promotion(self, description, percent_off, scope_type, scope_ref,
                         start_date, end_date):
        """Create a percent-off promotion on a product_id or a category, active
        over [start_date, end_date] inclusive. Never changes past sales."""
        if scope_type == "product":
            if not self._one("SELECT 1 FROM products WHERE product_id=?", (scope_ref,)):
                raise StoreError(f"Unknown product_id '{scope_ref}'.")
        elif scope_type == "category":
            if not self._one("SELECT 1 FROM products WHERE category=?", (scope_ref,)):
                raise StoreError(f"Unknown category '{scope_ref}'.")
        else:
            raise StoreError("scope_type must be 'product' or 'category'.")
        if not (0 < int(percent_off) < 100):
            raise StoreError("percent_off must be between 1 and 99.")
        if end_date < start_date:
            raise StoreError("end_date is before start_date.")
        promo_id = self._next_id("promotions", "promo_id", "PR-", 1)
        self.db.execute("INSERT INTO promotions VALUES (?,?,?,?,?,?,?,?)",
                        (promo_id, description, "percent_off", int(percent_off),
                         scope_type, scope_ref, start_date, end_date))
        self.db.commit()
        return self._one("SELECT * FROM promotions WHERE promo_id=?", (promo_id,))

    # ======================================================= TOOLS: replenishment

    def _best_supplier(self, product_id):
        """Rule 4: lowest unit_cost among suppliers with lead_time_days <= 10."""
        return self._one("""SELECT sc.supplier_id, s.supplier_name, sc.unit_cost,
                                   sc.lead_time_days
                            FROM supplier_catalog sc JOIN suppliers s USING (supplier_id)
                            WHERE sc.product_id=? AND sc.lead_time_days <= 10
                            ORDER BY sc.unit_cost, sc.lead_time_days LIMIT 1""",
                         (product_id,))

    def restock_report(self):
        """SKUs at/below their reorder point, each with its suggested reorder_qty
        and the supplier chosen by rule 4 (cheapest that delivers within 10 days)."""
        out = []
        for r in self._q("""SELECT i.*, s.product_id, p.product_name
                            FROM inventory i JOIN skus s USING (sku)
                                 JOIN products p USING (product_id)
                            WHERE i.on_hand_qty <= i.reorder_point ORDER BY i.sku"""):
            best = self._best_supplier(r["product_id"])
            out.append({**r,
                        "supplier": best and {**best, "unit_cost": usd(best["unit_cost"])},
                        "line_cost": best and usd(best["unit_cost"] * r["reorder_qty"])})
        return {"as_of": TODAY, "skus_to_reorder": out} if out else \
               {"as_of": TODAY, "skus_to_reorder": [],
                "note": "Nothing is at or below its reorder point."}

    def create_purchase_order(self, date, supplier_id, lines):
        """Open a PO with a supplier. `lines` = [{sku, quantity}]. Unit costs are
        locked from the supplier's catalog; fails if the supplier doesn't carry a
        product."""
        sup = self._one("SELECT * FROM suppliers WHERE supplier_id=?", (supplier_id,))
        if not sup:
            raise StoreError(f"Unknown supplier '{supplier_id}'.")
        if not lines:
            raise StoreError("A purchase order needs at least one line.")
        priced = []
        for l in lines:
            s = self._sku(l["sku"])
            c = self._one("""SELECT unit_cost FROM supplier_catalog
                             WHERE supplier_id=? AND product_id=?""",
                          (supplier_id, s["product_id"]))
            if not c:
                raise StoreError(f"{sup['supplier_name']} does not supply "
                                 f"{s['product_name']} ({s['product_id']}).")
            qty = int(l["quantity"])
            if qty <= 0:
                raise StoreError(f"Quantity for {l['sku']} must be positive.")
            priced.append((s, qty, c["unit_cost"]))
        po_id = self._next_id("purchase_orders", "po_id", "PO-", 5001)
        self.db.execute("INSERT INTO purchase_orders VALUES (?,?,?,'open')",
                        (po_id, supplier_id, date))
        out, total = [], 0
        for i, (s, qty, cost) in enumerate(priced, 1):
            self.db.execute("INSERT INTO po_lines VALUES (?,?,?,?,0,?)",
                            (po_id, i, s["sku"], qty, cost))
            total += qty * cost
            out.append({"line_no": i, "sku": s["sku"],
                        "product_name": s["product_name"], "qty_ordered": qty,
                        "unit_cost": usd(cost), "line_cost": usd(qty * cost)})
        self.db.commit()
        return {"po_id": po_id, "supplier": sup["supplier_name"],
                "supplier_id": supplier_id, "order_date": date, "status": "open",
                "lines": out, "po_total_cost": usd(total)}

    def list_purchase_orders(self):
        """All purchase orders with line-level ordered vs received quantities."""
        pos = self._q("SELECT * FROM purchase_orders ORDER BY po_id")
        for po in pos:
            po["lines"] = self._q("""SELECT line_no, sku, qty_ordered, qty_received,
                                            unit_cost FROM po_lines WHERE po_id=?
                                     ORDER BY line_no""", (po["po_id"],)) \
                          | _money("unit_cost")
        return pos

    def receive_purchase_order(self, date, po_id, receipts):
        """Receive delivered units against an open PO (partial deliveries fine).
        `receipts` = [{sku, quantity}]. Adds to on-hand stock and marks the PO
        'received' when complete, else 'partially_received'."""
        po = self._one("SELECT * FROM purchase_orders WHERE po_id=?", (po_id,))
        if not po:
            raise StoreError(f"No such purchase order '{po_id}'. Use "
                             f"list_purchase_orders, or create it first if this PO "
                             f"exists only on paper.")
        for r in receipts:
            line = self._one("""SELECT * FROM po_lines WHERE po_id=? AND sku=?""",
                             (po_id, r["sku"]))
            if not line:
                raise StoreError(f"PO {po_id} has no line for SKU {r['sku']}.")
            qty = int(r["quantity"])
            outstanding = line["qty_ordered"] - line["qty_received"]
            if qty <= 0 or qty > outstanding:
                raise StoreError(f"PO {po_id} / {r['sku']}: {outstanding} units "
                                 f"outstanding; cannot receive {qty}.")
            self.db.execute("""UPDATE po_lines SET qty_received = qty_received + ?
                               WHERE po_id=? AND line_no=?""",
                            (qty, po_id, line["line_no"]))
            self.db.execute("UPDATE inventory SET on_hand_qty = on_hand_qty + ? "
                            "WHERE sku=?", (qty, r["sku"]))
        done = self.db.execute("""SELECT MIN(qty_received >= qty_ordered)
                                  FROM po_lines WHERE po_id=?""", (po_id,)).fetchone()[0]
        status = "received" if done else "partially_received"
        self.db.execute("UPDATE purchase_orders SET status=? WHERE po_id=?",
                        (status, po_id))
        self.db.commit()
        lines = self._q("""SELECT pl.sku, pl.qty_ordered, pl.qty_received,
                                  i.on_hand_qty AS on_hand_now
                           FROM po_lines pl JOIN inventory i USING (sku)
                           WHERE pl.po_id=?""", (po_id,))
        return {"po_id": po_id, "received_date": date, "status": status,
                "lines": lines}

    # ========================================================== TOOLS: reporting

    def sales_report(self, start_date, end_date):
        """Per-product performance over [start_date, end_date] inclusive, using
        the frozen definitions (rule 6): revenue = dollars actually paid on orders
        in the period; refunds = refunds issued in the period; net revenue =
        revenue − refunds; cost = frozen unit cost × units that stayed sold
        (units sold − units returned in good condition); margin = net revenue −
        cost. Sorted by margin, highest first."""
        stats = {p["product_id"]: {"product_id": p["product_id"],
                                   "product_name": p["product_name"],
                                   "units_sold": 0, "revenue": 0, "refunds": 0,
                                   "units_returned_good": 0,
                                   "_cost": p["unit_cost"]}
                 for p in self._q("SELECT * FROM products")}
        for r in self._q("""SELECT s.product_id, l.quantity, l.unit_price,
                                   o.order_discount_pct
                            FROM order_lines l JOIN orders o USING (order_id)
                                 JOIN skus s USING (sku)
                            WHERE o.order_date BETWEEN ? AND ?""",
                         (start_date, end_date)):
            st = stats[r["product_id"]]
            st["units_sold"] += r["quantity"]
            st["revenue"] += self._paid_per_unit(
                r["unit_price"], r["order_discount_pct"]) * r["quantity"]
        for r in self._q("""SELECT s.product_id, r.quantity, r.condition,
                                   r.refund_amount
                            FROM returns r JOIN skus s USING (sku)
                            WHERE r.return_date BETWEEN ? AND ?""",
                         (start_date, end_date)):
            st = stats[r["product_id"]]
            st["refunds"] += r["refund_amount"]
            if r["condition"] == "good":
                st["units_returned_good"] += r["quantity"]
        rows = []
        for st in stats.values():
            cost = st.pop("_cost") * (st["units_sold"] - st["units_returned_good"])
            rows.append({**st, "net_revenue": st["revenue"] - st["refunds"],
                         "cost_of_units_kept": cost,
                         "margin": st["revenue"] - st["refunds"] - cost})
        rows.sort(key=lambda r: -r["margin"])
        totals = {k: sum(r[k] for r in rows)
                  for k in ("units_sold", "revenue", "refunds", "net_revenue",
                            "cost_of_units_kept", "margin")}
        money = ("revenue", "refunds", "net_revenue", "cost_of_units_kept", "margin")
        return {"period": [start_date, end_date],
                "by_product": rows | _money(*money),
                "totals": {k: usd(v) if k in money else v for k, v in totals.items()}}

    def stockout_report(self):
        """Rule 7. Velocity = units sold in May 2026 (the trailing-30-day window).
        Days of cover = product on-hand ÷ (monthly units ÷ 30), across variants.
        A product is flagged if any SKU is at/below its reorder point OR the
        product has < 14 days of cover."""
        out = []
        for p in self._q("SELECT * FROM products ORDER BY product_id"):
            pid = p["product_id"]
            on_hand = self._one("""SELECT SUM(i.on_hand_qty) q FROM inventory i
                                   JOIN skus USING (sku) WHERE product_id=?""",
                                (pid,))["q"]
            sold = self._one("""SELECT COALESCE(SUM(l.quantity),0) q
                                FROM order_lines l JOIN orders o USING (order_id)
                                     JOIN skus s USING (sku)
                                WHERE s.product_id=? AND
                                      o.order_date BETWEEN '2026-05-01' AND '2026-05-31'""",
                             (pid,))["q"]
            cover = round(on_hand / (sold / 30), 1) if sold else None
            low = self._q("""SELECT i.sku, i.on_hand_qty, i.reorder_point
                             FROM inventory i JOIN skus USING (sku)
                             WHERE product_id=? AND i.on_hand_qty <= i.reorder_point""",
                          (pid,))
            reasons = []
            if low:
                reasons.append("at/below reorder point: " +
                               ", ".join(f"{r['sku']} ({r['on_hand_qty']}≤"
                                         f"{r['reorder_point']})" for r in low))
            if cover is not None and cover < 14:
                reasons.append(f"only {cover} days of cover (<14)")
            out.append({"product_id": pid, "product_name": p["product_name"],
                        "on_hand_total": on_hand, "units_sold_last_30d": sold,
                        "days_of_cover": cover, "about_to_stock_out": bool(reasons),
                        "reasons": reasons})
        return {"as_of": TODAY, "products": out,
                "flagged": [r["product_name"] for r in out if r["about_to_stock_out"]]}

    # ====================================================== TOOL: ad-hoc queries

    def run_sql(self, query):
        """Read-only SELECT against the live schema, for ad-hoc questions the
        typed tools don't cover. Money columns are INTEGER CENTS."""
        if not query.lstrip().lower().startswith(("select", "with")):
            raise StoreError("run_sql is read-only: SELECT/WITH statements only. "
                             "All mutations must go through the typed tools.")
        try:
            rows = [dict(r) for r in self.db.execute(query).fetchmany(200)]
        except sqlite3.Error as e:
            raise StoreError(f"SQL error: {e}")
        return {"rows": rows, "note": "money columns are integer cents",
                "truncated_at_200": len(rows) == 200}


# Small convenience: `list_of_dicts | _money('a','b')` formats cents -> '12.34'.
class _money:
    def __init__(self, *keys):
        self.keys = keys
    def __ror__(self, rows):
        for r in rows:
            for k in self.keys:
                if r.get(k) is not None:
                    r[k] = usd(r[k])
        return rows
