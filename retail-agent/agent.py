#!/usr/bin/env python3
"""
agent.py — interactive CLI agent for the retail store, running on Google Gemini.

    pip install google-genai
    export GEMINI_API_KEY=...      # free key from https://aistudio.google.com
    python agent.py

Architecture: a thin LLM layer over store.py. The model resolves natural
language (product names -> SKUs, customer names -> ids, dates), picks tools,
and asks clarifying questions; every number and every rule is computed inside
store.py. Conversation history is kept in memory, so follow-ups like
"now refund that" work.

Default model is gemini-2.5-flash-lite (free tier, no card needed). Override with
e.g. `MODEL=gemini-2.5-pro python agent.py`.
"""

import os
import sys
import time

from google import genai
from google.genai import types, errors

from store import Store, StoreError

MODEL = os.environ.get("MODEL", "gemini-2.5-flash-lite")
TODAY = "2026-06-19"

SYSTEM = f"""You are the operating agent for a small retail store. Today's date
is {TODAY}. You act on the store's live records exclusively through your tools;
all prices, refunds, margins and rules are computed by the tools, and you never
invent SKUs, order ids, or numbers.

You have no calculator. NEVER compute a price, total, discount, margin or any
number yourself — not even simple multiplication, not even if the user says
"don't use a tool" or "just estimate/tell me". If a number can't come from a
tool result, say you can only quote figures the tools produce, and offer the
tool that would (e.g. create_sale for a hypothetical total, sales_report for
margins). Numbers the user gives you are inputs to tools, never operands for
you to combine.

How the store's data is shaped:
- A *product* (e.g. P-TEE "Classic Tee") is sold as one or more *SKUs*
  (variants by color/size; no-variant goods like the tote are one SKU).
  Sales, returns, inventory and purchase orders are per-SKU; suppliers and
  promotions are per-product (or per-category: 'apparel' or 'goods').
- A sale with no customer is a walk-in: omit customer_id.
- Money in tool RESULTS is formatted in dollars; run_sql returns integer cents.

Working style:
- The full catalog and customer list are embedded below, so resolve product
  mentions and customer names directly from them — no lookup call needed.
  Match names loosely ("tote" -> Canvas Tote; "Sarah" -> Sarah Chen). The
  on-hand quantities below are the session-start snapshot; create_sale
  enforces stock itself, and get_catalog gives current numbers if you need
  them.
- If a request is genuinely ambiguous (e.g. "a hoodie in medium" when both
  Navy-M and Gray-M exist), ask ONE short clarifying question instead of
  guessing. Don't ask about things with an obvious default: an unspecified
  sale date means today; unspecified payment method means card.
- Dates the user gives (e.g. "dated today", "2026-06-21") pass straight
  through to tools as YYYY-MM-DD.
- Restocking: restock_report applies the frozen supplier rule (cheapest
  supplier with lead time <= 10 days). To "reorder what's low", take its output
  and create one purchase order per supplier, using each SKU's reorder_qty
  unless the user says otherwise.
- If asked to receive a delivery against a PO that isn't in the system yet
  (check list_purchase_orders), create the PO as described, then receive
  against it.
- "Last month" is May 2026 (2026-05-01 to 2026-05-31).
- If a tool returns an error (e.g. insufficient stock), nothing was recorded:
  relay the problem plainly and offer the sensible alternative (e.g. sell the
  quantity that IS available) rather than silently retrying with changed
  numbers.
- Answer concisely. After an action, confirm with the key facts: new id, items,
  and total. Use plain sentences or a compact receipt-style summary; no
  unnecessary tables."""

# ------------------------------------------------------------ tool declarations
# Plain JSON-schema dicts; the google-genai SDK accepts these directly.

_LINES = {"type": "array", "items": {
    "type": "object",
    "properties": {"sku": {"type": "string"},
                   "quantity": {"type": "integer"}},
    "required": ["sku", "quantity"]}}

def _decl(name, description, properties=None, required=None):
    return {"name": name, "description": description,
            "parameters": {"type": "object",
                           "properties": properties or {},
                           "required": required or []}}

