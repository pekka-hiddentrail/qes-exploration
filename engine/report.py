"""Renders a run's output.json (+ bugs.json, if present) as a single
self-contained HTML file - everything the JSON contains, laid out for a
human to actually read checkpoint by checkpoint, instead of scrolling raw
JSON. ~70% of this was already identical across every prior experiment's
report.py (markdown-lite prose rendering, badges, CSS, page/checkpoint
structure) - that part lives here, generic and adapter-agnostic. The
genuinely per-SUT parts (how to render one test entry, how to render the
onboarding/schema section) are supplied by the adapter.

esc/inline_markdown/render_prose/badge/bool_badge/verdict_badge/
render_json_block are public so adapter render_test_entry/
render_onboarding_section implementations can reuse them.
"""

import html
import json
import re
from pathlib import Path

from engine.adapter import SUTAdapter


def esc(value) -> str:
    if value is None:
        return ""
    return html.escape(str(value))


_BOLD_RE = re.compile(r"\*\*(.+?)\*\*")
_CODE_RE = re.compile(r"`(.+?)`")
_HEADER_RE = re.compile(r"^(#{1,6})\s+(.*)")
_BULLET_RE = re.compile(r"^[-*]\s+(.*)")


def inline_markdown(text: str) -> str:
    escaped = esc(text)
    escaped = _BOLD_RE.sub(r"<strong>\1</strong>", escaped)
    escaped = _CODE_RE.sub(r"<code>\1</code>", escaped)
    return escaped


def render_prose(text) -> str:
    """The Driver and Skeptic write markdown-flavored prose (headers, **bold**,
    `code`, bullet lists) in every free-text field - this is a small, deliberately
    narrow markdown->HTML pass covering just what these fields actually contain,
    not a general markdown parser."""
    if not text:
        return ""
    html_parts = []
    list_buffer = []
    paragraph_buffer = []

    def flush_list():
        if list_buffer:
            html_parts.append("<ul>" + "".join(f"<li>{item}</li>" for item in list_buffer) + "</ul>")
            list_buffer.clear()

    def flush_paragraph():
        if paragraph_buffer:
            html_parts.append(f"<p>{' '.join(paragraph_buffer)}</p>")
            paragraph_buffer.clear()

    for raw_line in str(text).strip().split("\n"):
        line = raw_line.strip()
        if not line:
            flush_paragraph()
            flush_list()
            continue

        header_match = _HEADER_RE.match(line)
        if header_match:
            flush_paragraph()
            flush_list()
            level = min(len(header_match.group(1)) + 3, 6)
            html_parts.append(f"<h{level}>{inline_markdown(header_match.group(2))}</h{level}>")
            continue

        bullet_match = _BULLET_RE.match(line)
        if bullet_match:
            flush_paragraph()
            list_buffer.append(inline_markdown(bullet_match.group(1)))
            continue

        flush_list()
        paragraph_buffer.append(inline_markdown(line))

    flush_paragraph()
    flush_list()
    return "".join(html_parts)


def badge(text, kind) -> str:
    return f'<span class="badge badge-{kind}">{esc(text)}</span>'


def bool_badge(value, true_label="yes", false_label="no") -> str:
    if value is True:
        return badge(true_label, "good")
    if value is False:
        return badge(false_label, "bad")
    return badge("unknown", "warn")


def verdict_badge(verdict) -> str:
    kind = {
        "corroborated": "good",
        "inconclusive": "warn",
        "strong_enough": "good",
        "weak": "bad",
    }.get(verdict, "warn")
    return badge(verdict.replace("_", " ") if verdict else "unknown", kind)


def render_json_block(data) -> str:
    return f'<pre class="payload">{esc(json.dumps(data, ensure_ascii=False))}</pre>'


