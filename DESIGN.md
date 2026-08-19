# X Copy-Trade Tool — Design Document

Status: **Implemented and live.** Covers the assumptions, feasibility,
architecture, data flow, signal schema, risk rules, and approval workflow
behind the running system — this is a record of the decisions the
implementation follows, not a proposal awaiting sign-off.

---

## 1. Scope decisions

### Core decisions
| Question | Decision |
|---|---|
| X ingestion method | Manual/JSON input adapter is the default, primary path. The full X API adapter is built to the real v2 endpoint contracts but stays unused by default — X has no free tier for new developers (see Section 2), so it only activates once paid API access is configured. |
| OANDA account | A UK/Ireland practice account, same family as a related project of mine (`oanda-spreadbet-bot`) — its own `.env` in this project, own token/account ID. |
| Instrument scope | FX majors + broad CFD coverage: EUR_USD, GBP_USD, USD_JPY, AUD_USD, US30_USD, SPX500_USD, NAS100_USD, XAU_USD, WTICO_USD, BTC_USD, ETH_USD (subject to actual OANDA instrument-metadata confirmation per instrument at runtime — see Section 2). |

### Defaults chosen (conservative, labeled, changeable in config)
- **Jurisdiction / OANDA region:** UK/Ireland, same practice-account family
  as the related project mentioned above. CFD-style order semantics, not
  spread-betting-specific — the order/position language matches standard
  v20 CFD/forex accounts.
- **Non-English posts:** out of scope for v1 — the classifier and extractor
  are only validated against English text. A post detected as non-English is
  routed to `too_ambiguous_to_classify` rather than guessed at.
- **Replies / reposts / quote posts / images:** replies and quote posts are
  ingested with their parent/quoted context recorded (Section 5 context
  engine) since they often carry the actual trade update. Pure reposts
  (no added commentary) are recorded but never treated as an independent
  signal — only the original post can be. Images are recorded as metadata
  (URL, alt text if provided) but **not** analyzed (no vision model call in
  v1) — a post whose entire meaning depends on an image (e.g. a chart
  screenshot with no text) is marked `missing_fields: ["image_content"]` and
  gets `requires_human_review: true`.
- **Signal validity window:** 10 minutes from `posted_at` by default
  (`MAX_SIGNAL_AGE_MINUTES=10`, matching `.env.example`). Chosen because
  retail FX/CFD levels move quickly enough that a post older than this is
  treated as stale for auto-execution purposes; still viewable in the
  dashboard for manual action.
- **Partial trade details inferred from earlier posts:** allowed **only**
  for explicitly-linked follow-ups (a reply to, or clear textual reference
  to, an existing tracked signal — see Section 5). Never inferred from mere
  recency or topical similarity.
