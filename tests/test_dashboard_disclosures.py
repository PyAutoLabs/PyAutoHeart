"""The overview stays compact while every observation remains reachable."""
from html.parser import HTMLParser

from heart import dashboard
from tests.test_dashboard import (
    FRESH_NOW, _legacy_import, _lib, _unit_timings_slice,
    make_snapshot, make_verdict,
)


class Node:
    def __init__(self, tag='', attrs=()):
        self.tag, self.attrs, self.children = tag, dict(attrs), []

    def find(self, tag=None, cls=None):
        for child in self.children:
            if isinstance(child, Node):
                if ((tag is None or child.tag == tag) and
                        (cls is None or cls in child.attrs.get('class', '').split())):
                    yield child
                yield from child.find(tag, cls)

    def text(self):
        return ''.join(c.text() if isinstance(c, Node) else c for c in self.children)


class Page(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.root = Node()
        self.stack = [self.root]
        self.feed(html)
        assert len(self.stack) == 1, 'Unclosed HTML elements'

    def handle_starttag(self, tag, attrs):
        node = Node(tag, attrs)
        self.stack[-1].children.append(node)
        if tag not in {'meta', 'br', 'hr', 'input', 'img', 'link'}:
            self.stack.append(node)

    def handle_endtag(self, tag):
        assert self.stack[-1].tag == tag, (self.stack[-1].tag, tag)
        self.stack.pop()

    def handle_data(self, data):
        self.stack[-1].children.append(data)


def test_all_categories_start_collapsed_with_summary_and_separate_named_icons():
    board = dashboard.build_board(make_snapshot(), make_verdict(), now=FRESH_NOW)
    # Include every severity, an empty category, and both clipboard types.
    board.sections = [dashboard.Section(
        f'category-{state}', f'Category {state}', state, 'A short state summary.',
        action={'payload': 'pyauto-heart fix drift', 'label': 'Inspect drift'}
        if state == dashboard.WARN else None,
    ) for state in (dashboard.OK, dashboard.WARN, dashboard.FAIL,
                    dashboard.INFO, dashboard.UNOBS)]
    root = Page(dashboard._render_html(board)).root
    rows = list(root.find(cls='check-row'))
    assert len(rows) == len(board.sections)
    for row, section in zip(rows, board.sections):
        check = next(row.find('details', 'check'))
        assert 'open' not in check.attrs
        summary = next(check.find('summary'))
        assert section.title in summary.text() and section.summary in summary.text()
        assert not list(summary.find('button')) and not list(summary.find('a'))
        actions = next(row.find(cls='row-actions'))
        buttons = list(actions.find('button'))
        assert buttons
        for button in buttons:
            assert button.attrs['aria-label'] == button.attrs['title']
            assert button.attrs['data-cmd']
            assert button.text() == ''  # icons, not visible word labels
            svg = next(button.find('svg'))
            assert svg.attrs['aria-hidden'] == 'true'
        assert not list(check.find(cls='row-actions'))


def test_libraries_reveal_all_six_compact_repo_lines_and_observation_sources():
    snapshot = make_snapshot()
    snapshot['repos']['FixtureLibrary'] = _lib('failure')
    board = dashboard.build_board(snapshot, make_verdict(), now=FRESH_NOW,
                                  unobserved=('repo_state',))
    root = Page(dashboard._render_html(board)).root
    check = next(n for n in root.find('details', 'check')
                 if n.attrs['id'] == 'check-libraries')
    lines = list(next(check.find(cls='repo-lines')).find('li'))
    assert len(lines) == 6
    for line in lines:
        assert 'CI' in line.text() and 'repo state n/a here' in line.text()
    assert any('FixtureLibrary' in line.text() for line in lines)
    entries = list(check.find('li', 'entry'))
    assert len(entries) == len(next(s for s in board.sections if s.key == 'libraries').entries)
    assert 'Observation source' in check.text()


def test_timing_cards_are_inside_category_and_checks_precede_score_actions_gaps():
    board = dashboard.build_board(
        make_snapshot(unit_timings=_unit_timings_slice(), import_time=_legacy_import()),
        dict(make_verdict('stale'), stale_reasons=['install verification not run'],
             stale_details=[{'key': 'install_unknown'}]), now=FRESH_NOW)
    html = dashboard._render_html(board)
    root = Page(html).root
    checks = list(root.find('details', 'check'))
    timing = next(c for c in checks if c.attrs['id'] == 'check-import_time')
    assert 'open' not in timing.attrs
    assert list(timing.find(cls='timing-card'))
    assert all('timing-card' not in n.attrs.get('class', '')
               for c in checks for n in next(c.find('summary')).find())
    assert (html.index('<div class="board">') < html.index('Why this score:') <
            html.index('Fix Heart systematically') < html.index('Evidence gaps ('))
    assert '[these guys are giving me life]' not in html


def test_icon_copy_payloads_round_trip_quotes_newlines_and_markup():
    payload = '/health Inspect "repo"\nKeep <evidence> & don\'t execute it.'
    for face in ('copy command', 'copy prompt'):
        root = Page(dashboard._copy_btn(payload, 'Inspect "repo"', face, icon=True)).root
        button = next(root.find('button'))
        assert button.attrs['data-cmd'] == payload
        assert button.attrs['aria-label'].startswith(face + ': ')
        assert button.attrs['title'].endswith('Inspect "repo"')