def _render_checkpoint_conclusion(checkpoint_entry) -> str:
    """Every checkpoint forms a hypothesis (behavior + any anomalies noticed) and
    gets a cold Skeptic review of it. A "weak" verdict is what sends the process
    into another checkpoint; "strong_enough" is what ends it. Fully generic - the
    hypothesis/Skeptic schema is the same for every adapter."""
    hypothesis = checkpoint_entry["hypothesis"]
    skeptic = checkpoint_entry["skeptic_review"]
    anomalies = hypothesis.get("anomalies", [])
    untested = "".join(f"<li>{inline_markdown(a)}</li>" for a in hypothesis.get("untested_areas", []))
    gaps = "".join(f"<li>{inline_markdown(g)}</li>" for g in skeptic.get("gaps", []))
    next_tests = "".join(f"<li>{inline_markdown(t)}</li>" for t in skeptic.get("recommended_next_tests", []))

    prior_gaps_html = ""
    prior_gaps_response = hypothesis.get("prior_gaps_response", [])
    if prior_gaps_response:
        prior_gaps_items = "".join(f"<li>{inline_markdown(g)}</li>" for g in prior_gaps_response)
        prior_gaps_html = f"""
        <p><strong>Driver's response to the prior checkpoint's named gaps</strong></p>
        <ul>{prior_gaps_items}</ul>
        """

    prior_critique_html = ""
    prior_critique = skeptic.get("prior_critique_addressed")
    if prior_critique and prior_critique.strip().lower() != "n/a":
        prior_critique_html = f"""
        <p><strong>Was the Skeptic's own prior critique addressed?</strong></p>
        <div class="prose">{render_prose(prior_critique)}</div>
        """

    anomalies_html = ""
    if anomalies:
        anomaly_items = "".join(f"<li>{inline_markdown(a)}</li>" for a in anomalies)
        anomalies_html = f"""
        <p><strong>Anomalies noticed ({len(anomalies)})</strong></p>
        <ul>{anomaly_items}</ul>
        """
    else:
        anomalies_html = '<p class="prose-muted">No anomalies claimed this checkpoint.</p>'

    return f"""
    <div class="exhibit">
      <p class="eyebrow">Checkpoint {checkpoint_entry['checkpoint']} hypothesis</p>
      <h4>Observed behavior</h4>
      <div class="prose">{render_prose(hypothesis.get('observed_behavior'))}</div>
      {anomalies_html}
      <p><strong>Untested areas named by the Driver</strong></p>
      <ul>{untested}</ul>
      {prior_gaps_html}
      <h4>Skeptic review {verdict_badge(skeptic.get('verdict'))}</h4>
      <p><strong>Gaps identified</strong></p>
      <ul>{gaps}</ul>
      <p><strong>Coverage breadth check</strong></p>
      <div class="prose">{render_prose(skeptic.get('coverage_breadth_check'))}</div>
      <p><strong>Inference validity check</strong></p>
      <div class="prose">{render_prose(skeptic.get('inference_validity_check'))}</div>
      <p><strong>Anomaly critique</strong></p>
      <div class="prose">{render_prose(skeptic.get('anomaly_critique'))}</div>
      <p><strong>Recommended next tests</strong></p>
      <ul>{next_tests}</ul>
      {prior_critique_html}
      <div class="prose prose-muted">{render_prose(skeptic.get('reasoning'))}</div>
    </div>
    """


def _render_casting_section(casting_log, checkpoints, render_test_entry) -> str:
    by_checkpoint = {}
    for entry in casting_log:
        by_checkpoint.setdefault(entry["checkpoint"], {}).setdefault(entry["round"], []).append(entry)

    checkpoint_by_num = {c["checkpoint"]: c for c in checkpoints}

    # A checkpoint whose Driver gives up on its first round proposes zero tests, so
    # it never contributes anything to casting_log - but it still gets a hypothesis
    # + Skeptic review. Iterating only over by_checkpoint's keys would silently drop
    # that checkpoint's conclusion entirely, even though it's real, generated data.
    all_checkpoint_nums = sorted(set(by_checkpoint) | set(checkpoint_by_num))

    parts = []
    for checkpoint_num in all_checkpoint_nums:
        rounds = by_checkpoint.get(checkpoint_num, {})
        round_html = []
        for round_num in sorted(rounds):
            entries = rounds[round_num]
            reasoning = entries[0].get("round_reasoning", "")
            tests_html = "".join(render_test_entry(e) for e in entries)
            round_html.append(f"""
            <div class="round">
              <p class="eyebrow">Round {round_num}</p>
              <details class="reasoning" open>
                <summary>Reasoning</summary>
                <div class="prose">{render_prose(reasoning)}</div>
              </details>
              <div class="test-grid">{tests_html}</div>
            </div>
            """)
        if not rounds:
            round_html.append('<p class="prose-muted">No rounds executed - the Driver gave up immediately at the start of this checkpoint.</p>')

        conclusion_html = ""
        checkpoint_entry = checkpoint_by_num.get(checkpoint_num)
        if checkpoint_entry:
            conclusion_html = _render_checkpoint_conclusion(checkpoint_entry)

        parts.append(f"""
        <div class="checkpoint">
          <h3>Checkpoint {checkpoint_num}</h3>
          {''.join(round_html)}
          {conclusion_html}
        </div>
        """)
    return "".join(parts)


