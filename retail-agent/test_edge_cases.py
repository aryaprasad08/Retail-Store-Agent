"""Edge-case + hardest-scenario suite for the domain layer (store.py).

No LLM, no API key — this exercises the *brain* directly, which is where every
number and rule lives. It complements test_store.py (the 10 sample prompts)
with the nasty corners: rounding boundaries, promo windows/stacking, atomic
sale failures, over-returns, supplier-selection ties, PO receipt transitions,
report definitions, and run_sql injection safety.

Run:  python test_edge_cases.py      (exits non-zero if anything fails)
"""

import sqlite3
import sys

from store import Store, StoreError, _round_pct, cents, usd

FAILS = []

def check(name, cond):
    print(("PASS  " if cond else "FAIL  ") + name)
    if not cond:
        FAILS.append(name)

def rejects(name, fn, exc=StoreError):
    """Assert fn() raises `exc` (a clean, caught error) — not something else."""
    try:
        fn()
    except exc:
        check(name, True)
    except Exception as e:                       # noqa: BLE001 - deliberate
        print(f"FAIL  {name}  (raised {type(e).__name__}, not "
              f"{exc.__name__}: {e})")
        FAILS.append(name)
    else:
        print(f"FAIL  {name}  (no error raised)")
        FAILS.append(name)

def fresh():
    return Store("data")

def orders_count(s):
    return s._one("SELECT COUNT(*) c FROM orders")["c"]

def on_hand(s, sku):
    return s._one("SELECT on_hand_qty q FROM inventory WHERE sku=?", (sku,))["q"]

TODAY = "2026-06-19"

# ============================================================ money & rounding
# Whole-dollar seed prices never round; prove the half-up rule with the raw fn.
check("round half-up: 0.505 -> 0.51", _round_pct(101, 50) == 51)
check("round below half: 0.4949 -> 0.49", _round_pct(101, 51) == 49)
check("round exact: 20% off 25.00 = 20.00", _round_pct(2500, 20) == 2000)
check("round: 10% off 18.00 = 16.20", _round_pct(1800, 10) == 1620)
check("cents parses '7.5' -> 750", cents("7.5") == 750)
check("cents parses bare '9' -> 900", cents("9") == 900)
check("usd formats negatives", usd(-700) == "-7.00" and usd(0) == "0.00"
      and usd(1620) == "16.20")

# ================================================ promotion windows (inclusive)
s = fresh()
s.create_promotion("Tee window", 20, "product", "P-TEE",
                   "2026-06-20", "2026-06-22")
p_before, _ = s.effective_unit_price("TEE-BLU-M", "2026-06-19")
p_start, id_start = s.effective_unit_price("TEE-BLU-M", "2026-06-20")
p_end, _ = s.effective_unit_price("TEE-BLU-M", "2026-06-22")
p_after, id_after = s.effective_unit_price("TEE-BLU-M", "2026-06-23")
check("promo not active the day before window", p_before == 2500 and id_start)
check("promo active on start_date (inclusive)", p_start == 2000)
check("promo active on end_date (inclusive)", p_end == 2000)
check("promo not active the day after window", p_after == 2500 and id_after is None)

# ============================================= overlapping promos: lowest wins
s = fresh()
s.create_promotion("Apparel 10", 10, "category", "apparel", TODAY, TODAY)
s.create_promotion("Hoodie 25", 25, "product", "P-HOOD", TODAY, TODAY)
price, _ = s.effective_unit_price("HOOD-GRY-M", TODAY)
check("overlapping promos never stack; lowest price wins (45.00 not 40.50)",
      price == 4500)  # min(6000, -10%=5400, -25%=4500); NOT 6000*.9*.75=4050
# category promo reaches a goods SKU too
s.create_promotion("Goods 50", 50, "category", "goods", TODAY, TODAY)
g, _ = s.effective_unit_price("MUG", TODAY)
check("category 'goods' promo applies to MUG (6.00)", g == 600)

# ==================================================== create_sale hard corners
# Atomic: a later out-of-stock line must record NOTHING (no order, no decrement).
s = fresh()
rejects("multi-line sale fails atomically on any short line",
        lambda: s.create_sale(TODAY, [{"sku": "TEE-BLU-M", "quantity": 1},
                                       {"sku": "HOOD-NVY-L", "quantity": 100}]))
check("...and nothing was recorded (order count + stock unchanged)",
      orders_count(s) == 15 and on_hand(s, "TEE-BLU-M") == 22)

# promo + order discount compose (promo first, then prorate), each rounded.
s = fresh()
s.create_promotion("Tee 20", 20, "product", "P-TEE", TODAY, TODAY)
r = s.create_sale(TODAY, [{"sku": "TEE-BLU-M", "quantity": 1}],
                  order_discount_pct=10)
check("promo(20%)->20.00 then order discount(10%)->18.00 per unit",
      r["lines"][0]["paid_per_unit"] == "18.00"
      and r["lines"][0]["promo_applied"] is not None)

