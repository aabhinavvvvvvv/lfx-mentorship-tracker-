#!/usr/bin/env python3
"""
LFX Mentorship Term 2 Issue Watcher
=====================================
Scans every GitHub repo that has ever appeared in CNCF LFX Mentorship
(2025 Term 1/2/3 + 2026 Term 1) for new issues that look like LFX
Term 2 applications / announcements.

Sources:
  · results.json          — 2026 Term 1 (from scraper.py)
  · Historical READMEs    — 2025/01, 2025/02, 2025/03 (fetched live)

Usage:
  python watch.py --github-token ghp_xxx
  python watch.py -t ghp_xxx --days 60 --out term2_issues.json
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import logging
import os
import re
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
from rich import box
from rich.console import Console
from rich.logging import RichHandler
from rich.panel import Panel
from rich.progress import (
    BarColumn,
    MofNCompleteColumn,
    Progress,
    SpinnerColumn,
    TextColumn,
    TimeElapsedColumn,
)
from rich.table import Table
from tenacity import (
    before_sleep_log,
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

# ─────────────────────────────────────────────────────────────────────────────
# Setup
# ─────────────────────────────────────────────────────────────────────────────

console = Console()
logging.basicConfig(
    level=logging.WARNING,
    format="%(message)s",
    handlers=[RichHandler(console=console, show_path=False, markup=True)],
)
logger = logging.getLogger("lfx_watcher")
logger.setLevel(logging.INFO)

GITHUB_API = "https://api.github.com"
GH_DELAY   = 0.35

# Historical READMEs to pull repo lists from
HISTORICAL_READMES = [
    "programs/lfx-mentorship/2025/01-Mar-May/README.md",
    "programs/lfx-mentorship/2025/02-Jun-Aug/README.md",
    "programs/lfx-mentorship/2025/03-Sep-Nov/README.md",
]

ISSUE_RE = re.compile(
    r"https?://github\.com/([\w\-\.]+)/([\w\-\.]+)/issues/\d+", re.I
)

# Keywords that suggest an issue is about LFX Term 2 mentorship
TERM2_TITLE_RE = re.compile(
    r"lfx|mentorship|mentoring|lf.?mentoring|linux.?foundation|term.?2|"
    r"jun|aug|summer|cohort|apprentice|mentee",
    re.IGNORECASE,
)
TERM2_BODY_RE = re.compile(
    r"lfx|mentorship\.lfx|linuxfoundation\.org/project|"
    r"term.?2|2026.*jun|jun.*2026|jun.*aug|"
    r"mentee.application|apply.*mentee|mentee.*slot",
    re.IGNORECASE,
)
LFX_URL_RE = re.compile(
    r"https?://mentorship\.lfx\.linuxfoundation\.org/projects?/[\w\-]{8,}[^\s\)\]>\"']*",
    re.IGNORECASE,
)

# ─────────────────────────────────────────────────────────────────────────────
# Data model
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class WatchedIssue:
    repo:         str
    org_name:     str = ""
    issue_url:    str = ""
    issue_title:  str = ""
    issue_number: int = 0
    created_at:   str = ""
    state:        str = ""
    lfx_url:      str = ""
    confidence:   str = ""
    labels:       str = ""

# ─────────────────────────────────────────────────────────────────────────────
# GitHub client
# ─────────────────────────────────────────────────────────────────────────────

class GitHubClient:
    def __init__(self, token: str):
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if token:
            headers["Authorization"] = f"Bearer {token}"
        self._client = httpx.AsyncClient(
            base_url=GITHUB_API, headers=headers, timeout=30, follow_redirects=True,
        )
        self._last_call = 0.0

    async def close(self):
        await self._client.aclose()

    async def _throttle(self):
        elapsed = time.monotonic() - self._last_call
        if elapsed < GH_DELAY:
            await asyncio.sleep(GH_DELAY - elapsed)
        self._last_call = time.monotonic()

    @retry(
        retry=retry_if_exception_type((httpx.TimeoutException, httpx.NetworkError)),
        wait=wait_exponential(multiplier=1, min=2, max=30),
        stop=stop_after_attempt(4),
        before_sleep=before_sleep_log(logger, logging.WARNING),
        reraise=True,
    )
    async def _get(self, path: str, **kwargs) -> httpx.Response:
        await self._throttle()
        r = await self._client.get(path, **kwargs)
        if r.status_code == 403 and "rate limit" in r.text.lower():
            reset = int(r.headers.get("x-ratelimit-reset", time.time() + 60))
            wait  = max(reset - int(time.time()), 5)
            logger.warning(f"Rate limit — sleeping {wait}s")
            await asyncio.sleep(wait)
            return await self._get(path, **kwargs)
        return r

    async def get_readme(self, path: str) -> str:
        """Fetch a file from cncf/mentoring and return decoded text."""
        r = await self._get(f"/repos/cncf/mentoring/contents/{path}")
        if r.status_code != 200:
            logger.warning(f"Could not fetch {path}: {r.status_code}")
            return ""
        return base64.b64decode(r.json()["content"]).decode("utf-8", errors="replace")

    async def get_recent_issues(
        self, owner: str, repo: str, since_iso: str, sem: asyncio.Semaphore
    ) -> list[dict]:
        issues: list[dict] = []
        page = 1
        async with sem:
            while True:
                r = await self._get(
                    f"/repos/{owner}/{repo}/issues",
                    params={
                        "state": "all",
                        "sort": "created",
                        "direction": "desc",
                        "since": since_iso,
                        "per_page": 50,
                        "page": page,
                    },
                )
                if r.status_code != 200:
                    break
                batch = [i for i in r.json() if "pull_request" not in i]
                issues.extend(batch)
                if len(r.json()) < 50:
                    break
                page += 1
        return issues

# ─────────────────────────────────────────────────────────────────────────────
# Repo collection helpers
# ─────────────────────────────────────────────────────────────────────────────

def repos_from_results(path: str) -> dict[str, str]:
    """Return {owner/repo: org_name} from results.json (2026 Term 1)."""
    repos: dict[str, str] = {}
    try:
        data = json.loads(Path(path).read_text())
    except FileNotFoundError:
        return repos
    for rec in data:
        m = ISSUE_RE.search(rec.get("issue_url", ""))
        if m:
            key = f"{m.group(1)}/{m.group(2)}"
            if key not in repos:
                repos[key] = rec.get("org_name", "")
    return repos


def repos_from_readme_text(text: str) -> list[str]:
    """Extract unique owner/repo pairs from a README's issue links."""
    seen: set[str] = set()
    result: list[str] = []
    for m in ISSUE_RE.finditer(text):
        key = f"{m.group(1)}/{m.group(2)}"
        if key not in seen:
            seen.add(key)
            result.append(key)
    return result