def _render_one_bug_report(bug_report) -> str:
    steps = "".join(f"<li>{inline_markdown(s)}</li>" for s in bug_report.get("steps_to_reproduce", []))
    severity = bug_report.get("severity")
    status = bug_report.get("status")
    return f"""
    <div class="exhibit exhibit-final">
      <h3>{esc(bug_report.get('title'))}</h3>
      <p>{badge(severity, 'bad' if severity == 'high' else 'warn')}
         {badge(status, 'good' if status == 'corroborated' else 'warn')}</p>
      <div class="prose">{render_prose(bug_report.get('description'))}</div>
      <p><strong>Steps to reproduce</strong></p>
      <ol>{steps}</ol>
      <p><strong>Expected behavior</strong></p>
      <div class="prose">{render_prose(bug_report.get('expected_behavior'))}</div>
      <p><strong>Actual behavior</strong></p>
      <div class="prose">{render_prose(bug_report.get('actual_behavior'))}</div>
      <div class="caveats">
        <p><strong>Caveats</strong></p>
        <div class="prose">{render_prose(bug_report.get('caveats'))}</div>
      </div>
    </div>
    """


def _render_bug_report_section(bug_reports) -> str:
    if not bug_reports:
        return ""
    heading = "Bug report" if len(bug_reports) == 1 else f"Bug reports ({len(bug_reports)})"
    reports_html = "".join(_render_one_bug_report(b) for b in bug_reports)
    return f"""
    <section id="bug-report">
      <p class="eyebrow">Final checkpoint conclusion</p>
      <h2>{heading}</h2>
      {reports_html}
    </section>
    """