# ids increment; walk-in; stock decrements
s = fresh()
r = s.create_sale(TODAY, [{"sku": "MUG", "quantity": 2}], payment_method="cash")
check("new order id increments to O-1016, walk-in, cash, stock 30->28",
      r["order_id"] == "O-1016" and r["customer_id"] == "(walk-in)"
      and r["payment_method"] == "cash" and on_hand(s, "MUG") == 28)

rejects("unknown SKU rejected",
        lambda: s.create_sale(TODAY, [{"sku": "NOPE", "quantity": 1}]))
rejects("unknown customer rejected",
        lambda: s.create_sale(TODAY, [{"sku": "MUG", "quantity": 1}],
                              customer_id="C-999"))
rejects("zero quantity rejected",
        lambda: s.create_sale(TODAY, [{"sku": "MUG", "quantity": 0}]))
rejects("bad payment method rejected",
        lambda: s.create_sale(TODAY, [{"sku": "MUG", "quantity": 1}],
                              payment_method="bitcoin"))

# ===================================================== create_return hard corners
# Refund is the price actually PAID (rule 3), not list; damaged doesn't restock.
s = fresh()
r = s.create_return(TODAY, "O-1006", "TOTE", 1, "damaged")
check("damaged tote refunds paid 16.20 (not list 18.00), not restocked",
      r["refund_amount"] == "16.20" and r["restocked"] is False
      and on_hand(s, "TOTE") == 4)

# Over-return is net of prior returns: O-1006 bought 2 NVY-L, R-2001 took 1.
s = fresh()
rejects("cannot over-return net of an earlier return (1 left, ask 2)",
        lambda: s.create_return(TODAY, "O-1006", "HOOD-NVY-L", 2, "good"))
r = s.create_return(TODAY, "O-1006", "HOOD-NVY-L", 1, "good")
check("returning the last eligible unit works, good restocks 6->7",
      r["refund_amount"] == "54.00" and on_hand(s, "HOOD-NVY-L") == 7)

rejects("return for a SKU not on the order rejected",
        lambda: s.create_return(TODAY, "O-1006", "MUG", 1, "good"))
rejects("return against unknown order rejected",
        lambda: s.create_return(TODAY, "O-9999", "TOTE", 1, "good"))
rejects("invalid condition rejected",
        lambda: s.create_return(TODAY, "O-1006", "TOTE", 1, "worn"))
# Guard probe: create_return lacks an explicit qty>0 check (create_sale has one).
s = fresh()
rejects("zero-quantity return rejected cleanly (not a raw DB error)",
        lambda: s.create_return(TODAY, "O-1006", "TOTE", 0, "good"))
s = fresh()
rejects("negative-quantity return rejected cleanly",
        lambda: s.create_return(TODAY, "O-1006", "TOTE", -1, "good"))

# ================================================ supplier selection (rule 4)
s = fresh()
tote_sup = s._best_supplier("P-TOTE")
mug_sup = s._best_supplier("P-MUG")
hood_sup = s._best_supplier("P-HOOD")
check("TOTE -> Northwind 7.00 (Pioneer cheaper 6.50 but 14d > 10d, ineligible)",
      tote_sup["supplier_id"] == "SUP-NW" and tote_sup["unit_cost"] == 700)
check("MUG -> Pioneer 4.50 (lead time exactly 10 is eligible, beats NW 5.00)",
      mug_sup["supplier_id"] == "SUP-PG" and mug_sup["unit_cost"] == 450)
check("HOOD -> Northwind (only supplier that carries it)",
      hood_sup["supplier_id"] == "SUP-NW")

rr = s.restock_report()["skus_to_reorder"]
check("restock report: only TOTE below reorder, NW, 50 @ 7.00 = 350.00",
      len(rr) == 1 and rr[0]["sku"] == "TOTE"
      and rr[0]["supplier"]["supplier_id"] == "SUP-NW"
      and rr[0]["line_cost"] == "350.00")

# ============================================ purchase orders & receiving
s = fresh()
rejects("PO from a supplier that doesn't carry the product rejected",
        lambda: s.create_purchase_order(TODAY, "SUP-PG",
                                        [{"sku": "HOOD-GRY-M", "quantity": 5}]))
rejects("PO with zero quantity rejected",
        lambda: s.create_purchase_order(TODAY, "SUP-NW",
                                        [{"sku": "TOTE", "quantity": 0}]))
rejects("receiving against a non-existent PO rejected",
        lambda: s.receive_purchase_order(TODAY, "PO-9999",
                                        [{"sku": "TOTE", "quantity": 1}]))

# Two-stage receipt: 40 then 10 of 50 -> partial then received; 4 -> 44 -> 54.
s = fresh()
po = s.create_purchase_order(TODAY, "SUP-NW", [{"sku": "TOTE", "quantity": 50}])
check("PO opens with locked cost 7.00 and status open",
      po["status"] == "open" and po["lines"][0]["unit_cost"] == "7.00")
