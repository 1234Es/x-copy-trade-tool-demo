# X Copy-Trade Tool

A cautious pipeline: monitor posts from selected X accounts, interpret them
with an LLM into a strict structured signal, validate and risk-check that
signal independently, and (depending on mode) log it, propose it for human
approval, or submit it to an **OANDA practice** account. **Not a
profitability tool and not connected to any live-money path.** See
`DESIGN.md` for the full architecture, schema, risk rules, and approval
workflow — this README is the operational companion.

## Live

This project is publicly hosted at **(URL pending first Render deploy)**.
Anyone can browse the dashboard read-only — Posts, Proposals, Trades,
Metrics, Accounts, and a Roadmap tab — with no signup. It runs against a
real **OANDA practice** account (never real money, never the `live`
environment — see Section 5 below). All mutating actions (approve/reject a
proposal, submit a post, toggle auto-approve, add/remove tracked accounts,
the kill switch) require an operator login, gated server-side, not just a
hidden button — see `app/auth.py`. The public deployment currently runs on
a free-tier host with an ephemeral filesystem, so trade/post history baked
into that deployment resets on each redeploy; see the Roadmap tab's "Next"
section for the planned fix.

## 1. What's implemented

- Manual/JSON input adapter (default), webhook adapter (with a companion
  browser extension in `browser_extension/` — see Section 3), and a full
  X API v2 adapter (built to the real endpoint contracts but unused by
  default — see DESIGN.md Section 2 for why: X has no free API tier for new
  developers as of Feb 2026).
- Rule-based pre-filter + OpenAI structured-output classifier (Phase 3).
- OpenAI structured-output signal extractor with hard-coded (not just
  prompted) safeguards: every `evidence` fragment must appear verbatim in
  the source post, and `requires_human_review` is recomputed server-side.
- A context engine that explicitly labels *why* any piece of context is
  included, and never assumes two nearby posts share a trade.
- Deterministic signal validation (staleness, instrument mapping, SL/TP
  logic, market-open, conflicting positions) independent of numeric risk
  thresholds (exposure, R:R, spread), which live in the risk manager.
- Three approval modes (`observe` / `approval` / `practice_auto`), one gate
  in the code, defaulting to `observe`.
- OANDA v20 practice-only broker integration (hard-refuses to construct
  against the live endpoint).
- A FastAPI dashboard (tabbed: Posts / Proposals / Trades / Submit &
  Settings): connection status, circuit breaker, manual post submission,
  pending proposals with approve/reject, a full trade history (open and
  closed, with P/L), account equity, and a live auto-approve toggle.
- SQLite audit trail: every post, classification, signal, proposal, order,
  and trade is recorded.

## 2. Setup instructions

### 2.1 Prerequisites
- Python 3.11+
- An OpenAI API key with access to a structured-outputs-capable model.
- An OANDA **practice** account (v20), if you want the broker side to do
  anything beyond logging.

### 2.2 Install
```bash
cd x_copy_trade_tool
python -m venv .venv
# Windows: .venv\Scripts\activate      macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
```

### 2.3 Configure secrets
```bash
cp .env.example .env
```
Fill in `OPENAI_API_KEY` and, if you want OANDA connected,
`OANDA_API_TOKEN`/`OANDA_ACCOUNT_ID` (practice only). **Leave `X_BEARER_TOKEN`
blank unless you've separately signed up for paid X API access** — the
tool works fully via the manual/webhook adapters without it.

**Important**: this project's own `.env` always takes precedence over a
system/shell-level environment variable of the same name (e.g. an
`OPENAI_API_KEY` set globally on your machine for other tools) --
`app/config/settings.py` deliberately overrides pydantic-settings' default
source order to make this tool self-contained. If a value is blank in
`.env`, the ambient environment variable (if any) is used as a fallback.
Check `GET /api/status` if you're ever unsure what's actually in effect --
though note `openai` there only confirms a key string is present, not that
it's valid (a bad key surfaces as a logged `classification_call_failed`
error on the first real post, not on the status endpoint).

### 2.4 Run
```bash
python -m app.main
```
Then open http://127.0.0.1:8000 in a browser.

### 2.5 Run the tests
```bash
pytest
```
72 unit/integration tests with OpenAI and OANDA both mocked — no real
network calls happen during the test suite, and no order is ever submitted
during tests.

## 3. Obtaining and configuring permitted API credentials

