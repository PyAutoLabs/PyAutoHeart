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
        workflows = []
        for workflow, evidence in (ci.get('workflows') or {}).items():
            status = evidence.get('conclusion') or evidence.get('status') or 'unknown'
            workflows.append(f'<li>{link(evidence.get("url", ""), workflow)}: {esc(status)}</li>')
        detail = ('<ul>' + ''.join(workflows) + '</ul>') if workflows else '<p>No workflow-level evidence available.</p>'
        cards.append(f'''<details id="resource-{esc(resource['id'])}"><summary>{esc(resource['name'])} · {esc(projects)} · CI: {esc(state)} · {esc(freshness)}</summary>
<p>Regression, smoke and parity checks for {esc(projects)}. Source remains in the test workspace.</p>
<p>{sources} · {link(ci.get('url', ''), 'Latest recorded CI run')}</p>
<dl><dt>Observed</dt><dd>{esc(observed)} · {esc(freshness)}</dd><dt>Source revision</dt><dd>{esc(ci.get('head_sha') or ci.get('sha') or 'Unknown')}</dd></dl>
{detail}<p>Test-level counts and coverage are not supplied by this CI snapshot. CI is separate from full release validation; these rows do not change readiness.</p>
<p>Next: use Heart's check-in to inspect this workspace's latest run, missing evidence and failures before scheduling any check.</p></details>''')
    return ('<section id="test-workspaces"><h2>Test workspaces</h2><p>Project-owned test suites and their recorded CI evidence. Inventory is not a passing result. CI without matching repository run provenance is unavailable. Evidence older than 24 hours is labelled outdated here; release gates retain their own policy.</p>'
            + ''.join(cards) + ('' if cards else '<p>No public test-workspace resources available.</p>') + '</section>')
