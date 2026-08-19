"""Entry point. Builds the shared AppContext and serves the FastAPI
dashboard. Manual/webhook posts are processed synchronously in their route
handlers (app/api/server.py) -- there is no separate polling loop needed
for the default input adapters. If X_BEARER_TOKEN is configured, a
background poller is also started for each enabled tracked account.
"""
from __future__ import annotations

import os
import threading
import time
from datetime import datetime, timezone

import requests
import uvicorn

from app.api.server import create_app
from app.context import build_context
from app.monitoring.logging import get_logger
from app.sources.x_api_source import XApiSource

log = get_logger("main")

X_POLL_INTERVAL_SECONDS = 60
RECONCILIATION_INTERVAL_SECONDS = 30
SELF_PING_INTERVAL_SECONDS = 600


def _x_polling_loop(context) -> None:  # noqa: ANN001
    x_source = XApiSource(bearer_token=context.settings.x_bearer_token, base_url=context.settings.x_api_base_url)
    log.info("x_api_polling_started", accounts=[a["username"] for a in context.tracked_accounts if a.get("enabled")])
    while True:
        for account in context.tracked_accounts:
            if not account.get("enabled"):
                continue
            username = account["username"]
            cursor_key = f"x_api:{username}"
            since_id = context.repository.get_last_processed_post_id(cursor_key)
            try:
                posts = x_source.fetch_new_posts_for_username(username, since_id)
            except Exception as exc:  # noqa: BLE001 -- one bad poll must not kill the loop
                log.error("x_api_poll_failed", username=username, error=str(exc))
                context.risk_manager.record_api_error(datetime.now(timezone.utc))
                continue
            for post in posts:
                context.execution_engine.process_post(post)
                context.repository.set_last_processed_post_id(cursor_key, post.post_id, datetime.now(timezone.utc))
        time.sleep(X_POLL_INTERVAL_SECONDS)


def _reconciliation_loop(context) -> None:  # noqa: ANN001
    log.info("reconciliation_loop_started", interval_seconds=RECONCILIATION_INTERVAL_SECONDS)
    while True:
        try:
            summary = context.reconciler.reconcile(risk_manager=context.risk_manager)
            for trade_id in summary.closed_synced:
                log.info("trade_closed_synced", oanda_trade_id=trade_id)
            if summary.has_unexplained_mismatch:
                log.error(
                    "reconciliation_mismatch",
                    unexplained=summary.unexplained,
                    open_at_broker_not_local=summary.open_at_broker_not_local,
                )
        except Exception as exc:  # noqa: BLE001 -- one bad reconciliation pass must not kill the loop
            log.error("reconciliation_pass_failed", error=str(exc))
            context.risk_manager.record_api_error(datetime.now(timezone.utc))
        time.sleep(RECONCILIATION_INTERVAL_SECONDS)


def _self_ping_loop(public_url: str) -> None:
    """Only runs when PUBLIC_URL is set (the Render deployment) -- keeps a
    free-tier instance from spinning down after ~15 minutes of no inbound
    traffic by periodically requesting its own public URL. This does NOT
    protect against data loss: Render's free tier also wipes the ephemeral
    filesystem on every redeploy/restart regardless of this loop -- see
    the Roadmap tab's "Next" section.
    """
    log.info("self_ping_started", public_url=public_url, interval_seconds=SELF_PING_INTERVAL_SECONDS)
    while True:
        time.sleep(SELF_PING_INTERVAL_SECONDS)
        try:
            requests.get(f"{public_url}/api/status", timeout=10)
        except Exception as exc:  # noqa: BLE001 -- one bad ping must not kill the loop
            log.error("self_ping_failed", error=str(exc))


def main() -> None:
    context = build_context()
    log.info("app_starting", mode=context.settings.app_mode, oanda_environment=context.settings.oanda_environment)

    if context.settings.x_bearer_token:
        thread = threading.Thread(target=_x_polling_loop, args=(context,), daemon=True)
        thread.start()
    else:
        log.info("x_api_polling_disabled", detail="X_BEARER_TOKEN not set -- using manual/webhook input only")

    if context.settings.oanda_api_token and context.settings.oanda_account_id:
        recon_thread = threading.Thread(target=_reconciliation_loop, args=(context,), daemon=True)
        recon_thread.start()
    else:
        log.info("reconciliation_loop_disabled", detail="OANDA not configured -- nothing to reconcile against")

    if context.settings.public_url:
        ping_thread = threading.Thread(target=_self_ping_loop, args=(context.settings.public_url,), daemon=True)
        ping_thread.start()

    app = create_app(context)
    # PORT is injected by cloud hosts (e.g. Render); binding 0.0.0.0 is required
    # for the container to be reachable at all. Locally this is unchanged --
    # still just http://127.0.0.1:8000 / http://localhost:8000.
    port = int(os.environ.get("PORT", 8000))
    # forwarded_allow_ips defaults to trusting only 127.0.0.1 as the immediate
    # peer before it'll rewrite request.client.host from X-Forwarded-For. On
    # Render (and most PaaS) the immediate peer is the platform's own edge
    # proxy, not 127.0.0.1 -- without this, every request looks like it comes
    # from the same IP, which turns the per-IP login lockout in app/auth.py
    # into an accidental global lockout (one bad actor locks out everyone,
    # including the real operator). "*" is safe here specifically because the
    # only way to reach this container at all is through that one trusted edge.
    uvicorn.run(app, host="0.0.0.0", port=port, forwarded_allow_ips="*")


if __name__ == "__main__":
    main()
