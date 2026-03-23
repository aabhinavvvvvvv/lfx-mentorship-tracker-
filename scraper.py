#!/usr/bin/env python3
"""
LFX Mentorship Scraper  ·  CNCF 2026 Mar-May Cohort
====================================================
Scrapes mentee data from the CNCF LFX mentorship programme using:
  · GitHub API  – reads repo files and issues
  · LFX REST API – fetches mentee records (no browser needed)
  · GitHub Users API – resolves location for India detection

Usage:
  python scraper.py
  python scraper.py -t ghp_xxx -o results --indian-only -c 8
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import logging
import re
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional

import httpx
import pandas as pd
from rich import box
from rich.align import Align
from rich.columns import Columns
from rich.console import Console, Group
from rich.live import Live
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
from rich.rule import Rule
from rich.table import Table
from rich.text import Text
from tenacity import (
    before_sleep_log,
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

# ─────────────────────────────────────────────────────────────────────────────
# Console & Logging – route all log output through Rich
# ─────────────────────────────────────────────────────────────────────────────

console = Console()

logging.basicConfig(
    level=logging.WARNING,          # httpx noise stays quiet by default
    format="%(message)s",
    handlers=[RichHandler(console=console, show_path=False, markup=True)],
)
logger = logging.getLogger("lfx_scraper")
logger.setLevel(logging.INFO)

# ─────────────────────────────────────────────────────────────────────────────
# Constants & Patterns
# ─────────────────────────────────────────────────────────────────────────────

GITHUB_API  = "https://api.github.com"
CNCF_OWNER  = "cncf"
CNCF_REPO   = "mentoring"
LFX_DIR     = "programs/lfx-mentorship/2026/01-Mar-May"
LFX_API     = "https://api.mentorship.lfx.linuxfoundation.org"

# Only match URLs that contain a real UUID path — not hash-anchor URLs
LFX_URL_RE = re.compile(
    r"https?://mentorship\.lfx\.linuxfoundation\.org/projects?/[\w\-]{8,}[^\s\)\]>\"']*",
    re.IGNORECASE,
)
LFX_UUID_RE = re.compile(
    r"mentorship\.lfx\.linuxfoundation\.org/projects?/([\w\-]+)",
    re.IGNORECASE,
)
GITHUB_ISSUE_RE = re.compile(
    r"https?://github\.com/([\w\-\.]+)/([\w\-\.]+)/issues/(\d+)",
    re.IGNORECASE,
)
GITHUB_USER_RE = re.compile(
    r"https?://github\.com/([\w\-]+)/?$",
    re.IGNORECASE,
)
GITHUB_AVATAR_ID_RE = re.compile(
    r"avatars\d*\.githubusercontent\.com/u/(\d+)",
    re.IGNORECASE,
)

INDIA_KEYWORDS = frozenset([
    "india",
    "bangalore", "bengaluru",
    "mumbai", "bombay",
    "delhi", "new delhi",
    "hyderabad", "pune",
    "chennai", "madras",
    "kolkata", "calcutta",
    "ahmedabad", "jaipur",
    "noida", "gurugram", "gurgaon",
    "surat", "lucknow", "nagpur",
    "indore", "bhopal", "patna",
    "chandigarh", "coimbatore",
    "visakhapatnam", "vizag",
    "kochi", "cochin",
    "iit", "nit ",
    "karnataka", "maharashtra", "tamil nadu", "telangana",
    "rajasthan", "gujarat", "uttar pradesh", "west bengal",
    "kerala", "andhra pradesh", "madhya pradesh", "bihar", "odisha",
    "punjab", "haryana", "assam", "jharkhand", "uttarakhand",
])

GH_API_DELAY  = 0.4
LFX_API_DELAY = 0.3

# ─────────────────────────────────────────────────────────────────────────────
# Data Model
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class MenteeRecord:
    lfx_url:        str
    issue_url:      str
    org_name:       str           = ""   # CNCF project name from README (e.g. "Antrea")
    project_title:  str           = ""   # project title from README
    mentee_name:    str           = ""
    mentee_profile: str           = ""
    mentee_linkedin: str          = ""
    location:       str           = ""
    is_indian:      Optional[bool] = None
    mentors:        str           = ""   # comma-separated mentor names
    error:          str           = ""

    def to_output_dict(self) -> dict:
        return {
            "lfx_url":         self.lfx_url,
            "issue_url":       self.issue_url,
            "org_name":        self.org_name,
            "project_title":   self.project_title,
            "mentee_name":     self.mentee_name,
            "mentee_profile":  self.mentee_profile,
            "mentee_linkedin": self.mentee_linkedin,
            "location":        self.location,
            "is_indian":       self.is_indian,
            "mentors":         self.mentors,
        }

# ─────────────────────────────────────────────────────────────────────────────
# Rich UI helpers
# ─────────────────────────────────────────────────────────────────────────────

HEADER = """\
 ██╗     ███████╗██╗  ██╗    ███████╗ ██████╗██████╗  █████╗ ██████╗ ███████╗██████╗
 ██║     ██╔════╝╚██╗██╔╝    ██╔════╝██╔════╝██╔══██╗██╔══██╗██╔══██╗██╔════╝██╔══██╗
 ██║     █████╗   ╚███╔╝     ███████╗██║     ██████╔╝███████║██████╔╝█████╗  ██████╔╝
 ██║     ██╔══╝   ██╔██╗     ╚════██║██║     ██╔══██╗██╔══██║██╔═══╝ ██╔══╝  ██╔══██╗
 ███████╗██║     ██╔╝ ██╗    ███████║╚██████╗██║  ██║██║  ██║██║     ███████╗██║  ██║
 ╚══════╝╚═╝     ╚═╝  ╚═╝    ╚══════╝ ╚═════╝╚═╝  ╚═╝╚═╝  ╚═╝╚═╝     ╚══════╝╚═╝  ╚═╝"""


def make_header() -> Panel:
    title = Text(HEADER, style="bold cyan", justify="center")
    subtitle = Text(
        "CNCF LFX Mentorship  ·  2026 Mar–May Cohort  ·  India Detection",
        style="dim white", justify="center",
    )
    return Panel(
        Align.center(Group(title, subtitle)),
        border_style="bright_blue",
        padding=(0, 2),
    )


def make_results_table(records: list[MenteeRecord]) -> Table:
    t = Table(
        box=box.ROUNDED,
        border_style="grey50",
        header_style="bold white on grey23",
        show_lines=True,
        expand=True,
    )
    t.add_column("#",              style="dim",         width=4,  no_wrap=True)
    t.add_column("Mentee Name",    style="bold white",  min_width=22)
    t.add_column("GitHub Profile", style="cyan",        min_width=28)
    t.add_column("Location",       style="yellow",      min_width=18)
    t.add_column("Indian?",        justify="center",    width=9)
    t.add_column("Project Issue",  style="dim cyan",    min_width=20)

    for i, rec in enumerate(records, 1):
        # Indian? badge
        if rec.is_indian is True:
            badge = Text("✓  Yes", style="bold green")
        elif rec.is_indian is False:
            badge = Text("✗  No",  style="red")
        else:
            badge = Text("?  —",   style="dim yellow")

        # shorten URLs for display
        profile_display = rec.mentee_profile.replace("https://github.com/", "github.com/") \
                          if rec.mentee_profile else "[dim]—[/dim]"
        issue_display   = "/".join(rec.issue_url.split("/")[-4:]) \
                          if rec.issue_url else "—"

        t.add_row(
            str(i),
            rec.mentee_name or "[dim italic]unknown[/dim italic]",
            profile_display,
            rec.location    or "[dim]—[/dim]",
            badge,
            issue_display,
        )

    return t


def make_stats_panel(records: list[MenteeRecord]) -> Panel:
    total   = len(records)
    named   = sum(1 for r in records if r.mentee_name)
    indian  = sum(1 for r in records if r.is_indian is True)
    not_ind = sum(1 for r in records if r.is_indian is False)
    unknown = sum(1 for r in records if r.is_indian is None)

    pct = f"{indian/total*100:.0f}%" if total else "—"

    stats = Text()
    stats.append(f"  Total records  ", style="bold white")
    stats.append(f"{total:>4}\n",       style="bold cyan")
    stats.append(f"  Named mentees  ", style="bold white")
    stats.append(f"{named:>4}\n",       style="white")
    stats.append(f"  ✓ Indian       ", style="bold green")
    stats.append(f"{indian:>4}  ({pct})\n", style="bold green")
    stats.append(f"  ✗ Not Indian   ", style="red")
    stats.append(f"{not_ind:>4}\n",     style="red")
    stats.append(f"  ? Unknown      ", style="dim yellow")
    stats.append(f"{unknown:>4}",       style="dim yellow")

    return Panel(stats, title="[bold white]Stats[/bold white]",
                 border_style="grey50", padding=(0, 1))


def make_progress() -> Progress:
    return Progress(
        SpinnerColumn(style="cyan"),
        TextColumn("[bold white]{task.description}"),
        BarColumn(bar_width=32, style="cyan", complete_style="bright_cyan"),
        MofNCompleteColumn(),
        TextColumn("[dim]{task.fields[status]}"),
        TimeElapsedColumn(),
        console=console,
        transient=False,
    )

# ─────────────────────────────────────────────────────────────────────────────
# GitHub API Client
# ─────────────────────────────────────────────────────────────────────────────

class GitHubClient:
    def __init__(self, token: Optional[str] = None) -> None:
        headers = {
            "Accept":     "application/vnd.github.v3+json",
            "User-Agent": "lfx-mentorship-scraper/2.0",
        }
        if token:
            headers["Authorization"] = f"token {token}"
        self._client = httpx.AsyncClient(
            headers=headers, timeout=30.0, follow_redirects=True
        )
        self._last_call_ts: float = 0.0

    async def _throttle(self) -> None:
        elapsed = time.monotonic() - self._last_call_ts
        if elapsed < GH_API_DELAY:
            await asyncio.sleep(GH_API_DELAY - elapsed)
        self._last_call_ts = time.monotonic()

    @retry(
        retry=retry_if_exception_type((httpx.TransportError, httpx.TimeoutException)),
        stop=stop_after_attempt(4),
        wait=wait_exponential(multiplier=1, min=2, max=30),
        before_sleep=before_sleep_log(logger, logging.WARNING),
    )
    async def _get(self, url: str, **kwargs) -> httpx.Response:
        await self._throttle()
        resp = await self._client.get(url, **kwargs)
        if resp.status_code == 403:
            body = resp.text.lower()
            if "rate limit" in body or "api rate limit" in body:
                reset_ts   = int(resp.headers.get("X-RateLimit-Reset", time.time() + 60))
                sleep_secs = max(reset_ts - time.time() + 5, 10)
                logger.warning("GitHub rate limit – sleeping [cyan]%.0fs[/]", sleep_secs)
                await asyncio.sleep(sleep_secs)
                raise httpx.TransportError("rate limited")
        resp.raise_for_status()
        return resp

    async def get_json(self, url: str, **kwargs):
        return (await self._get(url, **kwargs)).json()

    async def list_dir(self, path: str) -> list[dict]:
        return await self.get_json(
            f"{GITHUB_API}/repos/{CNCF_OWNER}/{CNCF_REPO}/contents/{path}"
        )

    async def get_file_text(self, path: str) -> str:
        data = await self.get_json(
            f"{GITHUB_API}/repos/{CNCF_OWNER}/{CNCF_REPO}/contents/{path}"
        )
        return base64.b64decode(data["content"]).decode("utf-8")

    async def get_issue(self, owner: str, repo: str, number: int) -> dict:
        return await self.get_json(
            f"{GITHUB_API}/repos/{owner}/{repo}/issues/{number}"
        )

    async def get_issue_comments(self, owner: str, repo: str, number: int) -> list[dict]:
        url     = f"{GITHUB_API}/repos/{owner}/{repo}/issues/{number}/comments"
        results: list[dict] = []
        page    = 1
        while True:
            data = await self.get_json(url, params={"per_page": 100, "page": page})
            results.extend(data)
            if len(data) < 100:
                break
            page += 1
        return results

    async def get_user(self, username: str) -> dict:
        return await self.get_json(f"{GITHUB_API}/users/{username}")

    async def get_user_by_id(self, user_id: int) -> dict:
        return await self.get_json(f"{GITHUB_API}/user/{user_id}")

    async def close(self) -> None:
        await self._client.aclose()

# ─────────────────────────────────────────────────────────────────────────────
# LFX REST API Client
# ─────────────────────────────────────────────────────────────────────────────

class LFXClient:
    """
    Public LFX mentorship REST API — no auth required.

    Key endpoints discovered:
      GET /projects/{uuid}/active-mentees  → selected/accepted mentee for the term
      GET /projects/{uuid}/mentors         → mentor list
    """

    def __init__(self) -> None:
        self._client = httpx.AsyncClient(
            headers={"User-Agent": "lfx-mentorship-scraper/2.0"},
            timeout=30.0,
            follow_redirects=True,
        )
        self._last_call_ts: float = 0.0

    async def _throttle(self) -> None:
        elapsed = time.monotonic() - self._last_call_ts
        if elapsed < LFX_API_DELAY:
            await asyncio.sleep(LFX_API_DELAY - elapsed)
        self._last_call_ts = time.monotonic()

    @retry(
        retry=retry_if_exception_type((httpx.TransportError, httpx.TimeoutException)),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=15),
        before_sleep=before_sleep_log(logger, logging.WARNING),
    )
    async def _get(self, url: str, **kwargs) -> httpx.Response:
        await self._throttle()
        resp = await self._client.get(url, **kwargs)
        resp.raise_for_status()
        return resp

    async def get_active_mentee(self, project_uuid: str) -> list[dict]:
        """Returns the accepted/selected mentee(s) for the current term."""
        try:
            resp = await self._get(f"{LFX_API}/projects/{project_uuid}/active-mentees")
            return resp.json().get("mentees") or []
        except Exception as exc:
            logger.error("LFX active-mentees error [cyan]%s[/]: %s", project_uuid, exc)
            return []

    async def get_mentors(self, project_uuid: str) -> list[dict]:
        """Returns the mentor list for a project."""
        try:
            resp = await self._get(f"{LFX_API}/projects/{project_uuid}/mentors")
            return resp.json().get("mentors") or []
        except Exception as exc:
            logger.error("LFX mentors error [cyan]%s[/]: %s", project_uuid, exc)
            return []

    async def close(self) -> None:
        await self._client.aclose()

# ─────────────────────────────────────────────────────────────────────────────
# Scraping steps
# ─────────────────────────────────────────────────────────────────────────────

async def get_projects_from_readme(gh: GitHubClient) -> list[dict]:
    """
    Parse the CNCF mentoring README to extract all project entries.

    The README has this structure under "## Accepted Projects":
        ### OrgName
        #### Project Title
        - ...
        - Upstream Issue: <github url>   (may be absent)
        - LFX URL: <lfx url>

    Returns list of {org_name, project_title, issue_url, lfx_url}.
    """
    try:
        content = await gh.get_file_text(f"{LFX_DIR}/README.md")
    except Exception as exc:
        logger.error("Failed to read README: %s", exc)
        return []

    ISSUE_LINE_RE = re.compile(
        r"- Upstream Issue:\s*(https?://github\.com/[\w\-\.]+/[\w\-\.]+/issues/\d+)", re.I
    )
    LFX_LINE_RE = re.compile(
        r"- LFX URL:\s*(https?://mentorship\.lfx\.linuxfoundation\.org/project[s]?/[\w\-]+)", re.I
    )
    ORG_RE   = re.compile(r"^### (.+)$", re.M)
    PROJ_RE  = re.compile(r"^#### (.+)$", re.M)

    events: list[tuple[int, str, str]] = []
    for m in ORG_RE.finditer(content):
        events.append((m.start(), "org", m.group(1).strip()))
    for m in PROJ_RE.finditer(content):
        events.append((m.start(), "proj", m.group(1).strip()))
    for m in ISSUE_LINE_RE.finditer(content):
        events.append((m.start(), "issue", m.group(1).strip()))
    for m in LFX_LINE_RE.finditer(content):
        events.append((m.start(), "lfx", m.group(1).strip()))
    events.sort(key=lambda x: x[0])

    records: list[dict] = []
    current_org   = ""
    current_proj  = ""
    pending_issue = ""

    for _, typ, val in events:
        if typ == "org":
            current_org   = val
            current_proj  = ""
            pending_issue = ""
        elif typ == "proj":
            current_proj  = val
            pending_issue = ""
        elif typ == "issue":
            pending_issue = val
        elif typ == "lfx":
            records.append({
                "org_name":      current_org,
                "project_title": current_proj,
                "issue_url":     pending_issue,
                "lfx_url":       val,
            })
            pending_issue = ""

    return records


def _check_india(location: str = "", intro_text: str = "") -> Optional[bool]:
    combined = (location + " " + intro_text).lower()
    for kw in INDIA_KEYWORDS:
        if kw in combined:
            return True
    if location.strip():
        return False
    return None


def _normalise_url(url: str) -> str:
    """Ensure a URL has https:// prefix."""
    url = (url or "").strip()
    if url and not url.startswith("http"):
        url = "https://" + url
    return url


