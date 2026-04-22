#!/usr/bin/env python3
"""Aggregate commit counts across a GitHub organization and render an SVG."""
from __future__ import annotations

import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from datetime import datetime, timezone
from html import escape
from pathlib import Path
from typing import Iterator

ORG = os.environ["ORG"]
TOKEN = os.environ["GH_TOKEN"]
OUTPUT = Path(os.environ.get("OUTPUT", "assets/commits.svg"))
TOP_N = int(os.environ.get("TOP_N", "6"))

# Generic author emails that GitHub can misattribute to an unrelated
# public user. Commits with these emails get bucketed under "unknown"
# instead of being shown as the incidental matching login.
GENERIC_EMAILS = {
    "dev@example.com",
    "test@example.com",
    "user@example.com",
    "you@example.com",
    "root@localhost",
}

API = "https://api.github.com"
HEADERS = {
    "Authorization": f"Bearer {TOKEN}",
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
    "User-Agent": f"{ORG}-commit-stats",
}


_LINK_NEXT = re.compile(r'<([^>]+)>;\s*rel="next"')
WEEK_SEC = 7 * 24 * 3600
# Legitimate "no resource here" responses — not errors for our purpose.
_SOFT_MISS = {404, 409, 451}


def api_request(url_or_path: str, params: dict | None = None):
    """GET a GitHub API resource. Returns (status, headers, data).

    Handles two kinds of protocol-defined waits:
      - Secondary rate limit (Retry-After)
      - Primary rate limit exhausted (x-ratelimit-remaining == "0")

    These are cooperative pauses, not fallbacks. All other non-2xx
    responses either return soft-miss (404/409/451) or propagate.
    """
    url = url_or_path if url_or_path.startswith("http") else f"{API}{url_or_path}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    while True:
        req = urllib.request.Request(url, headers=HEADERS)
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                body = r.read()
                return r.status, dict(r.headers), json.loads(body) if body else None
        except urllib.error.HTTPError as e:
            retry_after = e.headers.get("retry-after")
            if retry_after and retry_after.isdigit():
                wait = min(int(retry_after) + 1, 3600)
                print(f"  secondary rate limit, waiting {wait}s", file=sys.stderr)
                time.sleep(wait)
                continue
            if e.code in (403, 429) and e.headers.get("x-ratelimit-remaining") == "0":
                reset = e.headers.get("x-ratelimit-reset")
                wait = 60
                if reset and reset.isdigit():
                    wait = max(1, int(reset) - int(time.time()) + 1)
                print(f"  rate limit exhausted, waiting {wait}s", file=sys.stderr)
                time.sleep(min(wait, 3600))
                continue
            if e.code in _SOFT_MISS:
                return e.code, dict(e.headers), None
            raise


def paginate(path: str, params: dict | None = None) -> Iterator[dict]:
    """Yield items across all pages using the Link header for navigation."""
    merged = {"per_page": 100, **(params or {})}
    _, headers, data = api_request(path, merged)
    while data:
        for item in data:
            yield item
        m = _LINK_NEXT.search(headers.get("Link", ""))
        if not m:
            return
        _, headers, data = api_request(m.group(1))


