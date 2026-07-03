# Writeup

## The one idea

Split the system along the line of what each side is good at. **The LLM handles
language** — resolving "a hoodie in medium for Sarah Chen" into a SKU and a
customer id, noticing ambiguity, remembering that "that" means order O-1016.
**The domain layer handles truth** — every price, refund, margin, rounding
rule, and supplier choice is computed in `store.py` by plain code over SQLite.
The model never does arithmetic and never touches state except through typed
tools, so every answer that involves a number is deterministic and every
mutation is validated (no overselling stock, no over-returning, no receiving
more than a PO ordered). `test_store.py` proves the rules without an LLM in the
loop at all.

## Domain model

The flat export becomes eight core entities plus one the data implies but
doesn't contain (purchase orders — needed the moment the store reorders).
Schema is in `store.py` (`SCHEMA`); the shape:

```
products ──< skus ──< order_lines >── orders >── customers
   │           │  └──< returns ─────────┘ (returns reference order + sku)
   │           ├──1 inventory
   │           └──< po_lines >── purchase_orders >── suppliers
   ├──< supplier_catalog >───────────────────────────┘
   └──(promotions scope by product_id or category)
```

Decisions worth calling out:

- **`products` vs `skus`.** The raw `products.csv` conflates the conceptual
  product with the sellable unit. Splitting them makes the tee (6 variants) and
  the tote (1 variant with NULL color/size) the same shape — no special cases.
  It also gives supplier pricing, promotions, cost, velocity, and margin their
  natural home (the product) while sales, stock, and returns stay per-SKU.
- **Frozen cost on the product.** Rule 1 says every unit costs the Northwind
  price; `products.unit_cost` is seeded from Northwind's catalog rows so margin
  math never joins to supplier data at query time.
- **Prices as integer cents.** Exact arithmetic; the single rounding rule
  (per-unit discounted price, half-up to the cent — rules 2 and 5) is
  implemented once and used for sales, refunds, and promo prices.
- **Derived values are never stored.** An order line stores `unit_price`
  (promo already applied, matching the seed's convention) and the order stores
  `order_discount_pct`; the paid price is always recomputed. Refunds therefore
  automatically refund what was actually paid.
- **Purchase orders** carry a status (`open → partially_received → received`)
  and per-line `qty_ordered/qty_received`, so partial deliveries ("40 of 50
  arrived") are first-class.
- **In-memory SQLite, reseeded per session.** The dataset is tiny; a fresh,
  deterministic start every run makes evaluation reproducible, while all
  mutations persist across turns within the session.

## Tool layer

Fourteen tools. Full parameter schemas live in `agent.py` (`TOOLS`); each maps
1:1 to a `Store` method.

**Lookups** (how the model grounds language in ids)
- `get_catalog()` — every SKU with product, variant, list price, stock,
  reorder point/qty. The model calls this first to turn "Blue Medium Classic
  Tee" into `TEE-BLU-M` and to see stock.
- `list_customers()` — resolve "Sarah Chen" → `C-001`.
- `list_promotions()`, `get_order(order_id)`, `list_purchase_orders()` —
  inspect state before acting; `get_order` includes the *paid* per-unit price
  and prior returns, which is exactly what a return needs.

**Actions** (validated mutations)
- `create_sale(date, lines[{sku,quantity}], customer_id?, order_discount_pct?,
  payment_method?)` — prices each line as list → best active promo on the sale
  date (inclusive window, lowest wins, no stacking) → order discount prorated
  per unit, rounded half-up. Atomic stock check; decrements inventory; returns
  a receipt. Omitted `customer_id` = walk-in.
- `create_return(date, order_id, sku, quantity, condition)` — refunds the
  price actually paid; `good` restocks, `damaged` doesn't; rejects returning
  more than was bought net of prior returns.
- `create_promotion(description, percent_off, scope_type, scope_ref,
  start_date, end_date)` — future sales only.
- `create_purchase_order(date, supplier_id, lines)` — costs locked from the
  supplier's catalog; fails if the supplier doesn't carry a product.
- `receive_purchase_order(date, po_id, receipts)` — partial receipts allowed,
  can't exceed what's outstanding; bumps on-hand and PO status.

**Analysis** (the frozen definitions, precomputed)
- `restock_report()` — SKUs at/below reorder point, each with reorder_qty and
  the rule-4 supplier (cheapest with lead time ≤ 10 days — which is why the
  tote goes to Northwind at $7.00, not the cheaper-but-slow Pioneer Goods).
- `sales_report(start, end)` — per-product units, revenue (dollars actually
  paid), refunds issued in the period, net revenue, cost of units that stayed
  sold, margin; sorted by margin. A good return removes both its revenue and
  its cost; a damaged return removes revenue but the cost stays (the unit is
  gone).
- `stockout_report()` — per-product velocity (May 2026 as the trailing 30
  days), days of cover across variants, and the flag: any SKU at/below its
  reorder point OR < 14 days of cover.
- `run_sql(query)` — a read-only SELECT escape hatch for ad-hoc questions the
  typed tools don't cover (per-customer spend, cash vs card mix, …). Writes
  are rejected, so the business rules can't be bypassed.

## The agent

`agent.py` is ~120 lines of glue over Google Gemini (`google-genai` SDK,
default model `gemini-2.5-flash-lite` — chosen because its free tier needs no
credit card and includes function calling, so the project runs at zero cost).
It holds a system prompt (today's date, the data shape, resolution etiquette,
"last month" = May 2026), the standard function-calling loop at temperature 0,
and a REPL that appends every turn to one message list — that's the whole
memory model, and it's why "now refund that" works. Free-tier rate limits
(429s) are retried automatically with backoff. The prompt tells the model to
ask one short clarifying question when a request is genuinely ambiguous (both
hoodie colors exist in medium), to treat missing dates as today, and — because
tools fail atomically with plain-English errors — to relay a failure (e.g.
only 4 totes on hand for a 10-tote sale) and offer the sensible alternative
instead of improvising. For "receive against a PO" when no PO exists in the
system, it creates the PO as described, then receives against it.

Because `store.py` has no LLM dependency, the provider is a swappable detail:
pointing the same 14 tools at a different tool-calling model means rewriting
only `agent.py`.


## How I used AI

I used Claude Code as a pair programmer throughout. AI proposed the architecture, wrote the implementation, and made the call on the products/skus split — the mechanical layers where speed matters and the design space is narrow: the SQLite schema, the CSV seeding, the 14 tool declarations, and the tool-calling loop. During iteration I made specific calls based on what it produced — money as integer cents for exact rounding, and the reading of the ambiguous damaged-return rule (revenue reverses, but the store keeps the cost)./

I also used AI adversarially: generating edge cases and stress-testing the agent is how I found and fixed a real bug — create_return accepted a non-positive quantity and would have crashed the session with an uncaught database error instead of a clean, recoverable message. That became a regression test.

I didn't trust the AI-written code by eye — test_store.py and test_edge_cases.py check every rule deterministically with no model in the loop, which is how I validated it. And moving the agent layer to Gemini's google-genai SDK was AI-assisted and took minutes precisely because store.py has zero LLM dependency — the payoff of the split.
## What I'd do next

Persist the database to disk behind a `--reset` flag, add an `audit_log`
table of tool calls, and support promotions beyond `percent_off` (the schema's
`type` column is already the seam).