_CSS = """
:root {
  --ink: #17262b;
  --ink-soft: #45575d;
  --paper: #eef2f0;
  --panel: #ffffff;
  --line: rgba(23, 38, 43, 0.14);
  --accent: #0e6e76;
  --good-bg: #dcece1; --good-fg: #205c33;
  --bad-bg: #f6dcd8;  --bad-fg: #8c2c22;
  --warn-bg: #f1e6cd; --warn-fg: #7a5510;
  --code-bg: #17262b; --code-fg: #d9e6e3;
  --font-display: "Iowan Old Style", "Palatino Linotype", Palatino, Georgia, serif;
  --font-body: -apple-system, "Segoe UI", "Helvetica Neue", Arial, sans-serif;
  --font-mono: "SF Mono", "Cascadia Code", "Roboto Mono", Consolas, monospace;
}

* { box-sizing: border-box; }

body {
  font-family: var(--font-body);
  background: var(--paper);
  color: var(--ink);
  margin: 0;
  line-height: 1.55;
  font-variant-numeric: tabular-nums;
}

.wrap { max-width: 880px; margin: 0 auto; padding: 0 1.5rem 4rem; }

.topbar {
  position: sticky; top: 0; z-index: 10;
  background: rgba(238, 242, 240, 0.92);
  backdrop-filter: blur(6px);
  border-bottom: 1px solid var(--line);
}
.topbar-inner {
  max-width: 880px; margin: 0 auto; padding: 0.85rem 1.5rem;
  display: flex; align-items: center; justify-content: space-between; gap: 1rem;
  flex-wrap: wrap;
}
.topbar-title { font-family: var(--font-display); font-size: 1.05rem; font-weight: 600; }
.topbar-nav { display: flex; gap: 1.25rem; list-style: none; margin: 0; padding: 0; font-size: 0.85rem; }
.topbar-nav a {
  color: var(--ink-soft); text-decoration: none; border-bottom: 1px solid transparent;
}
.topbar-nav a:hover, .topbar-nav a:focus-visible {
  color: var(--accent); border-bottom-color: var(--accent);
}
@media (prefers-reduced-motion: no-preference) {
  .topbar-nav a { transition: color 120ms ease, border-color 120ms ease; }
}

.hero { padding: 3rem 0 1.5rem; }
.hero h1 {
  font-family: var(--font-display); font-size: 2.1rem; font-weight: 600;
  margin: 0.3rem 0 1rem; text-wrap: balance;
}
.hero .eyebrow { margin-bottom: 0; }
.stat-row {
  display: flex; flex-wrap: wrap; gap: 1.75rem; padding: 1rem 1.25rem;
  background: var(--panel); border: 1px solid var(--line); border-radius: 6px;
}
.stat { display: flex; flex-direction: column; gap: 0.15rem; }
.stat .num { font-family: var(--font-mono); font-size: 1.3rem; font-weight: 600; }
.stat .label { font-size: 0.75rem; color: var(--ink-soft); text-transform: uppercase; letter-spacing: 0.05em; }

.eyebrow {
  font-size: 0.75rem; text-transform: uppercase; letter-spacing: 0.08em;
  color: var(--accent); font-weight: 600; margin: 0 0 0.4rem;
}

section { margin-top: 3rem; }
h2 { font-family: var(--font-display); font-size: 1.5rem; margin: 0 0 1.25rem; text-wrap: balance; }
h3 { font-family: var(--font-display); font-size: 1.2rem; margin: 1.5rem 0 0.5rem; }
h4 { font-size: 1rem; margin: 1.25rem 0 0.4rem; }

.checkpoint { margin: 1.75rem 0; }
.round { margin: 1.25rem 0 1.75rem; }

details.reasoning { margin: 0.5rem 0 1rem; }
details.reasoning summary {
  cursor: pointer; font-size: 0.8rem; color: var(--ink-soft);
  text-transform: uppercase; letter-spacing: 0.04em; font-weight: 600;
}
details.reasoning summary:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
details.reasoning .prose {
  color: var(--ink-soft); margin: 0.6rem 0 0;
  border-left: 2px solid var(--line); padding-left: 0.85rem;
}

.test-grid { display: flex; flex-direction: column; gap: 0.6rem; }
.test {
  background: var(--panel); border: 1px solid var(--line); border-radius: 4px;
  padding: 0.85rem 1rem;
}
.test-hypothesis { font-size: 0.92rem; margin-bottom: 0.5rem; }
.account-list { list-style: none; margin: 0.5rem 0 0; padding: 0; display: flex; flex-direction: column; gap: 0.5rem; }
.test-number {
  font-family: var(--font-mono); font-size: 0.72rem; color: var(--ink-soft);
  background: var(--paper); border: 1px solid var(--line); border-radius: 4px;
  padding: 0.05rem 0.4rem; margin-right: 0.5rem;
}
.probe-label { font-style: italic; color: var(--ink-soft); }
.payload, .schema-doc {
  font-family: var(--font-mono); font-size: 0.82rem; background: var(--code-bg);
  color: var(--code-fg); border-radius: 4px; padding: 0.6rem 0.75rem; margin: 0.4rem 0;
  white-space: pre-wrap; word-break: break-word; overflow-x: auto;
}
.test-predicted, .test-outcome { font-size: 0.88rem; margin-top: 0.35rem; }
.sep { color: var(--line); margin: 0 0.15rem; }

.exhibit {
  background: var(--panel); border: 1px solid var(--line); border-radius: 6px;
  padding: 1.25rem 1.5rem; margin: 1.25rem 0;
}
.exhibit-final { border-color: var(--accent); border-width: 1px; }

.prose { font-size: 0.94rem; }
.prose p { margin: 0.6rem 0; }
.prose p:first-child { margin-top: 0; }
.prose p:last-child { margin-bottom: 0; }
.prose h4, .prose h5, .prose h6 {
  font-family: var(--font-body); text-transform: uppercase; letter-spacing: 0.03em;
  color: var(--ink-soft); font-size: 0.78rem; margin: 1rem 0 0.35rem;
}
.prose ul { margin: 0.4rem 0; padding-left: 1.2rem; }
.prose li { margin: 0.2rem 0; }
.prose code {
  font-family: var(--font-mono); font-size: 0.85em; background: var(--paper);
  border: 1px solid var(--line); border-radius: 3px; padding: 0.05rem 0.3rem;
}
.prose-muted { color: var(--ink-soft); font-size: 0.92rem; }
.caveats {
  margin-top: 1rem; padding-top: 1rem; border-top: 1px solid var(--line);
  font-size: 0.92rem; color: var(--ink-soft);
}

.badge {
  display: inline-block; padding: 0.12rem 0.55rem; border-radius: 999px;
  font-size: 0.72rem; font-weight: 600; letter-spacing: 0.02em;
}
.badge-good { background: var(--good-bg); color: var(--good-fg); }
.badge-bad { background: var(--bad-bg); color: var(--bad-fg); }
.badge-warn { background: var(--warn-bg); color: var(--warn-fg); }

a:focus-visible, button:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
"""


