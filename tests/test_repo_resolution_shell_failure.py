"""A failing path resolver must not become an absent/clean repository."""
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[1]


def test_repo_state_propagates_background_worker_failure():
    result = subprocess.run(['bash', '-c', '''
source "$1/heart/checks/repo_state.sh"
heart_state_dir() { :; }
heart_log() { :; }
load_repos_yaml() { printf 'Example/Demo workspace\\n'; }
heart_repo_path() { return 2; }
check_repo_state_all
''', 'test', str(ROOT)], capture_output=True, text=True)
    assert result.returncode != 0


def test_url_sweep_rejects_unresolved_checkout():
    result = subprocess.run(['bash', '-c', '''
source "$1/heart/checks/url_sweep.sh"
heart_state_dir() { :; }
heart_log() { :; }
URL_CHECK_REPOS=(Demo)
heart_repo_path() { return 2; }
check_url_sweep
''', 'test', str(ROOT)], capture_output=True, text=True)
    assert result.returncode != 0
