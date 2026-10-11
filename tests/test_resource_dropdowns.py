from datetime import datetime, timezone
from heart.resources import render
from heart.dashboard import render as dashboard

CATALOGUE = {'schema_version': 1, 'resources': [{'id': 'example-tests', 'name': 'Example tests',
    'repo': 'checks', 'github': 'Example/Checks', 'relations': ['tests'],
    'projects': [{'id': 'demo', 'name': 'Demo'}], 'links': [{'label': 'Source', 'url': 'https://github.com/Example/Checks'}]}]}


def test_unknown_is_not_pass():
    page = render(CATALOGUE, {})
    assert 'CI: unavailable' in page and 'Age unknown' in page
    assert 'resource-example-tests' in page
    assert 'Test-level counts' in page


def test_old_failure_stays_failure_and_links_run():
    ci = {'conclusion': 'failure', 'ts': '2020-01-01T00:00:00+00:00', 'head_sha': 'abcdef',
          'url': 'https://github.com/Example/Checks/actions/runs/1',
          'workflows': {'Smoke': {'conclusion': 'failure'}}}
    page = render(CATALOGUE, {'checks': {'ci_status': ci}}, now=datetime(2026, 1, 1, tzinfo=timezone.utc))
    assert 'CI: failure' in page and 'Evidence outdated' in page
    assert 'abcdef' in page and '/actions/runs/1' in page
    assert 'Smoke' in page


def test_fresh_success_and_dashboard_integration():
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    snapshot = {'ts': now.isoformat(), 'repos': {'checks': {'ci_status': {'conclusion': 'success', 'ts': now.isoformat(), 'url': 'https://github.com/Example/Checks/actions/runs/1'}}}}
    page = dashboard(snapshot, {'verdict': 'green', 'score': 100}, fmt='html', now=now, resource_catalogue=CATALOGUE)
    assert 'CI: success' in page and 'Observed within 24 hours' in page
    assert 'id="resource-example-tests"' in page
    assert 'hashchange' in page


def test_unsafe_evidence_url_is_not_linked():
    page = render(CATALOGUE, {'checks': {'ci_status': {'url': 'javascript:alert(1)'}}})
    assert 'javascript:' not in page


def test_foreign_or_unattributed_ci_is_unavailable():
    for url in ['', 'https://github.com/Other/Checks/actions/runs/1']:
        page = render(CATALOGUE, {'checks': {'ci_status': {'conclusion': 'success', 'url': url}}})
        assert 'CI: unavailable' in page
        assert 'CI: success' not in page and 'Other/Checks' not in page


def test_foreign_workflow_invalidates_aggregate():
    ci = {'conclusion': 'success', 'url': 'https://github.com/Example/Checks/actions/runs/1',
          'workflows': {'Smoke': {'conclusion': 'success', 'url': 'https://github.com/Other/Checks/actions/runs/2'}}}
    assert 'CI: unavailable' in render(CATALOGUE, {'checks': {'ci_status': ci}})
