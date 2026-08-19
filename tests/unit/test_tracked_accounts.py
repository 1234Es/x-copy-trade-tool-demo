import pytest

from app.config.settings import (
    add_tracked_account,
    load_tracked_accounts,
    parse_x_handle,
    remove_tracked_account,
    set_tracked_account_enabled,
)


@pytest.fixture
def accounts_path(tmp_path):
    path = tmp_path / "tracked_accounts.yaml"
    path.write_text(
        "tracked_accounts:\n"
        "  - username: waltervannelli\n"
        "    profile_url: https://x.com/waltervannelli\n"
        "    enabled: true\n"
        "    max_open_positions_from_account: 2\n",
        encoding="utf-8",
    )
    return path


@pytest.mark.parametrize(
    "value, expected_username, expected_url",
    [
        ("https://x.com/someuser", "someuser", "https://x.com/someuser"),
        ("https://twitter.com/SomeUser", "someuser", "https://x.com/SomeUser"),
        ("@SomeUser", "someuser", "https://x.com/SomeUser"),
        ("someuser", "someuser", "https://x.com/someuser"),
    ],
)
def test_parse_x_handle_accepts_urls_and_bare_handles(value, expected_username, expected_url):
    username, profile_url = parse_x_handle(value)
    assert username == expected_username
    assert profile_url == expected_url


def test_parse_x_handle_rejects_garbage():
    with pytest.raises(ValueError):
        parse_x_handle("not a handle at all")


def test_add_tracked_account_persists_and_defaults_to_enabled(accounts_path):
    accounts = add_tracked_account("https://x.com/NewTrader", path=accounts_path)

    assert accounts[-1] == {
        "username": "newtrader",
        "profile_url": "https://x.com/NewTrader",
        "enabled": True,
        "max_open_positions_from_account": 2,
    }
    # Persisted to disk, not just returned in-memory.
    assert load_tracked_accounts(accounts_path)[-1]["username"] == "newtrader"


def test_add_tracked_account_rejects_duplicate(accounts_path):
    with pytest.raises(ValueError):
        add_tracked_account("waltervannelli", path=accounts_path)


def test_set_tracked_account_enabled_toggles_and_persists(accounts_path):
    accounts = set_tracked_account_enabled("waltervannelli", False, path=accounts_path)

    assert accounts[0]["enabled"] is False
    assert load_tracked_accounts(accounts_path)[0]["enabled"] is False


def test_set_tracked_account_enabled_unknown_account_raises(accounts_path):
    with pytest.raises(ValueError):
        set_tracked_account_enabled("nosuchuser", True, path=accounts_path)


def test_remove_tracked_account_deletes_and_persists(accounts_path):
    accounts = remove_tracked_account("waltervannelli", path=accounts_path)

    assert accounts == []
    assert load_tracked_accounts(accounts_path) == []


def test_remove_tracked_account_accepts_at_prefix_and_mixed_case(accounts_path):
    accounts = remove_tracked_account("@WalterVannelli", path=accounts_path)

    assert accounts == []


def test_remove_tracked_account_leaves_other_accounts_untouched(accounts_path):
    add_tracked_account("someoneelse", path=accounts_path)

    accounts = remove_tracked_account("waltervannelli", path=accounts_path)

    assert [a["username"] for a in accounts] == ["someoneelse"]
    assert [a["username"] for a in load_tracked_accounts(accounts_path)] == ["someoneelse"]


def test_remove_tracked_account_unknown_account_raises(accounts_path):
    with pytest.raises(ValueError):
        remove_tracked_account("nosuchuser", path=accounts_path)