# ─────────────────────────────────────────────────────────────────────────────
# Issue scoring
# ─────────────────────────────────────────────────────────────────────────────

def score_issue(issue: dict) -> str | None:
    title      = issue.get("title", "")
    body       = issue.get("body", "") or ""
    labels     = " ".join(lbl["name"].lower() for lbl in issue.get("labels", []))
    has_lfx    = bool(LFX_URL_RE.search(body))
    title_hit  = bool(TERM2_TITLE_RE.search(title))
    body_hit   = bool(TERM2_BODY_RE.search(body))
    label_hit  = any(k in labels for k in ("lfx", "mentoring", "mentorship", "mentor"))

    if has_lfx or (title_hit and (body_hit or label_hit)):
        return "high"
    if title_hit or (body_hit and label_hit):
        return "medium"
    return None

# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────

async def run(args: argparse.Namespace):
    token = args.github_token or os.getenv("GITHUB_TOKEN", "")
    gh    = GitHubClient(token)

    # ── Build repo list ───────────────────────────────────────────────────────
    console.print("[cyan]Building repo list from historical READMEs…[/cyan]")

    # Start with 2026 Term 1 repos (have org names)
    all_repos: dict[str, str] = repos_from_results(args.results)

    # Add repos from 2025 READMEs (no org name, just repo key)
    for readme_path in HISTORICAL_READMES:
        text = await gh.get_readme(readme_path)
        if text:
            term = readme_path.split("/")[-2]          # e.g. "01-Mar-May"
            new_repos = repos_from_readme_text(text)
            added = 0
            for repo_key in new_repos:
                if repo_key not in all_repos:
                    all_repos[repo_key] = ""
                    added += 1
            console.print(
                f"  [dim]{term}[/dim] → {len(new_repos)} repos "
                f"([green]+{added} new[/green])"
            )

    console.print(Panel(
        f"[bold cyan]LFX Term 2 Issue Watcher[/bold cyan]\n"
        f"[yellow]{len(all_repos)}[/yellow] unique repos across all terms · "
        f"looking back [yellow]{args.days}[/yellow] days",
        border_style="cyan",
    ))

    # ── Scan issues ───────────────────────────────────────────────────────────
    since_iso = (datetime.now(timezone.utc) - timedelta(days=args.days)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    sem   = asyncio.Semaphore(args.concurrency)
    found: list[WatchedIssue] = []

    progress = Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        MofNCompleteColumn(),
        TimeElapsedColumn(),
        console=console,
    )
    task_id = progress.add_task("Scanning repos…", total=len(all_repos))

    async def process(repo_key: str, org_name: str):
        owner, repo = repo_key.split("/", 1)
        issues = await gh.get_recent_issues(owner, repo, since_iso, sem)
        for issue in issues:
            conf = score_issue(issue)
            if conf:
                body = issue.get("body", "") or ""
                m    = LFX_URL_RE.search(body)
                found.append(WatchedIssue(
                    repo         = repo_key,
                    org_name     = org_name or repo_key.split("/")[0],
                    issue_url    = issue["html_url"],
                    issue_title  = issue["title"],
                    issue_number = issue["number"],
                    created_at   = issue["created_at"][:10],
                    state        = issue["state"],
                    lfx_url      = m.group(0) if m else "",
                    confidence   = conf,
                    labels       = ", ".join(lbl["name"] for lbl in issue.get("labels", [])),
                ))
        progress.advance(task_id)

    with progress:
        await asyncio.gather(*[process(r, o) for r, o in all_repos.items()])

    await gh.close()

    # ── Display ───────────────────────────────────────────────────────────────
    found.sort(key=lambda x: (x.confidence != "high", x.created_at))

    table = Table(
        box=box.ROUNDED, border_style="cyan",
        show_header=True, header_style="bold cyan",
    )
    table.add_column("Conf.",   style="bold", width=8)
    table.add_column("Date",    width=11)
    table.add_column("Org",     width=20)
    table.add_column("Issue Title", width=48)
    table.add_column("State",   width=7)

    for wi in found:
        cs = "green" if wi.confidence == "high" else "yellow"
        ss = "green" if wi.state == "open" else "dim"
        table.add_row(
            f"[{cs}]{wi.confidence}[/{cs}]",
            wi.created_at,
            wi.org_name,
            wi.issue_title[:47],
            f"[{ss}]{wi.state}[/{ss}]",
        )

    console.print()
    if found:
        console.print(table)
        console.print(
            f"\n[bold green]Found {len(found)} potential Term 2 issue(s)[/bold green] "
            f"across {len(set(w.repo for w in found))} repos"
        )
    else:
        console.print(Panel(
            "[yellow]No Term 2 issues found yet.[/yellow]\n"
            "Run again closer to when Term 2 applications open.",
            border_style="yellow",
        ))

    # ── Save ──────────────────────────────────────────────────────────────────
    out = Path(args.out)
    out.write_text(json.dumps([asdict(w) for w in found], indent=2))
    console.print(f"[dim]Saved → {out}[/dim]")


def load_token_from_env_file() -> str:
    """Read GITHUB_TOKEN from .env file in the current directory."""
    env_path = Path(".env")
    if not env_path.exists():
        return ""
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if line.startswith("GITHUB_TOKEN"):
            parts = line.split("=", 1)
            if len(parts) == 2:
                return parts[1].strip().strip('"').strip("'")
    return ""


def main():
    p = argparse.ArgumentParser(description="LFX Term 2 Issue Watcher")
    p.add_argument("-t", "--github-token", default="",
                   help="GitHub PAT (or set GITHUB_TOKEN env var / .env file)")
    p.add_argument("--days", type=int, default=90,
                   help="How many days back to search (default: 90)")
    p.add_argument("--results", default="results.json",
                   help="Term 1 results file (default: results.json)")
    p.add_argument("--out", default="term2_issues.json",
                   help="Output file (default: term2_issues.json)")
    p.add_argument("-c", "--concurrency", type=int, default=5)
    args = p.parse_args()
    # Token priority: CLI arg → env var → .env file
    if not args.github_token:
        args.github_token = os.getenv("GITHUB_TOKEN", "") or load_token_from_env_file()
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