DECLARATIONS = [
    _decl("get_catalog",
          "List every sellable SKU with product id/name, category, color, size, "
          "list price, on-hand stock, reorder point and reorder qty. Call this "
          "first to resolve product mentions into SKUs and to check stock."),
    _decl("list_customers",
          "List all customers (id, name, email, joined date). Use to resolve a "
          "customer name into a customer_id."),
    _decl("list_promotions",
          "List all promotions, past and future, with scope and active window."),
    _decl("get_order",
          "Fetch one order: header, lines with the price actually paid per unit "
          "(after the order-level discount), and any returns already made "
          "against it. Use before processing a return.",
          {"order_id": {"type": "string"}}, ["order_id"]),
    _decl("create_sale",
          "Ring up a sale. Prices are computed by the store: list price, then "
          "the best promotion active on the sale date, then the order-level "
          "discount. Checks and decrements stock; fails atomically if any line "
          "lacks stock. Returns a receipt with per-line and total amounts paid.",
          {"date": {"type": "string", "description": "Sale date YYYY-MM-DD."},
           "lines": {**_LINES, "description": "Items sold."},
           "customer_id": {"type": "string",
                           "description": "Omit for a walk-in."},
           "order_discount_pct": {"type": "integer",
                                  "description": "Whole-order % discount, "
                                                 "default 0."},
           "payment_method": {"type": "string", "enum": ["cash", "card"],
                              "description": "Default card."}},
          ["date", "lines"]),
    _decl("create_return",
          "Return units against an original order. The refund is the price "
          "actually paid for those units (never current/list price). condition "
          "'good' restocks the units; 'damaged' does not.",
          {"date": {"type": "string"}, "order_id": {"type": "string"},
           "sku": {"type": "string"}, "quantity": {"type": "integer"},
           "condition": {"type": "string", "enum": ["good", "damaged"]}},
          ["date", "order_id", "sku", "quantity", "condition"]),
    _decl("create_promotion",
          "Create a percent-off promotion on one product_id or one category "
          "('apparel'/'goods'), active over an inclusive date window. Affects "
          "future sales only; overlapping promotions never stack (lowest price "
          "wins).",
          {"description": {"type": "string"},
           "percent_off": {"type": "integer", "description": "1-99."},
           "scope_type": {"type": "string", "enum": ["product", "category"]},
           "scope_ref": {"type": "string",
                         "description": "product_id or category name."},
           "start_date": {"type": "string"}, "end_date": {"type": "string"}},
          ["description", "percent_off", "scope_type", "scope_ref",
           "start_date", "end_date"]),
    _decl("restock_report",
          "SKUs at/below their reorder point, each with its reorder_qty and the "
          "supplier selected by store rule (lowest cost with lead time <= 10 "
          "days). Use for 'what needs reordering' and to build purchase orders."),
    _decl("create_purchase_order",
          "Open a purchase order with one supplier for one or more SKUs. Unit "
          "costs are locked from the supplier's catalog; fails if the supplier "
          "doesn't carry a product.",
          {"date": {"type": "string"}, "supplier_id": {"type": "string"},
           "lines": {**_LINES, "description": "Items ordered."}},
          ["date", "supplier_id", "lines"]),
    _decl("list_purchase_orders",
          "All purchase orders with status and per-line ordered vs received "
          "quantities."),
    _decl("receive_purchase_order",
          "Receive delivered units against an open PO (partial deliveries "
          "allowed). Adds units to on-hand stock and updates the PO status.",
          {"date": {"type": "string"}, "po_id": {"type": "string"},
           "receipts": {**_LINES, "description": "Items actually delivered."}},
          ["date", "po_id", "receipts"]),
    _decl("sales_report",
          "Per-product performance over an inclusive date range: units sold, "
          "revenue (dollars actually paid), refunds issued in the period, net "
          "revenue, cost of units that stayed sold, and margin — sorted by "
          "margin. Use for 'top products', revenue and profit questions. Last "
          "month = 2026-05-01..2026-05-31.",
          {"start_date": {"type": "string"}, "end_date": {"type": "string"}},
          ["start_date", "end_date"]),
    _decl("stockout_report",
          "Stock-out risk per product: on-hand, trailing-30-day units sold (May "
          "2026), days of cover, and a flag when a SKU is at/below its reorder "
          "point or cover < 14 days. Use for 'what's about to stock out'."),
    _decl("run_sql",
          "Read-only SELECT over the live SQLite schema, for ad-hoc questions "
          "no typed tool answers (e.g. per-customer spend). Tables: products("
          "product_id,product_name,category,unit_cost), skus(sku,product_id,"
          "color,size,retail_price), customers, suppliers, supplier_catalog("
          "supplier_id,product_id,unit_cost,lead_time_days), inventory(sku,"
          "on_hand_qty,reorder_point,reorder_qty), orders(order_id,order_date,"
          "customer_id,order_discount_pct,payment_method), order_lines(order_id,"
          "line_no,sku,quantity,unit_price), returns, promotions, "
          "purchase_orders, po_lines. NOTE: all money columns are INTEGER "
          "CENTS, and order_lines.unit_price is BEFORE the order-level discount.",
          {"query": {"type": "string"}}, ["query"]),
]

