"""Deterministic checks of the domain layer (no LLM, no API key).
Run: python test_store.py
Each test mirrors one of the take-home's sample prompts or a frozen rule."""

from store import Store, StoreError

def fresh():
    return Store("data")

def check(name, cond):
    print(("PASS  " if cond else "FAIL  ") + name)
    assert cond, name

# Rule 2 worked example from the dictionary: O-1006 paid prices
s = fresh()
o = s.get_order("O-1006")
paid = {l["sku"]: l["paid_per_unit"] for l in o["lines"]}
check("O-1006 hoodie paid 54.00, tote paid 16.20",
      paid["HOOD-NVY-L"] == "54.00" and paid["TOTE"] == "16.20")

# P1: 2 Blue-M tees + 1 tote, walk-in, cash, today
s = fresh()
r = s.create_sale("2026-06-19", [{"sku": "TEE-BLU-M", "quantity": 2},
                                 {"sku": "TOTE", "quantity": 1}],
                  payment_method="cash")
check("P1 total 68.00, walk-in, stock decremented",
      r["order_total_paid"] == "68.00" and r["customer_id"] == "(walk-in)"
      and s._one("SELECT on_hand_qty q FROM inventory WHERE sku='TOTE'")["q"] == 3)

# P2: 10 totes but only 4 on hand -> atomic failure
s = fresh()
try:
    s.create_sale("2026-06-19", [{"sku": "TOTE", "quantity": 10}])
    ok = False
except StoreError as e:
    ok = "only 4 on hand" in str(e)
check("P2 insufficient stock rejected, nothing recorded",
      ok and s._one("SELECT COUNT(*) c FROM orders")["c"] == 15)

# P4: restock report -> only TOTE, from Northwind (PG is cheaper but 14d lead)
s = fresh()
rr = s.restock_report()["skus_to_reorder"]
check("P4 only TOTE below reorder point, Northwind selected, 50 @ 7.00",
      len(rr) == 1 and rr[0]["sku"] == "TOTE"
      and rr[0]["supplier"]["supplier_id"] == "SUP-NW"
      and rr[0]["reorder_qty"] == 50 and rr[0]["line_cost"] == "350.00")

# P5: PO for 50 totes from Northwind, receive 40 -> partial, stock 4 -> 44
po = s.create_purchase_order("2026-06-19", "SUP-NW", [{"sku": "TOTE", "quantity": 50}])
rec = s.receive_purchase_order("2026-06-19", po["po_id"],
                               [{"sku": "TOTE", "quantity": 40}])
check("P5 partial receipt: status partially_received, on hand 44",
      rec["status"] == "partially_received"
      and rec["lines"][0]["on_hand_now"] == 44)

# P6: Sarah Chen returns 1 Navy-L hoodie from O-1006, good.
# She bought 2 and already returned 1 (R-2001) -> this returns the last one.
s = fresh()
r = s.create_return("2026-06-19", "O-1006", "HOOD-NVY-L", 1, "good")
check("P6 refund is paid price 54.00 (not 60.00) and unit restocked",
      r["refund_amount"] == "54.00" and r["restocked"]
      and s._one("SELECT on_hand_qty q FROM inventory WHERE sku='HOOD-NVY-L'")["q"] == 7)
try:  # a third return must be rejected — only 2 were bought
    s.create_return("2026-06-19", "O-1006", "HOOD-NVY-L", 1, "good")
    ok = False
except StoreError:
    ok = True
check("P6 over-return rejected", ok)

# P7: tote from O-1006 comes back damaged -> refund 16.20, NOT restocked
s = fresh()
r = s.create_return("2026-06-19", "O-1006", "TOTE", 1, "damaged")
check("P7 damaged: refund 16.20, stock unchanged",
      r["refund_amount"] == "16.20" and not r["restocked"]
      and s._one("SELECT on_hand_qty q FROM inventory WHERE sku='TOTE'")["q"] == 4)

# P8: hoodie promo 20% off 06-20..06-22; Gray-M on 06-21 -> 48.00;
# outside the window it's 60.00 again
s = fresh()
s.create_promotion("Hoodie flash sale", 20, "product", "P-HOOD",
                   "2026-06-20", "2026-06-22")
sale = s.create_sale("2026-06-21", [{"sku": "HOOD-GRY-M", "quantity": 1}])
check("P8 promo price 48.00 inside window",
      sale["lines"][0]["paid_per_unit"] == "48.00"
      and sale["lines"][0]["promo_applied"] is not None)
after = s.create_sale("2026-06-23", [{"sku": "HOOD-GRY-M", "quantity": 1}])
check("P8 back to 60.00 after window",
      after["lines"][0]["paid_per_unit"] == "60.00")

# Rule 5: two overlapping promos -> lower price wins, no stacking
s.create_promotion("Apparel 10%", 10, "category", "apparel",
                   "2026-06-20", "2026-06-22")
p, _ = s.effective_unit_price("HOOD-GRY-M", "2026-06-21")
check("Rule 5 overlapping promos: lowest price wins (48.00, not 43.20)",
      p == 4800)

# P9: top 5 by margin, May 2026 (hand-computed from the seed)
s = fresh()
rep = s.sales_report("2026-05-01", "2026-05-31")
top = [(r["product_name"], r["margin"]) for r in rep["by_product"][:5]]
check("P9 margins: Tee 420 > Hoodie 282 > Socks 120 > Tote 108.20 > Mug 70",
      top == [("Classic Tee", "420.00"), ("Pullover Hoodie", "282.00"),
              ("Wool Socks", "120.00"), ("Canvas Tote", "108.20"),
              ("Ceramic Mug", "70.00")])
check("P9 hoodie net revenue 534.00 (588 paid − 54 refund), 9 units' cost",
      rep["by_product"][1]["net_revenue"] == "534.00"
      and rep["by_product"][1]["cost_of_units_kept"] == "252.00")

# P10: stock-out — only the Canvas Tote (4 ≤ reorder 10 AND 12 days cover)
s = fresh()
so = s.stockout_report()
tote = next(r for r in so["products"] if r["product_id"] == "P-TOTE")
check("P10 only Canvas Tote flagged; 12.0 days cover, both reasons",
      so["flagged"] == ["Canvas Tote"] and tote["days_of_cover"] == 12.0
      and len(tote["reasons"]) == 2)

# run_sql is read-only
s = fresh()
try:
    s.run_sql("DELETE FROM orders")
    ok = False
except StoreError:
    ok = True
check("run_sql rejects mutations", ok)
check("run_sql answers ad-hoc questions",
      s.run_sql("SELECT COUNT(*) n FROM orders")["rows"][0]["n"] == 15)

print("\nAll checks passed.")
