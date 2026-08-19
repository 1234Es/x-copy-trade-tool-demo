"""FastAPI dashboard + API (Phase 13). A minimal server-rendered page with
vanilla JS polling -- no frontend build step, appropriate for a local demo
tool. See DESIGN.md for why FastAPI was chosen over Streamlit: the
approve/reject workflow needs reliable action endpoints, which fits
FastAPI's request/response model much better than Streamlit's rerun model.
"""
from __future__ import annotations

import json
import secrets
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import Body, FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from starlette.requests import Request

from app.auth import SESSION_COOKIE_NAME
from app.config.settings import add_tracked_account, remove_tracked_account, set_tracked_account_enabled
from app.context import AppContext
from app.monitoring.health import check_oanda, check_openai, check_x_api
from app.monitoring.metrics import calculate_metrics
from app.sources.webhook_source import WebhookAuthError

TEMPLATES_DIR = Path(__file__).parent / "templates"

# Routes reachable without an operator session even when OPERATOR_PASSWORD is
# set: logging in obviously can't require already being logged in, and the
# webhook route has its own independent secret-header auth (WEBHOOK_SHARED_SECRET)
# that the browser extension already relies on -- it isn't cookie-based at all.
_AUTH_EXEMPT_PATHS = {"/api/auth/login", "/api/posts/webhook"}


def _is_operator(context: AppContext, request: Request) -> bool:
    if not context.settings.operator_password:
        return True
    return context.auth_state.is_valid(request.cookies.get(SESSION_COOKIE_NAME))