def aggregate():
    repos = list(paginate(f"/orgs/{ORG}/repos", {"type": "all"}))
    print(f"found {len(repos)} repos in {ORG}", file=sys.stderr)

    per_author: dict[str, dict] = {}
    weekly: dict[int, int] = defaultdict(int)
    active_repos = 0
    total_commits = 0

    for repo in repos:
        if repo.get("archived") or repo.get("disabled"):
            continue
        name = repo["name"]
        print(f"  · {name}", file=sys.stderr)

        count = 0
        for commit in paginate(f"/repos/{ORG}/{name}/commits"):
            meta = commit.get("author") or {}
            if meta.get("type") == "Bot":
                continue
            commit_author = commit.get("commit", {}).get("author") or {}
            email = (commit_author.get("email") or "").lower()
            if email in GENERIC_EMAILS:
                login = "unknown"
                meta = {}
            else:
                login = meta.get("login") or commit_author.get("name") or "unknown"
            if login.endswith("[bot]"):
                continue

            entry = per_author.setdefault(
                login,
                {"login": login, "avatar": meta.get("avatar_url", ""), "total": 0},
            )
            entry["total"] += 1
            total_commits += 1
            count += 1

            date_str = commit_author.get("date")
            if date_str:
                ts = int(datetime.fromisoformat(date_str.replace("Z", "+00:00")).timestamp())
                week_start = (ts // WEEK_SEC) * WEEK_SEC
                weekly[week_start] += 1

        if count > 0:
            active_repos += 1

    top = sorted(per_author.values(), key=lambda x: x["total"], reverse=True)[:TOP_N]
    weeks = sorted(weekly.items())[-52:]
    return {
        "top": top,
        "weeks": weeks,
        "repo_count": len(repos),
        "active_repos": active_repos,
        "total_commits": total_commits,
        "contributor_count": len(per_author),
    }


# --- SVG rendering ------------------------------------------------------------

WIDTH = 860
PAD_X = 32
HEADER_H = 82
ROW_H = 28
ROW_GAP = 8
NAME_COL_W = 170
COUNT_COL_W = 64
BAR_GAP = 10
BAR_SECTION_TITLE_H = 36
WEEK_SECTION_TITLE_H = 36
WEEK_CHART_H = 140
FOOTER_H = 36


def render_svg(stats: dict) -> str:
    top = stats["top"]
    weeks = stats["weeks"]
    bar_rows_h = max(1, len(top)) * (ROW_H + ROW_GAP)
    height = (
        HEADER_H
        + BAR_SECTION_TITLE_H
        + bar_rows_h
        + WEEK_SECTION_TITLE_H
        + WEEK_CHART_H
        + FOOTER_H
    )

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    max_count = max((a["total"] for a in top), default=1) or 1
    bar_area_w = WIDTH - PAD_X * 2 - NAME_COL_W - BAR_GAP - COUNT_COL_W

    parts: list[str] = []
    parts.append(
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{WIDTH}" height="{height}" '
        f'viewBox="0 0 {WIDTH} {height}" role="img" aria-label="{escape(ORG)} commit activity">'
    )
    parts.append(
        """
<style>
  .bg { fill: #ffffff; }
  .card { fill: #f6f8fa; stroke: #d0d7de; }
  .title { fill: #1f2328; font: 600 22px -apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, Arial, sans-serif; }
  .subtitle { fill: #656d76; font: 400 13px -apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, Arial, sans-serif; }
  .section { fill: #1f2328; font: 600 14px -apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, Arial, sans-serif; }
  .name { fill: #1f2328; font: 500 13px -apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, Arial, sans-serif; }
  .count { fill: #656d76; font: 500 12px ui-monospace, SFMono-Regular, Menlo, monospace; }
  .axis { fill: #8c959f; font: 400 11px ui-monospace, SFMono-Regular, Menlo, monospace; }
  .bar { fill: #2da44e; }
  .bar-bg { fill: #eaeef2; }
  .week { fill: #2da44e; }
  .week-empty { fill: #eaeef2; }
  @media (prefers-color-scheme: dark) {
    .bg { fill: #0d1117; }
    .card { fill: #151b23; stroke: #30363d; }
    .title { fill: #e6edf3; }
    .subtitle { fill: #9198a1; }
    .section { fill: #e6edf3; }
    .name { fill: #e6edf3; }
    .count { fill: #9198a1; }
    .axis { fill: #6e7681; }
    .bar { fill: #3fb950; }
    .bar-bg { fill: #21262d; }
    .week { fill: #3fb950; }
    .week-empty { fill: #21262d; }
  }
</style>
"""
    )
    parts.append(f'<rect class="bg" x="0" y="0" width="{WIDTH}" height="{height}" rx="6"/>')
    parts.append(
        f'<rect class="card" x="0.5" y="0.5" width="{WIDTH - 1}" height="{height - 1}" rx="6" fill="none"/>'
    )

    # Header
    parts.append(
        f'<text class="title" x="{PAD_X}" y="40">{escape(ORG)} / commit activity</text>'
    )
    parts.append(
        f'<text class="subtitle" x="{PAD_X}" y="62">'
        f'{stats["total_commits"]:,} commits · updated {escape(now)}</text>'
    )

    # Top contributors
    y = HEADER_H
    parts.append(f'<text class="section" x="{PAD_X}" y="{y + 20}">Top contributors</text>')
    y += BAR_SECTION_TITLE_H

    bar_x = PAD_X + NAME_COL_W
    for i, a in enumerate(top):
        ry = y + i * (ROW_H + ROW_GAP)
        name = a["login"]
        total = a["total"]
        bar_w = int(bar_area_w * (total / max_count))
        if len(name) > 18:
            parts.append(
                f'<text class="name" x="{PAD_X}" y="{ry + 18}" '
                f'textLength="{NAME_COL_W - 10}" lengthAdjust="spacingAndGlyphs">'
                f'{escape(name)}</text>'
            )
        else:
            parts.append(
                f'<text class="name" x="{PAD_X}" y="{ry + 18}">{escape(name)}</text>'
            )
        parts.append(
            f'<rect class="bar-bg" x="{bar_x}" y="{ry + 6}" '
            f'width="{bar_area_w}" height="16" rx="3"/>'
        )
        parts.append(
            f'<rect class="bar" x="{bar_x}" y="{ry + 6}" '
            f'width="{max(2, bar_w)}" height="16" rx="3"/>'
        )
        parts.append(
            f'<text class="count" x="{WIDTH - PAD_X}" y="{ry + 18}" '
            f'text-anchor="end">{total:,}</text>'
        )

    if not top:
        parts.append(
            f'<text class="subtitle" x="{PAD_X}" y="{y + 20}">No commits yet.</text>'
        )

    # Weekly activity
    y += bar_rows_h
    parts.append(
        f'<text class="section" x="{PAD_X}" y="{y + 20}">Weekly commits (last 52 weeks)</text>'
    )
    y += WEEK_SECTION_TITLE_H

    chart_w = WIDTH - PAD_X * 2
    chart_h = WEEK_CHART_H - 24
    chart_y = y
    n = 52
    max_weekly = max((v for _, v in weeks), default=1) or 1
    gap = 2
    bar_w = max(2, (chart_w - gap * (n - 1)) / n)

    # Fill missing weeks so the chart is always 52 columns wide.
    now_ts = int(datetime.now(timezone.utc).timestamp())
    # Align to start of week (Sunday UTC), matching GitHub API week buckets.
    week_sec = 7 * 24 * 3600
    end_week = (now_ts // week_sec) * week_sec
    week_map = dict(weeks)
    series = []
    for i in range(n):
        wts = end_week - (n - 1 - i) * week_sec
        series.append((wts, week_map.get(wts, 0)))

    for i, (_, v) in enumerate(series):
        h = int(chart_h * (v / max_weekly)) if max_weekly else 0
        x = PAD_X + i * (bar_w + gap)
        # Full-height background column + foreground bar anchored to bottom.
        parts.append(
            f'<rect class="week-empty" x="{x:.2f}" y="{chart_y}" '
            f'width="{bar_w:.2f}" height="{chart_h}" rx="1"/>'
        )
        if v > 0:
            parts.append(
                f'<rect class="week" x="{x:.2f}" y="{chart_y + chart_h - h}" '
                f'width="{bar_w:.2f}" height="{h}" rx="1">'
                f'<title>{datetime.fromtimestamp(series[i][0], tz=timezone.utc).date()}: {v} commits</title>'
                f'</rect>'
            )

    # Axis labels: leftmost week, rightmost week, and max value.
    left_label = datetime.fromtimestamp(series[0][0], tz=timezone.utc).strftime("%Y-%m-%d")
    right_label = datetime.fromtimestamp(series[-1][0], tz=timezone.utc).strftime("%Y-%m-%d")
    parts.append(
        f'<text class="axis" x="{PAD_X}" y="{chart_y + chart_h + 16}">{left_label}</text>'
    )
    parts.append(
        f'<text class="axis" x="{WIDTH - PAD_X}" y="{chart_y + chart_h + 16}" '
        f'text-anchor="end">{right_label}</text>'
    )
    parts.append(
        f'<text class="axis" x="{WIDTH - PAD_X}" y="{chart_y + 12}" '
        f'text-anchor="end">peak {max_weekly:,}/wk</text>'
    )

    # Footer
    y += WEEK_CHART_H
    parts.append(
        f'<text class="subtitle" x="{PAD_X}" y="{y + 22}">'
        f'Generated by GitHub Actions'
        f'</text>'
    )

    parts.append("</svg>\n")
    return "".join(parts)


def main() -> int:
    stats = aggregate()
    svg = render_svg(stats)
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(svg, encoding="utf-8")
    print(
        f"wrote {OUTPUT} — {stats['total_commits']:,} commits, "
        f"{stats['contributor_count']} contributors, {stats['repo_count']} repos",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
