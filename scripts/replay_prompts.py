"""Replay stored posts through the current prompts and report what changed.

Run this before deploying any change to app/nlp/prompts.py. It re-sends past
posts to the classifier and extractor and lists every decision that would
now come out differently, so a change that fixes one phrasing can't
silently break another. See app/nlp/replay.py for exactly what is and isn't
replayed.

It costs money (real OpenAI calls), so by default it only reports how many
calls it WOULD make. Pass --run to actually replay.

    python -m scripts.replay_prompts                      # estimate only
    python -m scripts.replay_prompts --since 2026-09-01   # narrow it down
    python -m scripts.replay_prompts --run                # spend, replay, report

Input is a JSON list in the same shape /api/posts returns ({post,
classification, signal} per item), by default data/replay/history.json.
--from-url pulls a fresh set from a running deployment and saves it there
first, so building up a replay set needs no database credentials. Replay
sets contain third-party posts and live in data/replay/, which is
gitignored.

Nothing is written to any database: the only outputs are a markdown report
in data/replay/ and a summary on screen.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import requests

from app.config.settings import load_risk_config, load_settings, load_tracked_accounts
from app.nlp.classifier import Classifier
from app.nlp.context_engine import ContextEngine
from app.nlp.openai_client import OpenAIClient
from app.nlp.replay import estimate_calls, needs_openai, render_report, replay
from app.nlp.signal_extractor import SignalExtractor

REPLAY_DIR = Path(__file__).parent.parent / "data" / "replay"
DEFAULT_INPUT = REPLAY_DIR / "history.json"


def _fetch(url: str) -> Path:
    response = requests.get(url, timeout=60)
    response.raise_for_status()
    REPLAY_DIR.mkdir(parents=True, exist_ok=True)
    out = REPLAY_DIR / f"fetched-{datetime.now(timezone.utc):%Y%m%d-%H%M%S}.json"
    out.write_text(json.dumps(response.json(), ensure_ascii=False, indent=1), encoding="utf-8")
    return out


def _select(items: list[dict], authors: set[str] | None, since: str | None, limit: int | None) -> list[dict]:
    chosen = [
        i for i in items
        if (authors is None or i["post"]["author"] in authors)
        and (since is None or i["post"]["posted_at"] >= since)
    ]
    chosen.sort(key=lambda i: i["post"]["posted_at"])
    return chosen[-limit:] if limit else chosen


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT, help="Replay set (default: %(default)s)")
    parser.add_argument("--from-url", help="Fetch a replay set first, e.g. https://<host>/api/posts?limit=500")
    parser.add_argument("--author", action="append",
                        help="Only replay this author (repeatable). Default: currently tracked accounts.")
    parser.add_argument("--since", help="Only posts on or after this date, e.g. 2026-09-01")
    parser.add_argument("--limit", type=int, help="Only the most recent N posts")
    parser.add_argument("--run", action="store_true", help="Actually replay (makes paid OpenAI calls)")
    args = parser.parse_args()

    path = _fetch(args.from_url) if args.from_url else args.input
    if not path.exists():
        print(f"No replay set at {path}. Use --from-url to fetch one.", file=sys.stderr)
        return 1
    all_items = json.loads(path.read_text(encoding="utf-8"))

    authors = set(args.author) if args.author else {a["username"] for a in load_tracked_accounts() if a.get("enabled")}
    items = _select(all_items, authors, args.since, args.limit)

    print(f"Replay set: {path} ({len(all_items)} posts)")
    print(f"Selected:   {len(items)} posts from {', '.join(sorted(authors))}"
          + (f" since {args.since}" if args.since else "") + (f", last {args.limit}" if args.limit else ""))
    print(f"            {sum(1 for i in items if needs_openai(i))} reach the model; "
          f"the rest are filtered locally at no cost")
    print(f"Estimate:   ~{estimate_calls(items)} OpenAI calls")
    if not args.run:
        print("\nNothing sent. Re-run with --run to replay.")
        return 0
    if not items:
        print("Nothing selected.")
        return 0

    settings = load_settings()
    if not settings.openai_api_key:
        print("OPENAI_API_KEY is not set.", file=sys.stderr)
        return 1
    client = OpenAIClient(api_key=settings.openai_api_key, model=settings.openai_model)
    extractor = SignalExtractor(
        client,
        ContextEngine(repository=None),  # only its static prompt formatting is used
        settings.min_signal_confidence,
        missing_stop_loss_behavior=load_risk_config().get("missing_stop_loss_behavior", "human_review"),
    )
    print(f"\nReplaying against {settings.openai_model}...")
    result = replay(items, Classifier(client), extractor)

    REPLAY_DIR.mkdir(parents=True, exist_ok=True)
    report = REPLAY_DIR / f"report-{datetime.now(timezone.utc):%Y%m%d-%H%M%S}.md"
    report.write_text(render_report(result, str(path)), encoding="utf-8")

    print(f"\n{len(result.changed)} changed, {result.unchanged} unchanged"
          + (f", {len(result.errors)} errored" if result.errors else ""))
    for outcome in result.changed[:10]:
        fields = ", ".join(f"{c.field}: {c.before} -> {c.after}" for c in outcome.changes)
        print(f"  {outcome.posted_at[:16]}  {fields}")
    if len(result.changed) > 10:
        print(f"  ...and {len(result.changed) - 10} more")
    print(f"\nFull report: {report}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