async def process_lfx_url(
    lfx_url:       str,
    issue_url:     str,
    org_name:      str,
    project_title: str,
    gh:            GitHubClient,
    lfx:           LFXClient,
    cache:         dict,
) -> MenteeRecord:
    """
    Fetch the selected mentee + mentors for one LFX project.
    Returns a single MenteeRecord (mentee_name="" if not yet selected).
    """
    uuid_match = LFX_UUID_RE.search(lfx_url)
    if not uuid_match:
        logger.warning("Cannot extract UUID from [yellow]%s[/]", lfx_url)
        return MenteeRecord(lfx_url=lfx_url, issue_url=issue_url,
                            org_name=org_name, project_title=project_title)

    proj_uuid = uuid_match.group(1)
    rec = MenteeRecord(lfx_url=lfx_url, issue_url=issue_url,
                       org_name=org_name, project_title=project_title)

    # ── Mentors ──────────────────────────────────────────────────────────────
    if proj_uuid not in cache.get("mentors", {}):
        cache.setdefault("mentors", {})[proj_uuid] = await lfx.get_mentors(proj_uuid)
    mentor_list = cache["mentors"][proj_uuid]
    rec.mentors = ", ".join(m.get("name", "") for m in mentor_list if m.get("name"))

    # ── Selected (active) mentee ─────────────────────────────────────────────
    if proj_uuid not in cache.get("active", {}):
        cache.setdefault("active", {})[proj_uuid] = await lfx.get_active_mentee(proj_uuid)
    active = cache["active"][proj_uuid]

    if not active:
        return rec   # no selection yet

    mentee = active[0]
    first  = (mentee.get("firstName") or "").strip()
    last   = (mentee.get("lastName")  or "").strip()
    rec.mentee_name = f"{first} {last}".strip()

    intro  = mentee.get("introduction") or ""
    links  = mentee.get("profileLinks") or {}

    # ── GitHub profile ────────────────────────────────────────────────────────
    gh_url = _normalise_url(links.get("githubProfileLink", ""))
    if gh_url and "github.com/" in gh_url:
        rec.mentee_profile = gh_url.rstrip("/")
        # Resolve location from GitHub user API
        login = gh_url.rstrip("/").split("/")[-1]
        try:
            gh_user  = await gh.get_user(login)
            loc      = (gh_user.get("location") or "").strip()
            rec.location  = loc
            rec.is_indian = _check_india(loc, intro)
        except Exception:
            pass

    # ── LinkedIn ──────────────────────────────────────────────────────────────
    li_raw = links.get("linkedinProfileLink", "")
    if li_raw:
        li_url = _normalise_url(li_raw)
        if "linkedin.com" in li_url:
            rec.mentee_linkedin = li_url.rstrip("/")

    # ── India fallback (intro text only) ─────────────────────────────────────
    if rec.is_indian is None:
        rec.is_indian = _check_india("", intro)

    return rec