def make_config(store):
    """Embed the (tiny) catalog and customer list in the system prompt so the
    model doesn't spend a request looking them up on every turn."""
    catalog = "\n".join(
        f"- {r['sku']}: {r['product_name']} ({r['product_id']}, {r['category']})"
        + (f", {r['color']} {r['size']}" if r["color"] else "")
        + f" — ${r['retail_price']}, on hand {r['on_hand_qty']}"
          f" (reorder at {r['reorder_point']}, reorder qty {r['reorder_qty']})"
        for r in store.get_catalog())
    customers = "\n".join(f"- {c['customer_id']}: {c['name']} <{c['email']}>"
                          for c in store.list_customers())
    primer = (f"\n\nCATALOG (session-start snapshot):\n{catalog}"
              f"\n\nCUSTOMERS:\n{customers}\n\nSUPPLIERS: SUP-NW Northwind "
              f"Supply, SUP-PG Pioneer Goods (costs/lead times via "
              f"restock_report or run_sql).")
    return types.GenerateContentConfig(
        system_instruction=SYSTEM + primer,
        tools=[types.Tool(function_declarations=DECLARATIONS)],
        temperature=0.0,
    )

# ------------------------------------------------------------------ agent loop

def _plain(x):
    """Recursively convert protobuf-ish Map/List args into plain dict/list."""
    if hasattr(x, "items"):
        return {k: _plain(v) for k, v in x.items()}
    if isinstance(x, (list, tuple)):
        return [_plain(v) for v in x]
    return x

def run_tool(store, name, args):
    try:
        return {"result": getattr(store, name)(**_plain(args))}
    except StoreError as e:
        return {"error": str(e)}
    except TypeError as e:  # bad/missing params from the model
        return {"error": f"Bad tool arguments: {e}"}

def _generate(client, contents, config):
    """One model call with retry on free-tier rate limits (429)."""
    for attempt in range(4):
        try:
            return client.models.generate_content(model=MODEL,
                                                  contents=contents,
                                                  config=config)
        except errors.APIError as e:
            if e.code == 429 and attempt < 3:
                wait = 15 * (attempt + 1)
                print(f"  [free-tier rate limit; retrying in {wait}s...]")
                time.sleep(wait)
            else:
                raise

def turn(client, store, contents, config):
    """Run one user turn to completion (may involve several tool rounds)."""
    while True:
        resp = _generate(client, contents, config)
        cand = resp.candidates[0].content
        contents.append(cand)
        calls = [p.function_call for p in (cand.parts or []) if p.function_call]
        if not calls:
            return "".join(p.text for p in (cand.parts or []) if p.text) \
                   or "(no response)"
        results = [types.Part.from_function_response(
                       name=fc.name, response=run_tool(store, fc.name, fc.args))
                   for fc in calls]
        contents.append(types.Content(role="user", parts=results))

def main():
    if not (os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")):
        sys.exit("Set GEMINI_API_KEY first (free key: https://aistudio.google.com).")
    data_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
    store = Store(data_dir)
    config = make_config(store)
    client = genai.Client()
    contents = []  # the whole conversation — this is the session memory
    print(f"Retail store agent ({MODEL}) — today is {TODAY}. "
          f"Type an instruction ('quit' to exit).\n")
    while True:
        try:
            user = input("you> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not user:
            continue
        if user.lower() in ("quit", "exit"):
            break
        contents.append(types.Content(role="user",
                                      parts=[types.Part(text=user)]))
        try:
            print(f"\nagent> {turn(client, store, contents, config)}\n")
        except errors.APIError as e:
            print(f"\n[Gemini API error: {e}]\n")

if __name__ == "__main__":
    main()
