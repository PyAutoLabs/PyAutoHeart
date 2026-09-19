"""URL audit accepts the default cwd and relative checkout paths."""
from heart.checks import url_check_live


def test_default_dot_is_scanned_without_a_repository_identity(tmp_path, monkeypatch):
    seen = []
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(url_check_live, "scan_repos", lambda paths: seen.extend(paths) or {})
    assert url_check_live.main(["--no-check"]) == 0
    assert seen == [tmp_path]


def test_relative_path_is_kept_relative_to_root(tmp_path, monkeypatch):
    seen = []
    child = tmp_path / "local" / "scripts"
    child.mkdir(parents=True)
    monkeypatch.setattr(url_check_live, "scan_repos", lambda paths: seen.extend(paths) or {})
    assert url_check_live.main(["--root", str(tmp_path), "--repos", "local/scripts", "--no-check"]) == 0
    assert seen == [child]