# ─────────────────────────────────────────────────────────────────────────────
# Top-level runner
# ─────────────────────────────────────────────────────────────────────────────

async def run_scraper(
    github_token:    Optional[str],
    output_prefix:   str,
    indian_only:     bool,
    max_concurrency: int,
) -> list[dict]:
    gh        = GitHubClient(token=github_token)
    lfx       = LFXClient()
    lfx_cache: dict              = {}
    all_records: list[MenteeRecord] = []

    progress = make_progress()
    live_records: list[MenteeRecord] = []

    def build_live() -> Group:
        parts: list = [make_header(), ""]
        if progress.tasks:
            parts.append(Panel(progress, title="[bold white]Progress[/bold white]",
                               border_style="bright_blue", padding=(0, 1)))
            parts.append("")
        if live_records:
            parts.append(Panel(
                make_results_table(live_records),
                title=f"[bold white]Live Results[/bold white]  [dim]({len(live_records)} rows)[/dim]",
                border_style="bright_blue",
            ))
            parts.append("")
            parts.append(make_stats_panel(live_records))
        return Group(*parts)

    with Live(build_live(), console=console, refresh_per_second=6, vertical_overflow="visible") as live:

        def refresh():
            live.update(build_live())

        # ── Step 1: parse README for all projects ───────────────────────────
        t_init = progress.add_task(
            "Reading CNCF README", total=2, status=""
        )
        progress.advance(t_init); refresh()

        lfx_entries = await get_projects_from_readme(gh)
        progress.advance(t_init); refresh()

        if not lfx_entries:
            progress.stop()
            console.print("[bold red]No projects found in README.[/bold red]")
            return []

        progress.update(t_init, completed=2,
                        status=f"[green]{len(lfx_entries)} projects[/green]")
        refresh()

        # ── Step 2: fetch mentees from LFX API ──────────────────────────────
        t_lfx = progress.add_task(
            "Fetching mentees   ", total=len(lfx_entries), status=""
        )
        semaphore = asyncio.Semaphore(max_concurrency)

        async def _bounded(entry: dict) -> MenteeRecord:
            async with semaphore:
                return await process_lfx_url(
                    entry["lfx_url"], entry["issue_url"],
                    entry["org_name"], entry["project_title"],
                    gh, lfx, lfx_cache
                )

        tasks = [asyncio.ensure_future(_bounded(e)) for e in lfx_entries]

        for fut in asyncio.as_completed(tasks):
            rec = await fut
            all_records.append(rec)
            live_records.append(rec)
            status = f"[dim]{rec.mentee_name[:32]}[/dim]" if rec.mentee_name else ""
            progress.update(t_lfx, advance=1, status=status)
            refresh()

        progress.update(t_lfx, status="[green]done[/green]")
        refresh()

    await gh.close()
    await lfx.close()

    results = [r.to_output_dict() for r in all_records]
    _save(results, output_prefix, indian_only)
    return results

