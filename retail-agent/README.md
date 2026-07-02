# Retail Store Agent

An interactive CLI agent that runs a small retail store: sales, returns,
promotions, restocking, purchase orders, and reporting — with all business
rules enforced deterministically in a domain layer, and Google Gemini on top
for language.

## Run it (free — no credit card)

1. Get a free Gemini API key: go to https://aistudio.google.com, sign in with
   any Google account, and click **Get API key**.
2. Then:

```bash
pip install -r requirements.txt
export GEMINI_API_KEY=your-key-here        # Windows PowerShell: $env:GEMINI_API_KEY="..."
python agent.py
```

That's it — one command, then talk to it:

```
you> Ring up two Classic Tees, Blue Medium, and one Canvas Tote for a walk-in paying cash, dated today.
agent> Done — order O-1016, 2× Classic Tee Blue M @ $25.00 and 1× Canvas Tote @ $18.00, total $68.00, cash, walk-in.
you> now return the tote, it came back damaged
agent> ...
```

The agent remembers earlier turns within the session. Type `quit` to exit.

Defaults to `gemini-2.5-flash-lite` (free tier, high request quota). For the
hardest multi-step prompts, `MODEL=gemini-2.5-pro python agent.py` (or
`gemini-2.5-flash`) is more reliable. On the free tier's requests-per-minute
limit the agent automatically waits and retries, so an occasional
`[free-tier rate limit; retrying...]` line is normal, not an error.

## Verify without any API key

The whole rule engine is testable deterministically:

```bash
python test_store.py        # the 10 sample-prompt scenarios
python test_edge_cases.py   # 63 edge cases + hardest corners
```

`test_store.py` replays the sample-prompt scenarios (pricing, promo windows,
refunds of the price actually paid, partial PO receipt, supplier selection, May
margins, stock-out flags) against hand-computed expected values.
`test_edge_cases.py` pushes on the sharp edges: half-up rounding boundaries,
inclusive promo windows, no-stacking, atomic multi-line sale failures,
over-returns net of prior returns, the lead-time-10 supplier tie, PO receipt
transitions, report definitions, and `run_sql` injection safety.

## Layout

| file | what it is |
|---|---|
| `store.py` | the domain model + every tool implementation (the "brain") |
| `agent.py` | tool declarations, system prompt, tool-use loop, interactive REPL |
| `test_store.py` | deterministic checks of the rules and sample scenarios |
| `test_edge_cases.py` | edge cases and hardest corners of the domain layer |
| `data/` | the provided seed CSVs + data dictionary |
| `WRITEUP.md` | domain model, tool layer, and approach |

State lives in an in-memory SQLite database seeded fresh from `data/` on every
start: mutations persist for the whole session, and every session starts from
the same known-good state, so test runs are reproducible.

`store.py` has zero LLM dependency — the agent layer is swappable (any
tool-calling model works by rewriting only `agent.py`).