### OpenAI
1. Create a key at platform.openai.com (or your organization's console).
2. Set `OPENAI_API_KEY` in `.env`.
3. Set `OPENAI_MODEL` to a structured-outputs-capable model you have
   access to (defaults to `gpt-4o-2024-08-06`; verify current model
   availability/pricing directly with OpenAI before relying on this).

### OANDA (practice only)
1. Log into the OANDA fxTrade Account Management Portal.
2. Generate a personal access token ("Manage API Access").
3. Confirm the sub-account you're pointing at is a **v20 practice**
   account (not "OANDA One", which lacks some order-management features
   this tool uses) — see the sibling `oanda-spreadbet-bot` project's notes
   on this same distinction if you have access to it.
4. Set `OANDA_API_TOKEN`, `OANDA_ACCOUNT_ID`, leave
   `OANDA_ENVIRONMENT=practice`. There is no supported way to point this
   tool at a live account — `OandaPracticeBroker` raises
   `LiveTradingNotSupportedError` if `environment != "practice"`.

### X API (optional, only if you've separately obtained paid access)
1. As of Feb 2026 this requires signing up for X's pay-per-use API tier
   (or having a legacy Basic/Pro subscription) — see DESIGN.md Section 2
   for current pricing. There is no free path.
2. Set `X_BEARER_TOKEN`. The background poller in `app/main.py` starts
   automatically once this is non-empty, polling every tracked account in
   `app/config/tracked_accounts.yaml` every 60 seconds.

### Webhook adapter (for a browser extension or approved third-party feed)
1. Set `WEBHOOK_SHARED_SECRET` in `.env` to any random string. Leaving it
   blank disables the `/api/posts/webhook` route entirely (returns 403).
2. POST to `/api/posts/webhook` with header `X-Webhook-Secret: <your secret>`
   and a JSON body matching the manual-post shape (`author`, `text`, and
   optional `post_id`/`posted_at`/`reply_to_id`/`quoted_post_id`/`is_repost`/`media`).

### Browser extension (`browser_extension/`) — the recommended way to feed in real posts
A small, unpacked Chrome/Edge extension that adds a "→ Copy-Trade" button to
posts you're already looking at on x.com/twitter.com in your own logged-in
browser session. Clicking it opens an editable confirmation dialog (so you
can fix anything the DOM extraction got wrong or missed), then sends the
post to your local app over the webhook adapter above. **It never logs
in, never runs on a timer, and never touches your X credentials or session
cookies** — see DESIGN.md's answer on why this is meaningfully different
from browser-automation scraping.

Setup:
1. In `.env`, set `WEBHOOK_SHARED_SECRET` to a random string and restart
   `python -m app.main`.
2. Open `chrome://extensions` (or the Edge equivalent), enable "Developer
   mode", click "Load unpacked", and select the `browser_extension/`
   folder.
3. Click the extension's icon → Settings, and enter the same
   `WEBHOOK_SHARED_SECRET` value and the app's base URL (defaults to
   `http://127.0.0.1:8000`, which is correct if you haven't changed it).
4. Browse to x.com, find a post, click "→ Copy-Trade" on it, review/edit
   the pre-filled fields, and click Send. Check the dashboard's "Recent
   posts" feed to confirm it arrived.

Known limitation: extraction relies on X's current `data-testid` DOM
attributes (`tweet`, `tweetText`, `socialContext`) — these have been
stable for years but are not guaranteed, and reply/quote/repost detection
in particular is best-effort. If X changes their markup, extraction may
return blank fields; the confirmation dialog lets you fill them in by hand
rather than silently sending wrong data, but the extension itself would
need a selector update to keep auto-filling correctly.

## 4. Practice-account testing procedure

1. Start in `observe` mode (the default). Submit posts via the dashboard's
   "Submit a post manually" form, or `POST /api/posts/manual`, using
   realistic examples: an explicit entry, a cryptic follow-up ("adding
   here"), a post missing a stop loss, an unrelated post.
2. Watch `GET /api/posts` (or the dashboard feed) to see the full
   classification → extraction → validation trail for each, including
   rejection reasons.
3. Once you're comfortable with the interpretations, switch
   `APP_MODE=approval` and repeat — confirm proposals appear with the
   source post, evidence, and risk numbers, and that Approve/Reject work
   and that an unanswered proposal actually expires
   (`PROPOSAL_EXPIRY_MINUTES`).
4. **Auto-approve** (dashboard → Submit & Settings tab, or
   `AUTO_APPROVE_PROPOSALS` in `.env` as the startup default): when on, any
   signal that already passed every validation and risk check executes
   immediately with no click — including signals that would otherwise need
   review (missing stop/target via the fallback risk model, low
   confidence). This is a stronger bypass than `practice_auto`'s confidence
   threshold alone, since it also overrides `requires_human_review`. It
   never affects `observe` mode, which still never submits an order
   regardless. Toggling it on the dashboard takes effect immediately, no
   restart needed.
5. Only after that, consider `APP_MODE=practice_auto` — and even then,
   review the risk defaults in `app/config/risk.yaml` deliberately; they
   are starting points, not a recommendation.
6. Whichever mode, check `GET /api/trades` (dashboard's Trades tab) and PnL
   card against what the OANDA practice platform itself shows, periodically
   — this is your reconciliation check, in addition to the automatic one
   in `app/broker/reconciliation.py`.

## 5. Security checklist

- [ ] `.env` is not committed (`.gitignore` covers it) and contains no
      value copied into any generated file, log, or chat transcript.
- [ ] `OANDA_ENVIRONMENT` is `practice` — confirm this explicitly; the code
      refuses `live` at both the settings layer and the broker layer, but
      don't rely on that alone.
- [ ] `WEBHOOK_SHARED_SECRET` is a real random value if the webhook route
      is in use, not left as a guessable placeholder.
- [ ] Logs (`logs/app.log`) are checked periodically for anything the
      redaction patterns in `app/monitoring/logging.py` might have missed
      — redaction is pattern-based (bearer tokens, long hex strings,
      `sk-...` keys) and is a safety net, not a guarantee.
- [ ] No browser session cookies or X login credentials exist anywhere in
      this codebase — by design, since only the manual/webhook/official-API
      adapters are implemented. If you ever consider adding a scraping
      adapter, that is explicitly out of scope and against the brief this
      was built to.
- [ ] `APP_MODE` and `REQUIRE_HUMAN_APPROVAL` are checked before leaving
      the tool running unattended for any length of time.

## 6. Failure-recovery procedure

| Symptom | First step | Then |
|---|---|---|
| `GET /api/status` shows OANDA disconnected | Check `OANDA_API_TOKEN`/`OANDA_ACCOUNT_ID` in `.env`, and OANDA's own status page. | The tool falls back to `NullBroker` if credentials are absent at startup — restart after fixing `.env` rather than expecting hot-reload. |
| Circuit breaker tripped (dashboard shows it) | Check `circuit_breaker_events` in the SQLite DB for the trigger reason and details. | Daily/weekly loss trips and reconciliation-mismatch trips require investigating the underlying cause before clearing; use the dashboard's kill-switch-clear endpoint (`POST /api/kill-switch/clear`) only after you understand why it tripped. |
| A proposal never appears despite an obviously actionable post | Check `GET /api/posts` for that post's classification and (if present) rejection reason first. | Most "missing" proposals are a deliberate rejection somewhere in the pipeline (stale, unmapped instrument, low confidence) — the audit trail always has the reason; this is working as designed, not a bug, unless the reason itself looks wrong. |
| Reconciliation mismatch between local and OANDA state | Do not restart before investigating — compare `GET /api/trades/open` against the OANDA platform directly. | Resolve the specific discrepancy, then manually clear the `RECONCILIATION_MISMATCH` circuit-breaker trip. |
| Process crash / restart | Just restart (`python -m app.main`) — SQLite state persists across restarts, and the X-API poller (if enabled) resumes from its last processed post ID per tracked account (`cursors` table). | Check `GET /api/status` and open trades immediately after restart to confirm nothing was missed while down. |

## 7. Limitations and known failure cases

- **X ingestion is manual/webhook by default.** Nothing is monitored
  automatically unless you separately obtain paid X API access and set
  `X_BEARER_TOKEN`. This is a deliberate, disclosed choice (DESIGN.md
  Section 2), not an oversight.
- **No image/vision analysis.** A post whose meaning depends entirely on
  an attached chart image with no text is marked `missing_fields:
  ["image_content"]` and forced to human review — it is never interpreted
  from the image itself.
- **Non-English posts are out of scope for v1** and route to
  `too_ambiguous` rather than being (mis)translated and guessed at.
- **OANDA does not offer single US equities.** Posts about individual
  stocks ($AAPL, $TSLA, etc.) will correctly fail to map to any OANDA
  instrument and get rejected — this is expected, not a mapping gap to fix.
- **UK retail accounts cannot trade crypto CFDs** (FCA product intervention,
  2021). BTC/ETH aliases exist in `instruments.yaml` so the pipeline can
  still classify and extract crypto-related posts, but expect the
  broker-availability check to reject them on a UK retail practice account.
- **The instrument alias table is a best-effort starting guess.** Nobody
  on this project has authorized visibility into what @waltervannelli (or
  any other tracked account) actually posts about; real usage via the
  manual adapter will surface gaps in `app/config/instruments.yaml`, which
  is expected and should be extended over time, not treated as a defect.
  Adding a new alias is a config change, not a code change.
- **Sample size for any given account is inherently small at first.**
  Treat early practice-mode results the same way the sibling
  `oanda-spreadbet-bot` project treats backtests: a handful of trades is
  not validation, however good or bad they look.
- **The author glossary starts empty and only grows via explicit human
  confirmation** (`author_glossary.confirmed_by_human`). Until you've
  confirmed terms for a given author, ambiguous shorthand ("runner
  remains", "letting it work") will correctly reduce confidence and trigger
  human review rather than being guessed at.
- **Fallback stop-loss/take-profit model (`missing_stop_loss_behavior:
  apply_risk_model` in `app/config/risk.yaml`).** Some accounts never state
  a stop or target in text (e.g. "rolling in o/n 10 lots short at the
  average 1.11407" — a real position update with no risk parameters
  attached). With this deliberately enabled, a missing stop is computed
  from `fallback_stop_distance_pips` and a missing target from
  `fallback_reward_to_risk_multiple` × that stop distance, rather than
  rejecting the signal outright. Every order still gets a real protective
  stop — this only changes whether that stop came from the post or was
  computed — but it does mean the *level* is a system guess, not the
  author's actual risk management. Check `logs/app.log` for
  `fallback_risk_model_applied` entries to see exactly when this fired.