# ─────────────────────────────────────────────────────────────────────────────
# Save & final summary
# ─────────────────────────────────────────────────────────────────────────────

def _save(results: list[dict], prefix: str, indian_only: bool) -> None:
    if indian_only:
        results = [r for r in results if r.get("is_indian") is True]

    json_path = f"{prefix}.json"
    Path(json_path).write_text(
        json.dumps(results, indent=2, ensure_ascii=False), encoding="utf-8"
    )

    csv_path = f"{prefix}.csv"
    cols = ["lfx_url", "issue_url", "mentee_name", "mentee_profile", "location", "is_indian"]
    df = pd.DataFrame(results, columns=cols) if results else pd.DataFrame(columns=cols)
    df.to_csv(csv_path, index=False, encoding="utf-8")

    # ── Final summary panel ──────────────────────────────────────────────────
    total   = len(results)
    named   = sum(1 for r in results if r.get("mentee_name"))
    indian  = sum(1 for r in results if r.get("is_indian") is True)
    not_ind = sum(1 for r in results if r.get("is_indian") is False)
    unknown = sum(1 for r in results if r.get("is_indian") is None)
    pct     = f"{indian/total*100:.1f}%" if total else "—"

    summary = Table.grid(padding=(0, 3))
    summary.add_column(justify="right",  style="dim white")
    summary.add_column(justify="left")

    summary.add_row("Total records",   f"[bold cyan]{total}[/]")
    summary.add_row("Named mentees",   f"[white]{named}[/]")
    summary.add_row("✓ Indian",        f"[bold green]{indian}[/]  [dim]({pct})[/]")
    summary.add_row("✗ Not Indian",    f"[red]{not_ind}[/]")
    summary.add_row("? Unknown",       f"[yellow]{unknown}[/]")
    summary.add_row("",                "")
    summary.add_row("CSV output",      f"[cyan]{csv_path}[/]")
    summary.add_row("JSON output",     f"[cyan]{json_path}[/]")
    summary.add_row("Completed",       f"[dim]{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}[/]")

    console.print()
    console.print(Rule("[bold white]Run Complete[/bold white]", style="bright_blue"))
    console.print(Panel(
        Align.center(summary),
        title="[bold white] Results Summary [/bold white]",
        border_style="bright_blue",
        padding=(1, 4),
    ))

# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────

def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="scraper.py",
        description="Scrape LFX mentorship data from the CNCF mentoring repository.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
examples:
  python scraper.py
  python scraper.py -t ghp_xxxx
  python scraper.py -t ghp_xxxx --indian-only
  python scraper.py -t ghp_xxxx -o data/2026-mar-may -c 8
""",
    )
    p.add_argument("--output",       "-o", default="results",  metavar="PREFIX",
                   help="Output file prefix without extension (default: results)")
    p.add_argument("--github-token", "-t", default=None,       metavar="TOKEN",
                   help="GitHub personal access token (strongly recommended)")
    p.add_argument("--indian-only",        action="store_true",
                   help="Only write Indian mentees to the output files")
    p.add_argument("--concurrency",  "-c", type=int, default=5, metavar="N",
                   help="Concurrent LFX API requests (default: 5)")
    p.add_argument("--verbose",      "-v", action="store_true",
                   help="Enable DEBUG-level logging")
    return p


def main() -> None:
    args = _build_parser().parse_args()
    if args.verbose:
        logger.setLevel(logging.DEBUG)

    asyncio.run(run_scraper(
        github_token    = args.github_token,
        output_prefix   = args.output,
        indian_only     = args.indian_only,
        max_concurrency = args.concurrency,
    ))


if __name__ == "__main__":
    main()