r1 = s.receive_purchase_order(TODAY, po["po_id"], [{"sku": "TOTE", "quantity": 40}])
check("partial receipt: status partially_received, stock 4->44",
      r1["status"] == "partially_received" and on_hand(s, "TOTE") == 44)
rejects("cannot receive more than outstanding (10 left, ask 20)",
        lambda: s.receive_purchase_order(TODAY, po["po_id"],
                                        [{"sku": "TOTE", "quantity": 20}]))
r2 = s.receive_purchase_order(TODAY, po["po_id"], [{"sku": "TOTE", "quantity": 10}])
check("final receipt: status received, stock 44->54",
      r2["status"] == "received" and on_hand(s, "TOTE") == 54)

# ==================================================== reporting definitions
s = fresh()
rep = s.sales_report("2026-05-01", "2026-05-31")
names = [(r["product_name"], r["margin"]) for r in rep["by_product"]]
check("May margins ranked: Tee420 > Hoodie282 > Socks120 > Tote108.20 > Mug70",
      names == [("Classic Tee", "420.00"), ("Pullover Hoodie", "282.00"),
                ("Wool Socks", "120.00"), ("Canvas Tote", "108.20"),
                ("Ceramic Mug", "70.00")])
check("May totals: 80 units, 54.00 refunds, 1000.20 margin",
      rep["totals"]["units_sold"] == 80 and rep["totals"]["refunds"] == "54.00"
      and rep["totals"]["margin"] == "1000.20")

# A period with no sales is all zeros, not an error.
empty = s.sales_report("2026-07-01", "2026-07-31")
check("empty period reports zeros, not a crash",
      empty["totals"]["units_sold"] == 0 and empty["totals"]["margin"] == "0.00")

# Creating a promotion now must not rewrite historical (May) numbers.
before = s.sales_report("2026-05-01", "2026-05-31")["totals"]["margin"]
s.create_promotion("Retroactive?", 90, "category", "apparel",
                   "2026-05-01", "2026-05-31")
after = s.sales_report("2026-05-01", "2026-05-31")["totals"]["margin"]
check("a new promotion never changes past sales", before == after == "1000.20")

# Stock-out: only the Canvas Tote, 12.0 days of cover, both reasons.
s = fresh()
so = s.stockout_report()
tote = next(r for r in so["products"] if r["product_id"] == "P-TOTE")
check("stockout: only Canvas Tote flagged, 12.0 days cover, two reasons",
      so["flagged"] == ["Canvas Tote"] and tote["days_of_cover"] == 12.0
      and len(tote["reasons"]) == 2)

# ==================================================== run_sql read-only guard
s = fresh()
for bad in ("DELETE FROM orders",
            "UPDATE inventory SET on_hand_qty = 999",
            "INSERT INTO customers VALUES ('C-9','x','y','z')",
            "DROP TABLE orders",
            "PRAGMA table_info(orders)"):
    rejects(f"run_sql refuses: {bad.split()[0]}", lambda b=bad: s.run_sql(b))
# Stacked-statement injection: SQLite executes one statement, so this errors out
# (as a caught StoreError) rather than running the DELETE.
rejects("run_sql blocks stacked-statement injection",
        lambda: s.run_sql("SELECT 1; DELETE FROM orders"))
check("...and the orders table is intact after all the above",
      orders_count(s) == 15)
check("run_sql allows a plain SELECT",
      s.run_sql("SELECT COUNT(*) n FROM orders")["rows"][0]["n"] == 15)
check("run_sql allows a WITH/CTE query",
      s.run_sql("WITH x AS (SELECT 1 a) SELECT a FROM x")["rows"][0]["a"] == 1)

# ================================================ create_promotion validation
s = fresh()
rejects("percent_off = 0 rejected", lambda: s.create_promotion(
    "z", 0, "product", "P-TEE", TODAY, TODAY))
rejects("percent_off = 100 rejected", lambda: s.create_promotion(
    "z", 100, "product", "P-TEE", TODAY, TODAY))
rejects("end before start rejected", lambda: s.create_promotion(
    "z", 10, "product", "P-TEE", "2026-06-22", "2026-06-20"))
rejects("unknown product scope rejected", lambda: s.create_promotion(
    "z", 10, "product", "P-XXX", TODAY, TODAY))
rejects("unknown category scope rejected", lambda: s.create_promotion(
    "z", 10, "category", "toys", TODAY, TODAY))
ok = s.create_promotion("Goods sale", 15, "category", "goods", TODAY, TODAY)
check("valid category promotion created", ok["scope_ref"] == "goods")

# ---------------------------------------------------------------------- summary
print()
if FAILS:
    print(f"{len(FAILS)} FAILED: " + "; ".join(FAILS))
    sys.exit(1)
print("All edge-case checks passed.")
