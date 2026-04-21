#!/usr/bin/env python3
"""Aggregate commit counts across a GitHub organization and render an SVG."""
from __future__ import annotations

import json
import math
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from datetime import datetime, timezone
from html import escape
from pathlib import Path

ORG = os.environ["ORG"]
TOKEN = os.environ["GH_TOKEN"]
OUTPUT = Path(os.environ.get("OUTPUT", "assets/commits.svg"))
TOP_N = int(os.environ.get("TOP_N", "10"))

API = "https://api.github.com"
HEADERS = {
    "Authorization": f"Bearer {TOKEN}",
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
    "User-Agent": f"{ORG}-commit-stats",
}


def api_get(path: str, params: dict | None = None):
    url = f"{API}{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    last_err: Exception | None = None
    for attempt in range(6):
        req = urllib.request.Request(url, headers=HEADERS)
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                status = r.status
                body = r.read()
                if status == 202:
                    # Stats are being computed; wait and retry.
                    time.sleep(2 + attempt * 2)
                    continue
                return status, json.loads(body) if body else None
        except urllib.error.HTTPError as e:
            last_err = e
            if e.code == 404:
                return 404, None
            if e.code in (403, 429):
                reset = e.headers.get("x-ratelimit-reset")
                wait = 10
                if reset and reset.isdigit():
                    wait = max(5, int(reset) - int(time.time()) + 2)
                print(f"  rate limited, sleeping {wait}s", file=sys.stderr)
                time.sleep(min(wait, 120))
                continue
            if 500 <= e.code < 600:
                time.sleep(2 + attempt * 2)
                continue
            raise
        except urllib.error.URLError as e:
            last_err = e
            time.sleep(2 + attempt * 2)
    if last_err:
        print(f"  giving up on {path}: {last_err}", file=sys.stderr)
    return 0, None


def list_repos() -> list[dict]:
    repos: list[dict] = []
    page = 1
    while True:
        status, data = api_get(
            f"/orgs/{ORG}/repos",
            {"per_page": 100, "page": page, "type": "all"},
        )
        if not data:
            break
        repos.extend(data)
        if len(data) < 100:
            break
        page += 1
    return repos


def contributors(repo: str) -> list[dict]:
    status, data = api_get(f"/repos/{ORG}/{repo}/stats/contributors")
    if not data:
        return []
    return data


def weekly_activity(repo: str) -> list[dict]:
    status, data = api_get(f"/repos/{ORG}/{repo}/stats/commit_activity")
    if not data:
        return []
    return data


def aggregate():
    repos = list_repos()
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
        contribs = contributors(name)
        had_commits = False
        for c in contribs:
            if not c or not c.get("author"):
                continue
            author = c["author"]
            if author.get("type") == "Bot":
                continue
            login = author["login"]
            entry = per_author.setdefault(
                login,
                {"login": login, "avatar": author.get("avatar_url", ""), "total": 0},
            )
            entry["total"] += c.get("total", 0)
            total_commits += c.get("total", 0)
            if c.get("total", 0) > 0:
                had_commits = True
        for w in weekly_activity(name):
            if w and w.get("total"):
                weekly[w["week"]] += w["total"]
        if had_commits:
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
HEADER_H = 96
ROW_H = 28
ROW_GAP = 8
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
    bar_area_w = WIDTH - PAD_X * 2 - 200  # space for name + count

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
    subtitle = (
        f'{stats["total_commits"]:,} commits · '
        f'{stats["contributor_count"]} contributors · '
        f'{stats["active_repos"]} / {stats["repo_count"]} active repos'
    )
    parts.append(f'<text class="subtitle" x="{PAD_X}" y="62">{escape(subtitle)}</text>')
    parts.append(
        f'<text class="subtitle" x="{PAD_X}" y="80">Updated {escape(now)}</text>'
    )

    # Top contributors
    y = HEADER_H
    parts.append(f'<text class="section" x="{PAD_X}" y="{y + 20}">Top contributors</text>')
    y += BAR_SECTION_TITLE_H

    name_col_w = 170
    for i, a in enumerate(top):
        ry = y + i * (ROW_H + ROW_GAP)
        name = a["login"]
        total = a["total"]
        bar_w = int(bar_area_w * (total / max_count))
        if len(name) > 18:
            parts.append(
                f'<text class="name" x="{PAD_X}" y="{ry + 18}" '
                f'textLength="{name_col_w - 10}" lengthAdjust="spacingAndGlyphs">'
                f'{escape(name)}</text>'
            )
        else:
            parts.append(
                f'<text class="name" x="{PAD_X}" y="{ry + 18}">{escape(name)}</text>'
            )
        parts.append(
            f'<rect class="bar-bg" x="{PAD_X + name_col_w}" y="{ry + 6}" '
            f'width="{bar_area_w}" height="16" rx="3"/>'
        )
        parts.append(
            f'<rect class="bar" x="{PAD_X + name_col_w}" y="{ry + 6}" '
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
