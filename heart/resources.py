"""Read-only test-workspace view; catalogue is Scientist's public projection."""
from datetime import datetime, timezone
import html
import json
from pathlib import Path
from urllib.parse import urlsplit


def load(path):
    data = json.loads(Path(path).read_text())
    if data.get('schema_version') != 1 or not isinstance(data.get('resources'), list):
        raise ValueError('invalid public resource catalogue')
    return data


def link(url, label):
    parsed = urlsplit(str(url))
    if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password:
        return html.escape(str(label))
    return f'<a href="{html.escape(str(url), quote=True)}">{html.escape(str(label))}</a>'


def verified_ci(ci, github):
    """A reused local key cannot establish which remote produced cached runs."""
    urls = [ci.get('url')] + [entry.get('url') for entry in (ci.get('workflows') or {}).values()]
    urls = [url for url in urls if url]
    if not github or not urls:
        return {}
    for url in urls:
        parsed = urlsplit(str(url))
        parts = parsed.path.strip('/').split('/')
        if (parsed.scheme != 'https' or parsed.netloc.lower() != 'github.com'
                or len(parts) < 5 or '/'.join(parts[:2]).lower() != github.lower()
                or parts[2:4] != ['actions', 'runs'] or not parts[4].isdigit()):
            return {}
    return ci


def render(catalogue, repos, *, now=None):
    esc = lambda value: html.escape(str(value), quote=True)
    cards = []
    now = now or datetime.now(timezone.utc)
    for resource in (catalogue or {}).get('resources', []):
        if 'tests' not in resource.get('relations', []):
            continue
        name = resource.get('repo', '')
        ci = verified_ci((repos.get(name) or {}).get('ci_status') or {}, resource.get('github'))
        state = ci.get('conclusion') or ci.get('status') or 'unavailable'
        observed = ci.get('ts') or 'Unknown'
        freshness = 'Age unknown'
        try:
            age = (now - datetime.fromisoformat(observed.replace('Z', '+00:00'))).total_seconds()
            freshness = ('Evidence outdated' if age > 86400 else 'Observed within 24 hours') if age >= 0 else 'Observation time is in the future'
        except (ValueError, TypeError):
            pass
        projects = ', '.join(p['name'] for p in resource['projects'])
        sources = ' · '.join(link(l['url'], l['label']) for l in resource['links'])
        workflows = ci.get('workflows') or {}
        rows = []
        failures = []
        for workflow, evidence in workflows.items():
            status = evidence.get('conclusion') or evidence.get('status') or 'unknown'
            rows.append(f'<li>{link(evidence.get("url", ""), workflow)}: {esc(status)}</li>')
            if status in {'failure', 'timed_out', 'cancelled', 'action_required'}:
                failures.append(workflow)
        workflow_count = str(len(workflows)) if workflows else 'Not reported'
        failure_summary = ', '.join(failures) if failures else ('None' if state == 'success' else 'See run' if ci else 'Unknown')
        detail = '<ul>' + ''.join(rows) + '</ul>' if rows else ''
        cards.append(f'''<details id="resource-{esc(resource['id'])}"><summary>{esc(resource['name'])} · CI: {esc(state)} · {esc(freshness)}</summary>
<div class="test-workspace-detail"><dl>
<dt>Suite</dt><dd>{esc(projects)} — regression, smoke and parity</dd>
<dt>Tests</dt><dd>Count not reported</dd>
<dt>Workflows</dt><dd>{workflow_count}</dd>
<dt>Failing workflows</dt><dd>{esc(failure_summary)}</dd>
<dt>Updated</dt><dd>{esc(observed)}</dd>
</dl>{detail}<p>{sources} · {link(ci.get('url', ''), 'CI run')}</p></div></details>''')
    return ('<section id="test-workspaces"><h2>Test workspaces</h2>'
            + ''.join(cards) + ('' if cards else '<p>No test workspaces configured.</p>') + '</section>')