def create_app(context: AppContext) -> FastAPI:
    app = FastAPI(title="X Copy-Trade Tool")
    # Wide open on purpose: every route this CORS config applies to is either
    # a public read (GET) or protected by its own auth (the operator session
    # cookie, or the webhook's shared secret) -- CORS is not the security
    # boundary here. allow_credentials is deliberately NOT enabled, so a
    # cross-origin fetch() from another site can read public GET data but
    # can never have the operator's session cookie attached to it; SameSite=Lax
    # on that cookie blocks it independently anyway.
    app.add_middleware(
        CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"]
    )

    @app.middleware("http")
    async def require_operator_for_writes(request: Request, call_next):  # noqa: ANN001, ANN202
        if (
            context.settings.operator_password
            and request.method not in ("GET", "HEAD", "OPTIONS")
            and request.url.path not in _AUTH_EXEMPT_PATHS
            and not _is_operator(context, request)
        ):
            return JSONResponse({"detail": "Operator login required."}, status_code=401)
        return await call_next(request)

    templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

    @app.get("/", response_class=HTMLResponse)
    def landing(request: Request) -> HTMLResponse:
        return templates.TemplateResponse(request, "landing.html", {})

    @app.get("/app", response_class=HTMLResponse)
    def dashboard(request: Request) -> HTMLResponse:
        return templates.TemplateResponse(
            request,
            "dashboard.html",
            {
                "app_mode": context.settings.app_mode,
                "operator_login_enabled": bool(context.settings.operator_password),
                "operator_authenticated": _is_operator(context, request),
            },
        )

    @app.post("/api/auth/login")
    def login(request: Request, payload: dict[str, Any] = Body(...)) -> JSONResponse:
        client_ip = request.client.host if request.client else "unknown"
        now = time.time()
        if context.auth_state.is_locked_out(client_ip, now):
            raise HTTPException(status_code=429, detail="Too many failed attempts. Try again later.")
        password = payload.get("password") or ""
        if not context.settings.operator_password or not secrets.compare_digest(
            password, context.settings.operator_password
        ):
            context.auth_state.record_failed_attempt(client_ip, now)
            raise HTTPException(status_code=401, detail="Incorrect password.")
        token = context.auth_state.create_session()
        response = JSONResponse({"authenticated": True})
        response.set_cookie(
            SESSION_COOKIE_NAME,
            token,
            httponly=True,
            samesite="lax",
            secure=context.settings.secure_cookies,
            max_age=60 * 60 * 12,
        )
        return response

    @app.post("/api/auth/logout")
    def logout(request: Request) -> JSONResponse:
        context.auth_state.invalidate(request.cookies.get(SESSION_COOKIE_NAME))
        response = JSONResponse({"authenticated": False})
        response.delete_cookie(SESSION_COOKIE_NAME)
        return response

    @app.get("/api/auth/status")
    def auth_status(request: Request) -> JSONResponse:
        return JSONResponse({"authenticated": _is_operator(context, request)})

    @app.get("/api/status")
    def status() -> JSONResponse:
        now = datetime.now(timezone.utc)
        oanda_status = check_oanda(context.broker if context.settings.oanda_api_token else None)
        openai_status = check_openai(context.settings.openai_api_key)
        x_status = check_x_api(context.settings.x_bearer_token)
        return JSONResponse(
            {
                "app_mode": context.settings.app_mode,
                "connections": [_status_dict(s) for s in (oanda_status, openai_status, x_status)],
                "circuit_breaker": {
                    "tripped": context.circuit_breaker.is_tripped(now),
                    "active_reasons": [r.value for r in context.circuit_breaker.active_reasons(now)],
                    "active_trips": [
                        {
                            "reason": e.reason.value,
                            "tripped_at": e.tripped_at.isoformat(),
                            "clears_at": e.clears_at.isoformat() if e.clears_at else None,
                            "details": e.details,
                        }
                        for e in context.circuit_breaker.active_events(now)
                    ],
                },
                "instrument_cooldowns": [
                    {
                        "instrument": c["instrument"],
                        "started_at": c["started_at"].isoformat(),
                        "ends_at": c["ends_at"].isoformat(),
                    }
                    for c in context.risk_manager.active_instrument_cooldowns(now)
                ],
            }
        )

    @app.post("/api/kill-switch")
    def kill_switch() -> JSONResponse:
        now = datetime.now(timezone.utc)
        context.circuit_breaker.kill_switch(now)
        context.repository.insert_circuit_breaker_event("kill_switch", {"triggered_via": "dashboard"}, now)
        return JSONResponse({"ok": True})

    @app.post("/api/kill-switch/clear")
    def clear_kill_switch() -> JSONResponse:
        from app.risk.circuit_breaker import TripReason

        context.circuit_breaker.manual_clear(TripReason.KILL_SWITCH)
        return JSONResponse({"ok": True})

    @app.post("/api/circuit-breaker/clear")
    def clear_circuit_breaker() -> JSONResponse:
        """Clears every auto-tripped reason (API error bursts, loss limits,
        reconciliation mismatches, etc.) but deliberately leaves KILL_SWITCH
        alone -- that one is a human's explicit stop and must only be
        undone via the dedicated kill-switch clear action above, never
        bundled into a general "clear the noise" click."""
        from app.risk.circuit_breaker import TripReason

        for reason in TripReason:
            if reason != TripReason.KILL_SWITCH:
                context.circuit_breaker.manual_clear(reason)
        now = datetime.now(timezone.utc)
        return JSONResponse(
            {
                "tripped": context.circuit_breaker.is_tripped(now),
                "active_reasons": [r.value for r in context.circuit_breaker.active_reasons(now)],
            }
        )

    @app.get("/api/posts")
    def posts(limit: int = 20) -> JSONResponse:
        rows = context.repository.get_recent_posts(limit=limit)
        result = []
        for row in rows:
            classification = context.repository.get_classification(row["post_id"])
            signal = context.repository.get_signal_for_post(row["post_id"])
            result.append(
                {
                    "post": _jsonable(row),
                    "classification": _jsonable(classification) if classification else None,
                    "signal": _jsonable(signal) if signal else None,
                }
            )
        return JSONResponse(result)

    @app.post("/api/posts/manual")
    def submit_manual_post(payload: dict[str, Any] = Body(...)) -> JSONResponse:
        try:
            post = context.manual_source.submit(payload)
        except Exception as exc:  # noqa: BLE001 -- surfaced as a 400, not a crash
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        outcome = context.execution_engine.process_post(post)
        return JSONResponse(
            {
                "post_id": post.post_id,
                "status": outcome.status,
                "detail": outcome.detail,
                "signal_id": outcome.signal_id,
                "proposal_id": outcome.proposal_id,
            }
        )

    @app.post("/api/posts/webhook")
    def submit_webhook_post(
        payload: dict[str, Any] = Body(...), x_webhook_secret: str | None = Header(default=None)
    ) -> JSONResponse:
        if not context.webhook_source.enabled:
            raise HTTPException(status_code=403, detail="Webhook adapter is disabled (WEBHOOK_SHARED_SECRET not set).")
        try:
            post = context.webhook_source.ingest(payload, x_webhook_secret)
        except WebhookAuthError as exc:
            raise HTTPException(status_code=401, detail=str(exc)) from exc
        outcome = context.execution_engine.process_post(post)
        return JSONResponse({"post_id": post.post_id, "status": outcome.status, "detail": outcome.detail})

    @app.get("/api/proposals")
    def proposals() -> JSONResponse:
        now = datetime.now(timezone.utc)
        context.approval_workflow.expire_stale_proposals(now)
        pending = context.repository.get_pending_proposals()
        result = []
        for proposal in pending:
            signal = context.repository.get_signal(proposal["signal_id"])
            post = context.repository.get_raw_post(signal["post_id"]) if signal else None
            result.append({"proposal": _jsonable(proposal), "signal": _jsonable(signal) if signal else None, "post": _jsonable(post) if post else None})
        return JSONResponse(result)

    @app.post("/api/proposals/{proposal_id}/approve")
    def approve_proposal(proposal_id: str) -> JSONResponse:
        now = datetime.now(timezone.utc)
        approved = context.approval_workflow.approve(proposal_id, decided_by="dashboard_user", now=now)
        if not approved:
            raise HTTPException(status_code=409, detail="Proposal is not pending (already decided or expired).")
        outcome = context.execution_engine.execute_approved_proposal(proposal_id, now)
        return JSONResponse({"status": outcome.status, "detail": outcome.detail})

    @app.post("/api/proposals/{proposal_id}/reject")
    def reject_proposal(proposal_id: str) -> JSONResponse:
        now = datetime.now(timezone.utc)
        rejected = context.approval_workflow.reject(proposal_id, decided_by="dashboard_user", now=now)
        if not rejected:
            raise HTTPException(status_code=409, detail="Proposal is not pending (already decided or expired).")
        return JSONResponse({"status": "rejected"})

    @app.get("/api/trades/open")
    def open_trades() -> JSONResponse:
        return JSONResponse(_jsonable(context.repository.get_open_trades()))

    @app.get("/api/trades")
    def all_trades(limit: int = 200) -> JSONResponse:
        return JSONResponse(_jsonable(context.repository.get_all_trades(limit=limit)))

    @app.get("/api/settings")
    def get_settings() -> JSONResponse:
        return JSONResponse({"auto_approve_proposals": context.approval_workflow.auto_approve})

    @app.post("/api/settings/auto-approve")
    def set_auto_approve(payload: dict[str, Any] = Body(...)) -> JSONResponse:
        enabled = bool(payload.get("enabled", False))
        context.approval_workflow.set_auto_approve(enabled)
        return JSONResponse({"auto_approve_proposals": context.approval_workflow.auto_approve})

    @app.get("/api/accounts")
    def get_accounts() -> JSONResponse:
        return JSONResponse(context.tracked_accounts)

    @app.post("/api/accounts")
    def add_account(payload: dict[str, Any] = Body(...)) -> JSONResponse:
        handle = (payload.get("handle") or "").strip()
        if not handle:
            raise HTTPException(status_code=400, detail="Provide an X handle or profile URL.")
        try:
            context.tracked_accounts = add_tracked_account(handle)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return JSONResponse(context.tracked_accounts)

    @app.post("/api/accounts/{username}/toggle")
    def toggle_account(username: str, payload: dict[str, Any] = Body(...)) -> JSONResponse:
        enabled = bool(payload.get("enabled"))
        try:
            context.tracked_accounts = set_tracked_account_enabled(username, enabled)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return JSONResponse(context.tracked_accounts)

    @app.delete("/api/accounts/{username}")
    def delete_account(username: str) -> JSONResponse:
        try:
            context.tracked_accounts = remove_tracked_account(username)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return JSONResponse(context.tracked_accounts)

    @app.get("/api/metrics")
    def metrics(period: str = "today") -> JSONResponse:
        try:
            m = calculate_metrics(context.repository, period)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return JSONResponse(
            {
                "period": m.period,
                "period_start": m.period_start.isoformat() if m.period_start else None,
                "period_end": m.period_end.isoformat(),
                "trades_copied": m.trades_copied,
                "closed_trades": m.closed_trades,
                "wins": m.wins,
                "losses": m.losses,
                "win_rate": m.win_rate,
                "total_pnl": m.total_pnl,
                "open_trades": m.open_trades,
            }
        )

    @app.get("/api/pnl")
    def pnl() -> JSONResponse:
        try:
            snapshot = context.broker.get_account_snapshot()
            return JSONResponse(
                {"balance": snapshot.equity, "equity": snapshot.equity, "currency": snapshot.currency}
            )
        except Exception as exc:  # noqa: BLE001
            return JSONResponse({"error": str(exc)}, status_code=200)

    return app


def _status_dict(status: Any) -> dict[str, Any]:
    return {"name": status.name, "connected": status.connected, "detail": status.detail}


def _jsonable(value: Any) -> Any:
    """Recursively converts datetimes and nested rows to JSON-safe values."""
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_jsonable(v) for v in value]
    if isinstance(value, datetime):
        return value.isoformat()
    return value
