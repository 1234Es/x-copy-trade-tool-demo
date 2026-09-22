"""Covers the auto-approve-off warning surfaced on the dashboard status bar.

Auto-approve is in-memory and resets to off on every restart. Since the model
marks nearly every signal from the tracked source as needing review, having it
off in practice_auto means almost nothing trades -- which ran unnoticed from
2026-09-13 to 2026-09-22 because nothing said so.
"""
from types import SimpleNamespace

import pytest

from app.api.server import AUTO_APPROVE_OFF_WARNING, _operational_warnings


def _context(app_mode: str, auto_approve: bool) -> SimpleNamespace:
    return SimpleNamespace(
        settings=SimpleNamespace(app_mode=app_mode),
        approval_workflow=SimpleNamespace(auto_approve=auto_approve),
    )


def test_warns_when_auto_approve_is_off_in_practice_auto():
    warnings = _operational_warnings(_context("practice_auto", auto_approve=False))
    assert warnings == [AUTO_APPROVE_OFF_WARNING]


def test_warning_names_where_to_fix_it():
    # A warning that doesn't say where the toggle is sends the operator
    # hunting -- it lives on a tab that's easy to miss.
    assert "Submit & Settings" in AUTO_APPROVE_OFF_WARNING


def test_no_warning_when_auto_approve_is_on():
    assert _operational_warnings(_context("practice_auto", auto_approve=True)) == []


@pytest.mark.parametrize("mode", ["approval", "observe"])
def test_no_warning_in_modes_where_auto_approve_off_is_expected(mode):
    # approval mode wants a click per trade; observe never trades at all.
    # Warning in either would be noise the operator learns to ignore.
    assert _operational_warnings(_context(mode, auto_approve=False)) == []
