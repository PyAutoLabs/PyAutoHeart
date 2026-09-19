"""Heart's checkout resolver works with the canonical family layout."""
from pathlib import Path

from heart import _workspace


def test_grouped_checkout_is_used_for_smoke_and_checks(tmp_path):
    family = tmp_path / "workspaces"
    checkout = family / "demo_workspace"
    checkout.mkdir(parents=True)
    (checkout / ".git").mkdir()
    assert _workspace.repo_path(tmp_path, "demo_workspace", required=True) == checkout
    assert checkout in _workspace.iter_checkouts(tmp_path)