def _stat(value, label) -> str:
    return f'<div class="stat"><span class="num">{esc(value)}</span><span class="label">{esc(label)}</span></div>'


def render_report(output: dict, bug_reports: list | None, adapter: SUTAdapter) -> str:
    bug_reports = bug_reports or []
    api_schema = output.get("api_schema", "")
    onboarding_extra = output.get("onboarding_extra", {})
    happy_day_example = output.get("happy_day_example", {})
    casting_log = output.get("casting_log", [])
    checkpoints = output.get("checkpoints", [])
    checkpoints_run = len({e["checkpoint"] for e in casting_log} | {c["checkpoint"] for c in checkpoints})

    if output.get("error"):
        eyebrow, title = "Run incomplete", "Stopped early"
        stats = [_stat(output["error"][:40] + ("..." if len(output["error"]) > 40 else ""), "reason")]
    elif output.get("anomaly_found"):
        eyebrow = "Anomaly found"
        title = bug_reports[0]["title"] if len(bug_reports) == 1 else f"{len(bug_reports)} anomalies found"
        stats = [
            _stat(checkpoints_run, "checkpoints run"),
            _stat(len(casting_log), "tests executed"),
            _stat(len(bug_reports), "bugs reported"),
        ]
    else:
        reason = output.get("stopped_reason", "unknown")
        eyebrow, title = "Checkpoints concluded", "No anomaly found"
        stats = [
            _stat(checkpoints_run, "checkpoints run"),
            _stat(len(casting_log), "tests executed"),
            _stat(reason.replace("_", " "), "stopped because"),
        ]

    nav_items = [("#schema", "Schema")]
    if casting_log or checkpoints:
        nav_items.append(("#casting", "Checkpoints"))
    if bug_reports:
        nav_items.append(("#bug-report", "Bug report"))
    nav_html = "".join(f'<li><a href="{href}">{label}</a></li>' for href, label in nav_items)

    report_title = adapter.report_title or f"{adapter.display_name} Investigation Report"
    onboarding_html = adapter.render_onboarding_section(api_schema, onboarding_extra, happy_day_example)
    casting_html = _render_casting_section(casting_log, checkpoints, adapter.render_test_entry)

    return f"""<!doctype html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{esc(report_title)}</title>
<style>{_CSS}</style>
</head>
<body>
<div class="topbar">
  <div class="topbar-inner">
    <span class="topbar-title">{esc(adapter.display_name)} Report</span>
    <ul class="topbar-nav">{nav_html}</ul>
  </div>
</div>

<div class="wrap">
  <div class="hero">
    <p class="eyebrow">{eyebrow}</p>
    <h1>{esc(title)}</h1>
    <div class="stat-row">{''.join(stats)}</div>
  </div>

  <section id="schema">
    <p class="eyebrow">Onboarding</p>
    <h2>Schema &amp; happy-day example</h2>
    {onboarding_html}
  </section>

  <section id="casting">
    <p class="eyebrow">Checkpoint loop</p>
    <h2>Checkpoints</h2>
    {casting_html}
  </section>

  {_render_bug_report_section(bug_reports)}
</div>
</body>
</html>
"""


def render_report_from_dir(results_dir: Path, adapter: SUTAdapter) -> str:
    output = json.loads((results_dir / "output.json").read_text(encoding="utf-8"))
    bugs_path = results_dir / "bugs.json"
    bug_reports = json.loads(bugs_path.read_text(encoding="utf-8")) if bugs_path.exists() else []
    return render_report(output, bug_reports, adapter)
