import pytest

from app.sources.manual_source import ManualSource, ManualSourceValidationError


def test_submit_valid_post():
    source = ManualSource()
    post = source.submit({"author": "@waltervannelli", "text": "Long EURUSD here"})
    assert post.author == "waltervannelli"  # @ stripped, lowercased
    assert post.source == "manual"
    assert post.post_id.startswith("manual-")


def test_submit_rejects_missing_author():
    source = ManualSource()
    with pytest.raises(ManualSourceValidationError):
        source.submit({"text": "no author here"})


def test_submit_rejects_missing_text():
    source = ManualSource()
    with pytest.raises(ManualSourceValidationError):
        source.submit({"author": "waltervannelli"})


def test_fetch_new_posts_drains_queue():
    source = ManualSource()
    source.submit({"author": "waltervannelli", "text": "post one"})
    source.submit({"author": "waltervannelli", "text": "post two"})
    posts = source.fetch_new_posts(since_post_id=None)
    assert len(posts) == 2
    assert source.fetch_new_posts(since_post_id=None) == []


def test_explicit_post_id_and_timestamp_are_respected():
    source = ManualSource()
    post = source.submit({"author": "waltervannelli", "text": "hi", "post_id": "manual-001", "posted_at": "2026-07-13T14:00:00Z"})
    assert post.post_id == "manual-001"
    assert post.posted_at.year == 2026
