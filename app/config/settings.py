"""Environment-backed settings plus YAML config loaders.

Secrets (API keys, tokens, account IDs) come only from environment
variables / `.env` -- never hard-coded, never committed. Everything else
(risk numbers, tracked accounts, instrument aliases) lives in version-
controlled YAML under this directory so it can be reviewed and diffed.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Literal

import yaml
from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource, SettingsConfigDict

CONFIG_DIR = Path(__file__).parent
PROJECT_ROOT = CONFIG_DIR.parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=str(PROJECT_ROOT / ".env"), extra="ignore")

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        # This project's own .env always wins over an ambient/system-level
        # environment variable of the same name (e.g. a shared OPENAI_API_KEY
        # set globally for other tools on the machine). Pydantic-settings'
        # default order does the opposite (real env vars beat .env
        # unconditionally), which silently ignored .env edits here -- this
        # tool should be self-contained regardless of what else is
        # configured on the host.
        return init_settings, dotenv_settings, env_settings, file_secret_settings

    app_mode: Literal["observe", "approval", "practice_auto"] = "observe"

    openai_api_key: str = ""
    openai_model: str = "gpt-4o-2024-08-06"

    x_bearer_token: str = ""
    x_api_base_url: str = "https://api.x.com"

    oanda_environment: Literal["practice", "live"] = "practice"
    oanda_api_token: str = ""
    oanda_account_id: str = ""

    database_url: str = "sqlite:///data/copy_trader.db"

    require_human_approval: bool = True
    # Starting value for the live, dashboard-toggleable auto-approve switch
    # (app/execution/approval_workflow.py) -- when on, a signal that already
    # passed every validation/risk check executes immediately, with no
    # human click, REGARDLESS of requires_human_review. This is a distinct,
    # stronger bypass than practice_auto's confidence threshold alone.
    # observe mode is never affected by this (it never submits orders at all).
    auto_approve_proposals: bool = False
    min_signal_confidence: float = Field(default=0.90, ge=0.0, le=1.0)
    max_signal_age_minutes: int = Field(default=10, gt=0)
    proposal_expiry_minutes: int = Field(default=5, gt=0)

    webhook_shared_secret: str = ""

    # Operator auth for a public deployment -- when blank (the local-dev
    # default), every write route is open exactly like before, no login
    # required. When set, POST/DELETE routes (other than /api/auth/login and
    # the already-independently-secured /api/posts/webhook) require a valid
    # operator session. See app/auth.py.
    operator_password: str = ""
    # Only set true behind real HTTPS (the Render deployment) -- a Secure
    # cookie is silently dropped by browsers over plain http, which would
    # break local login testing.
    secure_cookies: bool = False
    # This service's own public URL (e.g. https://x-copy-trade-tool-demo.onrender.com),
    # set only in the Render deployment. When present, main.py starts a
    # keep-alive thread that pings it periodically to avoid free-tier
    # inactivity spin-down. Absent locally, so nothing extra runs on the
    # user's machine.
    public_url: str = ""

    @field_validator("oanda_environment")
    @classmethod
    def _reject_live(cls, v: str) -> str:
        # Belt-and-braces: this version has no live-trading code path at
        # all, but fail loudly at settings-load time too rather than only
        # relying on the broker layer never implementing one.
        if v == "live":
            raise ValueError(
                "OANDA_ENVIRONMENT=live is not supported in this version -- "
                "live trading has not been designed or approved yet."
            )
        return v

    @model_validator(mode="after")
    def _practice_auto_requires_credentials(self) -> "Settings":
        if self.app_mode == "practice_auto" and not (self.oanda_api_token and self.oanda_account_id):
            raise ValueError("practice_auto mode requires OANDA_API_TOKEN and OANDA_ACCOUNT_ID to be set.")
        return self


def load_settings(env_file: str | None = None) -> Settings:
    if env_file:
        return Settings(_env_file=env_file)  # type: ignore[call-arg]
    return Settings()


def _load_yaml(path: Path) -> dict:
    if not path.exists():
        raise FileNotFoundError(f"Required config file missing: {path}")
    with path.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def load_tracked_accounts(path: Path | None = None) -> list[dict]:
    data = _load_yaml(path or (CONFIG_DIR / "tracked_accounts.yaml"))
    return data.get("tracked_accounts", [])


# Header re-written above the account list every time the dashboard's
# Accounts tab saves a change -- yaml.safe_dump has no concept of comments,
# so a per-line comment can't survive a round-trip. Keeping it as a static
# file header preserves the documentation without pretending we can.
_TRACKED_ACCOUNTS_HEADER = (
    "# Per-source-account exposure cap (DESIGN.md/Phase 7: \"max exposure per\n"
    "# source account\"). Independent of the global max_open_positions.\n"
    "# Managed by the dashboard's Accounts tab as well as by hand -- both\n"
    "# write this same file.\n"
)

_X_HANDLE_RE = re.compile(r"[A-Za-z0-9_]{1,15}")
_X_PROFILE_URL_RE = re.compile(r"^https?://(?:www\.)?(?:x|twitter)\.com/(@?[A-Za-z0-9_]{1,15})/?$")


def parse_x_handle(value: str) -> tuple[str, str]:
    """Accepts either a bare handle ("waltervannelli", "@waltervannelli") or
    a full profile URL ("https://x.com/waltervannelli") and returns
    (lowercased username, canonical profile_url) -- the same shape stored in
    tracked_accounts.yaml. Raises ValueError on anything that isn't a
    plausible X handle."""
    value = value.strip()
    match = _X_PROFILE_URL_RE.match(value)
    handle = match.group(1).lstrip("@") if match else value.lstrip("@")
    if not _X_HANDLE_RE.fullmatch(handle):
        raise ValueError(f"'{value}' doesn't look like an X handle or profile URL (e.g. https://x.com/username).")
    return handle.lower(), f"https://x.com/{handle}"


def save_tracked_accounts(accounts: list[dict], path: Path | None = None) -> None:
    target = path or (CONFIG_DIR / "tracked_accounts.yaml")
    body = yaml.safe_dump({"tracked_accounts": accounts}, sort_keys=False, default_flow_style=False)
    target.write_text(_TRACKED_ACCOUNTS_HEADER + "\n" + body, encoding="utf-8")


def add_tracked_account(
    handle_or_url: str, path: Path | None = None, max_open_positions_from_account: int = 2
) -> list[dict]:
    """Adds a new tracked account and persists it to tracked_accounts.yaml.
    Raises ValueError if the handle is malformed or already tracked."""
    username, profile_url = parse_x_handle(handle_or_url)
    accounts = load_tracked_accounts(path)
    if any(a["username"].lower() == username for a in accounts):
        raise ValueError(f"@{username} is already tracked.")
    accounts.append(
        {
            "username": username,
            "profile_url": profile_url,
            "enabled": True,
            "max_open_positions_from_account": max_open_positions_from_account,
        }
    )
    save_tracked_accounts(accounts, path)
    return accounts


def set_tracked_account_enabled(username: str, enabled: bool, path: Path | None = None) -> list[dict]:
    """Toggles an existing tracked account on/off and persists the change.
    Raises ValueError if the account isn't tracked."""
    normalized = username.lstrip("@").lower()
    accounts = load_tracked_accounts(path)
    for account in accounts:
        if account["username"].lower() == normalized:
            account["enabled"] = enabled
            save_tracked_accounts(accounts, path)
            return accounts
    raise ValueError(f"@{normalized} is not tracked.")


def remove_tracked_account(username: str, path: Path | None = None) -> list[dict]:
    """Deletes a tracked account from tracked_accounts.yaml entirely (not just
    disabling it). Raises ValueError if the account isn't tracked. Past
    posts/signals/trades already attributed to this author are untouched --
    this only stops future polling and re-tracking."""
    normalized = username.lstrip("@").lower()
    accounts = load_tracked_accounts(path)
    remaining = [a for a in accounts if a["username"].lower() != normalized]
    if len(remaining) == len(accounts):
        raise ValueError(f"@{normalized} is not tracked.")
    save_tracked_accounts(remaining, path)
    return remaining


def load_instrument_map(path: Path | None = None) -> tuple[dict[str, str], set[str]]:
    data = _load_yaml(path or (CONFIG_DIR / "instruments.yaml"))
    aliases = {k.lower(): v for k, v in data.get("aliases", {}).items()}
    known_unsupported = {s.lower() for s in data.get("known_unsupported", [])}
    return aliases, known_unsupported


def load_risk_config(path: Path | None = None) -> dict:
    return _load_yaml(path or (CONFIG_DIR / "risk.yaml"))