- **Hosting:** started as a local-machine tool; now also runs as a public,
  operator-gated deployment (see the README's "Live" section).
- **Approval before each practice trade:** on by default —
  `REQUIRE_HUMAN_APPROVAL=true` and `APP_MODE=observe`. Practice
  auto-execution is a distinct, explicitly-opted-into mode (Section 6).

### Known gaps
- **OpenAI API key**: required in `.env` for the NLP pipeline to run at
  all — it's built against the documented Structured Outputs contract
  (verified below), but does nothing without a key configured.
- **X API credentials**: not needed under the default manual-adapter setup.
  Live monitoring requires separately signing up for X's pay-per-use API
  (Section 2) and setting `X_BEARER_TOKEN`.
- **What each tracked account actually posts about** is only knowable by
  watching real posts arrive — there's no authorized way to browse an
  account's history up front to pre-validate coverage without one of the
  permitted ingestion methods. The instrument-mapping table is a
  best-effort guess at likely coverage; real posts (via the manual adapter)
  reveal gaps, which is expected and handled safely (unmappable instrument
  → reject).

---

## 2. Feasibility assessment: X and OANDA access

### X (Twitter) API — verified 2026-07-13
- **No free tier for new developers as of February 2026.** X replaced the
  old Free/Basic/Pro tiers with **pay-per-use** as the default: $0.015 per
  post created ($0.20 if it contains a link), **$0.005 per post read**,
  capped at 2,000,000 reads/month. This requires signing up for API access
  and putting a billing method on file — there is no zero-cost path to
  programmatic reads anymore.
- Legacy Basic ($200/mo, 15,000 reads/month) and Pro ($5,000/mo) tiers still
  exist for developers who already had them, but are **closed to new
  signups**, and X has signaled legacy Basic subscribers migrate to
  pay-per-use after 2026-06-01 anyway.
- Rate limits (pay-per-use, still enforced): ~3,500 tweet reads / 15 min per
  app, ~450 search requests / 15 min per app, 15-minute rolling windows.
  A 24-hour dedup rule means re-requesting the same post/profile same-day
  is normally only billed once.
- **Given this, and a general preference for user-supplied data over
  unauthorized access**, the manual/JSON input adapter is the correct
  default, not a fallback of last resort. The `x_api_source.py` adapter is
  still built to the real v2 endpoint contracts (user lookup, user tweet
  timeline, tweet-by-ID) so it's ready the moment paid API access is
  configured — it's simply not wired into the default run configuration.
- Sources: [X API Pricing 2026 — Postproxy](https://postproxy.dev/blog/x-api-pricing-2026/), [X API Pricing — Blotato](https://www.blotato.com/blog/twitter-api-pricing), [X (Twitter) API in 2026 — SocialCrawl](https://www.socialcrawl.dev/blog/x-twitter-api-2026)

### OANDA practice API — verified 2026-07-12/13
- Current API: **v20 REST API**. Practice base URL
  `https://api-fxpractice.oanda.com`, streaming
  `https://stream-fxpractice.oanda.com`. Auth: `Authorization: Bearer <personal
  access token>` header, generated via the fxTrade Account Management Portal.
- Rate limits: 120 requests/sec REST, 20 concurrent streams, 2 new
  connections/sec.
- Order types: Market, Limit, Stop, Market-if-Touched, Take-Profit,
  Stop-Loss, Guaranteed Stop-Loss, Trailing Stop-Loss. Protective
  stop-loss/take-profit attach via `stopLossOnFill`/`takeProfitOnFill` on
  the parent order.
- **Operational note:** whichever practice sub-account this points at needs
  to be a standard v20 account with the expected instruments enabled —
  instrument metadata (`/v3/accounts/{id}/instruments`) is fetched at
  runtime and is the source of truth for what's actually tradeable, not the
  static list above.

### OpenAI Structured Outputs — verified 2026-07-13
- Correct mechanism: `response_format: {"type": "json_schema", "json_schema": {...}, "strict": true}`
  on Chat Completions, or `text: {"format": {"type": "json_schema", "strict":
  true, "schema": ...}}` on the Responses API. Strict mode enforces schema
  adherence (the model cannot emit a non-conforming response) and is
  supported on `gpt-4o-2024-08-06` and later, all GPT-4.1 variants, and
  current-generation models.
- `OPENAI_MODEL` is left configurable in `.env` rather than hard-coded to a
  specific snapshot, since model names change faster than this document —
  set it to whatever current structured-outputs-capable model is available.
- Source: [OpenAI — Structured model outputs](https://developers.openai.com/api/docs/guides/structured-outputs)

---

## 3. Architecture

```text
x_copy_trade_tool/
    app/
        config/          # settings, tracked accounts, instrument map
        sources/          # input adapters: manual, JSON, webhook, X API (unused by default)
        nlp/              # classifier, extractor, context engine, schemas, prompts
        broker/           # OANDA practice client, order manager, reconciliation
        risk/             # risk manager, position sizing, circuit breaker
        execution/        # signal validator, approval workflow, execution engine
        storage/          # SQLite models + repository
        monitoring/       # logging, alerts, health
        api/              # FastAPI server (dashboard backend)
        auth.py            # operator session auth for the public deployment
        main.py
    tests/
        unit/ integration/ fixtures/
    scripts/               # one-off maintenance scripts (e.g. sanitizing data before a public commit)
    data/                 # sqlite db file lives here, gitignored except the public seed
    .env.example
    requirements.txt
    Dockerfile / render.yaml
    README.md
```

### Why FastAPI + a small server-rendered dashboard, not Streamlit
Both were considered:

| | FastAPI (chosen) | Streamlit |
|---|---|---|
| Approval workflow (buttons that must reliably trigger a POST before a proposal expires) | Native — a real endpoint per action | Awkward — reruns the whole script on every interaction, easy to accidentally re-trigger side effects |
| Long-running background monitoring loop alongside a UI | Natural (background task / separate process, UI just reads state) | Fights Streamlit's rerun model |
| Multiple simultaneous "windows" (post feed, proposal queue, open trades) updating independently | Yes, via separate endpoints/polling | Whole-page reruns make this clunky |
| Setup speed for a first prototype | Slightly more boilerplate | Faster to a single chart/table |

Streamlit is faster to a *read-only* dashboard, but this tool's core
interaction — reviewing a proposal and clicking Approve/Reject before it
expires — is exactly the kind of stateful, action-triggering UI Streamlit
handles poorly. FastAPI with a minimal server-rendered HTML page (vanilla
JS + fetch, no heavy frontend framework needed) was the better fit and
wasn't meaningfully slower to build for a UI this size.

### Data flow

```
Input adapter (manual/JSON/webhook/X API)
        │  raw post + metadata
        ▼
storage: raw_posts table (immutable, append-only)
        │
        ▼
nlp/classifier.py ──▶ rule-based pre-filter (ticker/keyword detection)
        │                        │
        │              too obviously unrelated?
        │                        │  yes -> stop, log "unrelated", no OpenAI call
        ▼                        
   OpenAI structured classification (post category)
        │
   category in {new_trade, update_stop, update_target,
                partial_close, full_close}?  -- no -> stop, log category, no further processing
        ▼
context_engine.py ──▶ gathers parent/thread/author-recent/open-signals/
                       instrument-aliases/open-OANDA-positions
        │
        ▼
signal_extractor.py ──▶ OpenAI structured extraction (Section 4 schema)
        │
        ▼
schema validation (pydantic) ──▶ invalid JSON / schema violation -> reject, log raw response
        │
        ▼
execution/signal_validator.py ──▶ Section 6 checks (staleness, instrument
        │                          mapping, R:R, spread, duplicates, conflicts...)
        │  fails any check -> reject with reason, stop
        ▼
risk/risk_manager.py ──▶ Section 7 checks (independent of the above)
        │  fails -> reject with reason, stop
        ▼
execution/approval_workflow.py ──▶ mode-dependent (Section 6 approval modes):
        │   observe: stop here, log hypothetical trade
        │   approval: create proposal, wait for human click, expire if unanswered
        │   practice_auto: proceed only if confidence + all checks already passed
        ▼
broker/order_manager.py ──▶ OANDA practice order submission
        │
        ▼
storage: full audit trail + dashboard read model
```

---

## 4. Structured signal schema

Implemented as a Pydantic model (`app/nlp/schemas.py`) so it doubles as the
JSON Schema passed to OpenAI's `strict: true` structured output and as the
validator on the way back:

```json
{
  "post_id": "string",
  "author": "string",
  "signal_type": "new_trade | update_stop | update_target | partial_close | full_close | observation | no_trade",
  "instrument": "string | null",
  "direction": "long | short | null",
  "order_type": "market | limit | stop | null",
  "entry_price": "number | null",
  "entry_zone_low": "number | null",
  "entry_zone_high": "number | null",
  "stop_loss": "number | null",
  "take_profit": ["number"],
  "timeframe": "string | null",
  "valid_until": "ISO-8601 datetime | null",
  "referenced_trade_id": "string | null",
  "confidence": "number between 0 and 1",
  "evidence": ["exact text fragments supporting the interpretation"],
  "assumptions": ["string"],
  "missing_fields": ["string"],
  "requires_human_review": true,
  "reasoning_summary": "brief non-sensitive explanation"
}
```

Two behavioral rules enforced in code, not just prompted for:
- **`requires_human_review` is forced to `true` server-side** (regardless of
  what the model returns) whenever `confidence < MIN_SIGNAL_CONFIDENCE`, any
  of `instrument`/`direction`/`stop_loss` is null for a `new_trade` signal, or
  `missing_fields` is non-empty. The model's own judgment is a signal, not
  the final word.
- **`evidence` must be a verbatim substring check** — every string in
  `evidence` is checked against the original post text after extraction;
  any evidence fragment that doesn't literally appear in the source post
  fails validation and the whole signal is rejected (catches the model
  paraphrasing or inventing supporting text).

---

## 5. Conservative risk settings

Starting risk defaults, with rationale for each (all overridable in
`app/config/`):

| Setting | Default | Why |
|---|---|---|
| `risk_per_trade_percent` | 0.25% | Lower than the related TA-bot project's 0.5% — signal quality here depends on a third party's judgment plus an LLM's interpretation of it, two additional sources of error beyond a self-contained technical rule, so size down accordingly. |
| `max_daily_loss_percent` | 1.0% | Tight, matching an "err toward no trade" posture. |
| `max_weekly_loss_percent` | 2.5% | |
| `max_open_positions` | 2 | Copy-trading one account shouldn't need much concurrent exposure; keeps correlated-signal risk bounded. |
| `max_trades_per_day` | 5 | A real trader posting actionable signals more than ~5x/day would be unusual; a higher rate is more likely a pipeline bug or a source flooding low-quality posts. |
| `minimum_reward_to_risk` | 1.5 | Same floor as the related project — reject trades whose stated target doesn't clear this even if every other check passes. |
| `require_stop_loss` | true | Non-negotiable: no order is ever submitted without a real, computed protective stop. By default (`missing_stop_loss_behavior: human_review`) a post lacking one routes to human review. Optionally (`missing_stop_loss_behavior: apply_risk_model`, deliberately enabled per-deployment) a missing stop/target is instead computed from `fallback_stop_distance_pips` (per instrument, pips) and `fallback_reward_to_risk_multiple` — for accounts whose author never states a stop/target in text but whose direction and entry are otherwise trusted. This changes *where the stop comes from*, never *whether one is attached*. |

Additional controls beyond this starter set: max exposure per instrument,
max exposure per source account (relevant once more than one account is
tracked), max trades/hour, cooldown after a loss, cooldown after repeated
API failures, emergency kill switch, auto-shutdown after reconciliation
failure — all specified in full in `app/risk/risk_manager.py`.

**These are starting values, not guarantees of safe risk levels for any
given account.** Treat every number in this table as a hypothesis to
revisit once there's real practice-mode results to look at, the same way
the related project's defaults were meant to be revisited.

---

## 6. Human-approval workflow

Three modes (`APP_MODE` env var), default `observe`:

1. **`observe`** — full pipeline runs (classify → extract → validate →
   risk-check), a hypothetical order is logged with complete reasoning, and
   execution stops there. No OANDA order endpoint is ever called. This is
   where the tool starts and stays until `APP_MODE` is deliberately
   changed.

2. **`approval`** — same pipeline, but a passing signal becomes a
   **proposal**: source post, extracted fields, evidence, risk calculation,
   and a countdown are shown on the dashboard. Nothing is submitted to
   OANDA until an operator clicks Approve. Proposals expire automatically
   after `PROPOSAL_EXPIRY_MINUTES` (default 5) — an unanswered proposal is
   treated as a "no," not a pending trade that fires late.

3. **`practice_auto`** — a passing signal that also clears
   `confidence >= MIN_SIGNAL_CONFIDENCE` (default 0.90) submits directly to
   the OANDA **practice** account with no per-trade click — but every
   mandatory field, every risk check, and every validation rule from
   Sections 5–6 still applies identically; this mode changes *who* clicks
   "go," not *what* is allowed through. Live-account trading has no
   equivalent mode in this version — there is no configuration path that
   reaches a real-money endpoint.

The mode is a single source of truth read by
`execution/approval_workflow.py`; nothing downstream (risk manager, order
manager) behaves differently based on mode except at that one gate, so
switching modes can't accidentally bypass a validation or risk rule.

---

## 7. Database design (SQLite, repository layer over it)

- `raw_posts` — post_id (PK), author, text, posted_at, source, reply_to_id,
  quoted_post_id, is_repost, media_json, ingested_at, raw_payload_json.
  Append-only; a post is never mutated, an edit/delete is a new row
  referencing the original (full history preserved rather than an
  overwritten one).
- `classifications` — post_id (FK), category, rule_based_signals_json,
  openai_request_id, classified_at.
- `signals` — signal_id (PK, uuid), post_id (FK), full extracted schema
  (Section 4) as columns + json blob, validation_result, rejection_reason,
  created_at.
- `proposals` — proposal_id (PK), signal_id (FK), status
  (pending/approved/rejected/expired), expires_at, decided_at, decided_by.
- `orders` — order_id (PK, client-order-id used as OANDA idempotency key),
  signal_id (FK), oanda_order_id, instrument, units, status, submitted_at,
  broker_response_json.
- `trades` — oanda_trade_id (PK), order_id (FK), open/close price/time,
  realized_pl, exit_reason.
- `author_glossary` — author, term, meaning, confirmed_by_human,
  confirmed_at (a human-confirmed-only glossary of author-specific
  shorthand — see Section 5).
- `instrument_aliases` — alias (e.g. "gold", "$DXY"), oanda_instrument,
  notes.
- `circuit_breaker_events`, `reconciliation_log`, `account_snapshots` —
  same shape/purpose as the related project's equivalents.

Repository layer (`app/storage/repository.py`) is the only code that
touches SQL, using SQLAlchemy Core (not full ORM) against a `DATABASE_URL`
— swapping to PostgreSQL later is close to a connection-string change (see
the Roadmap tab's "Next" section for why that's actually planned: the
current public deployment's free-tier filesystem is ephemeral).

---

## 8. Public deployment

The system now runs two ways: locally (the setup in the README) and as a
public, operator-gated deployment (Docker + Render). The pipeline, risk
rules, and broker integration above are identical in both — the only
addition is `app/auth.py`, a session-based operator login that gates every
write route (approve/reject, kill switch, tracked accounts, manual post
submission) behind a password, enforced server-side by middleware rather
than by hiding a button. Public visitors get full read access to every
dashboard tab; nothing about the pipeline itself changes based on who's
looking at it.
