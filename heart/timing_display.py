"""Pure timing presentation. Never changes collector data or readiness policy."""

import datetime
import html
import math


def escape(value):
    return html.escape(str(value if value is not None else "unknown"))


def rows(value):
    return [r for r in value if isinstance(r, dict)] if isinstance(value, list) else []


def seconds(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return value if math.isfinite(value) and value >= 0 else None


def duration(value):
    value = seconds(value)
    return "unavailable" if value is None else f"{value:,.2f}s"


def metric(label, value):
    return f'<span>{escape(label)} <strong class="duration">{duration(value)}</strong></span>'


def date_label(value):
    try:
        return datetime.date.fromisoformat(str(value)[:10]).isoformat()
    except ValueError:
        return "unknown"


def disclosure(label, body):
    return f'<details><summary>{escape(label)}</summary>{body}</details>'


def cards(items, kind):
    if not items:
        return ""
    shown = ''.join(items[:3])
    if len(items) > 3:
        shown += disclosure(f"Show {len(items) - 3} more {kind}", ''.join(items[3:]))
    return f'<div class="timings">{shown}</div>'


def source(row, stamp=None, label="CI measurement"):
    url = str(row.get("run_url") or row.get("actions_url") or "")
    link = f' · <a href="{escape(url)}">source run ↗</a>' if url.startswith(("https://", "http://")) else ""
    error = f'<p>{escape(row["error"])}</p>' if row.get("error") else ""
    identifiers = rows(row.get("slowest"))
    full_names = ('<p>Full test identifiers</p><ul>' + ''.join(
        f'<li>{escape(test.get("nodeid"))}</li>' for test in identifiers) + '</ul>') if identifiers else ""
    return disclosure("Source and coverage", f'{escape(label)} · observed {escape(row.get("at") or stamp)}{link}{error}{full_names}')


def change(row, baseline_key="baseline_s"):
    baseline, now = seconds(row.get(baseline_key)), seconds(row.get("seconds"))
    if now is None:
        return "Unavailable / import failed; no duration measured"
    if baseline is None or baseline <= 0:
        return "Baseline building — no comparable baseline yet"
    delta = (now / baseline - 1) * 100
    return f"Baseline {duration(baseline)} · change {delta:+.0f}%"


def imports(slice_, summary):
    slice_ = slice_ if isinstance(slice_, dict) else {}
    measured = rows(slice_.get("imports"))
    ci = bool(measured)
    if not measured:
        # Older/local collectors expose only regression rows, not all imports.
        measured = [dict(r, seconds=r.get("latest_seconds"), baseline_s=r.get("baseline_seconds"), state=state)
                    for state in ("red", "yellow") for r in rows(summary.get(state))]
        measured += [{"package": p, "seconds": None} for p in summary.get("packages_unavailable", [])]
    plain, rendered = [], []
    for row in measured:
        name = str(row.get("package") or "?")
        if row.get("python"):
            name += f" · Python {row['python']}"
        status = ("Measured regression" if row.get("state") in ("red", "yellow") else
                  "Within baseline" if seconds(row.get("baseline_s")) and seconds(row.get("seconds")) is not None else
                  "No comparison")
        comparison = change(row)
        coverage = f"{row.get('samples', 'unknown')} baseline samples"
        plain.append(f"{name}: {duration(row.get('seconds'))} · {comparison} · {coverage}")
        rendered.append(f'<article class="timing-card"><h4>{escape(name)}</h4>'
                        f'<div class="timing-metrics">{metric("Current", row.get("seconds"))}</div>'
                        f'<p>{escape(comparison)}</p><p>{escape(status)} · {escape(coverage)}</p>'
                        f'{source(row, slice_.get("ts") or summary.get("ts"), "CI import probe" if ci else "Local import probe")}</article>')
    return plain, cards(rendered, "imports")


def suites(slice_):
    slice_ = slice_ if isinstance(slice_, dict) else {}
    legs = sorted(rows(slice_.get("repos")), key=lambda r: -(seconds(r.get("wall_s")) or 0))
    comparisons = {(r.get("repo"), r.get("python"), r.get("nodeid")): r for r in rows(slice_.get("tests"))}
    plain, rendered = [], []
    for leg in legs:
        name = f"{leg.get('repo', '?')} · Python {leg.get('python', '?')}"
        suite = leg.get("suite") if isinstance(leg.get("suite"), dict) else {}
        counts = f"{leg.get('tests', 'unknown')} tests · " + " · ".join(
            f"{suite.get(k, 'unknown')} {k}" for k in ("failures", "errors", "skipped"))
        cache = leg.get("cache") if isinstance(leg.get("cache"), dict) else {}
        cache_text = " · ".join(f"{k} cache {cache[k]}" for k in ("jax", "numba") if cache.get(k) in ("hit", "miss"))
        plain.append(f"{name}: wall-clock {duration(leg.get('wall_s'))} · {counts}" + (f" · {cache_text}" if cache_text else ""))
        tests = []
        for row in sorted(rows(leg.get("slowest")), key=lambda r: -(seconds(r.get("seconds")) or 0)):
            node = row.get("nodeid") or "?"
            prev = comparisons.get((leg.get("repo"), leg.get("python"), node), {})
            # Collector ratio=None means no comparable run (same run/cache change
            # included). A previous duration alone must not imply a regression.
            if seconds(prev.get("ratio")) is not None and seconds(prev.get("prev_s")) is not None:
                comparison = f"Previous {duration(prev['prev_s'])} · change {(prev['ratio'] - 1) * 100:+.0f}%"
                comparison += " · measured regression" if prev.get("state") == "warn" else " · within drift threshold"
            else:
                comparison = "No comparable previous run"
            plain.append(f"  {node}: {duration(row.get('seconds'))} · {comparison}")
            short_name = str(node).split("::")[-1]
            tests.append(f'<li><span class="test-name" title="{escape(node)}">{escape(short_name)}</span> '
                         f'<strong class="duration">{duration(row.get("seconds"))}</strong><br>{escape(comparison)}</li>')
        top = '<ol class="bottlenecks">' + ''.join(tests[:3]) + '</ol>' if tests else '<p>No per-test measurements available.</p>'
        if len(tests) > 3:
            top += disclosure(f"Show {len(tests) - 3} more measured tests", '<ol start="4" class="bottlenecks">' + ''.join(tests[3:]) + '</ol>')
        rendered.append(f'<article class="timing-card"><h4>{escape(name)}</h4>'
                        f'<div class="timing-metrics">{metric("Suite wall-clock", leg.get("wall_s"))}</div>'
                        f'<p>{escape(counts)}</p>' + (f'<p>{escape(cache_text)}</p>' if cache_text else '') +
                        '<p>Slowest tests by duration · slowness alone is not a regression.</p>' + top +
                        source(leg, slice_.get("ts"), "CI suite; parallel Python legs are shown separately") + '</article>')
    return plain, cards(rendered, "suite legs")


def chart(history, key):
    points = []
    for day in sorted(rows(history), key=lambda r: str(r.get("date") or "")):
        try:
            date = datetime.date.fromisoformat(str(day.get("date")))
        except ValueError:
            continue
        gates = day.get("gates") if isinstance(day.get("gates"), dict) else {}
        row = gates.get(key) if isinstance(gates.get(key), dict) else {}
        points.append((date, seconds(row.get("p50_s"))))
    valid = [(d, v) for d, v in points if v is not None]
    if not valid:
        return '<p>No measured daily history yet.</p>'
    low, high = min(v for _, v in valid), max(v for _, v in valid)
    start, end = points[0][0], points[-1][0]
    span = max((end - start).days, 1)
    marks, segment, last = [], [], None
    for day, value in points:
        if value is None or (last and (day - last).days > 1):
            if segment:
                marks.append('<polyline points="' + ' '.join(segment) + '"/>')
                segment = []
        last = day
        if value is None:
            continue
        x, y = 10 + 300 * (day - start).days / span, 75 - 60 * (value - low) / max(high - low, 1)
        segment.append(f"{x:.1f},{y:.1f}")
        marks.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="3"><title>{day}: {duration(value)}</title></circle>')
    if segment:
        marks.append('<polyline points="' + ' '.join(segment) + '"/>')
    label = f"{key}: daily median seconds, {start} to {end}; {len(valid)} measured days; gaps are unavailable"
    table = '<ul>' + ''.join(f'<li>{d}: {duration(v)}</li>' for d, v in points) + '</ul>'
    return (f'<figure class="timing-chart"><figcaption>{escape(label)}</figcaption>'
            f'<div class="chart-range">Range {duration(low)} – {duration(high)}</div>'
            f'<svg viewBox="0 0 320 90" role="img" aria-label="{escape(label)}"><title>{escape(label)}</title>'
            + ''.join(marks) + f'</svg><div class="chart-dates"><span>{start}</span><span>{end}</span></div>'
            + disclosure("Daily median values (text)", table) + '</figure>')


def gates(ct):
    plain, rendered = [], []
    for row in sorted(rows(ct.get("gates")), key=lambda r: -(seconds(r.get("median_s")) or 0)):
        name = f"{row.get('repo', '?')} · {row.get('workflow', '?')}"
        window = f"Window {date_label(row.get('window_from'))} → {date_label(ct.get('ts'))}"
        coverage = f"{row.get('runs_counted', 0)} completed runs · {window}"
        plain.append(f"{name}: median {duration(row.get('median_s'))} · max {duration(row.get('max_s'))} · {coverage}")
        comparison = (f"Baseline median {duration(row['baseline_s'])}" if seconds(row.get("baseline_s")) is not None else "Baseline building")
        comparison += " · measured slowdown" if row.get("state") == "warn" else ""
        rendered.append(f'<article class="timing-card"><h4>{escape(name)}</h4>'
                        f'<div class="timing-metrics">{metric("Median", row.get("median_s"))}{metric("Max", row.get("max_s"))}</div>'
                        f'<p>{escape(coverage)}</p><p>{escape(comparison)}</p>'
                        + chart(ct.get("history"), f"{row.get('repo')}/{row.get('workflow')}")
                        + source(row, ct.get("ts"), "CI workflow wall-clock") + '</article>')
    return plain, cards(rendered, "CI gates")
