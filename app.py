"""
LFX Mentorship Dashboard
========================
Clean org-by-org view:  Org → Projects → Selected Mentee + Mentors

Run:
    streamlit run app.py
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pandas as pd
import streamlit as st

# ─────────────────────────────────────────────────────────────────────────────
# Page config
# ─────────────────────────────────────────────────────────────────────────────

st.set_page_config(
    page_title="LFX Mentorship Tracker",
    page_icon="🌏",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ─────────────────────────────────────────────────────────────────────────────
# Styling
# ─────────────────────────────────────────────────────────────────────────────

st.markdown("""
<style>
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap');
html, body, [class*="css"] { font-family: 'Inter', sans-serif; }

/* ── Page background ── */
.stApp { background: #0d1117; }

/* ── Sidebar ── */
section[data-testid="stSidebar"] { background: #161b22; border-right: 1px solid #21262d; }

/* ── Org header block ── */
.org-block {
    background: linear-gradient(90deg, #161b22, #1c2128);
    border-left: 3px solid #58a6ff;
    border-radius: 0 8px 8px 0;
    padding: 12px 20px;
    margin: 24px 0 12px;
}
.org-name { font-size: 1.2rem; font-weight: 700; color: #e6edf3; }
.org-meta { font-size: 0.78rem; color: #8b949e; margin-top: 2px; }

/* ── Project card ── */
.proj-card {
    background: #161b22;
    border: 1px solid #21262d;
    border-radius: 10px;
    padding: 18px 22px;
    margin-bottom: 14px;
}
.proj-title { font-size: 1rem; font-weight: 600; color: #79c0ff; margin-bottom: 6px; }

/* ── Mentee row ── */
.mentee-row {
    display: flex;
    align-items: center;
    gap: 10px;
    background: #0d1117;
    border: 1px solid #21262d;
    border-radius: 8px;
    padding: 10px 14px;
    margin-top: 8px;
}
.mentee-name { font-weight: 600; color: #e6edf3; font-size: 0.95rem; }
.mentee-links { font-size: 0.8rem; color: #8b949e; }
.mentee-links a { color: #58a6ff; text-decoration: none; margin-right: 10px; }
.mentee-links a:hover { text-decoration: underline; }

/* ── Badges ── */
.badge {
    display: inline-block;
    border-radius: 999px;
    padding: 2px 10px;
    font-size: 0.7rem;
    font-weight: 600;
    white-space: nowrap;
}
.badge-india     { background:#0d4429; color:#3fb950; border:1px solid #238636; }
.badge-not-india { background:#2d1515; color:#f85149; border:1px solid #6e2020; }
.badge-unknown   { background:#2d2208; color:#d29922; border:1px solid #9e6a03; }
.badge-accepted  { background:#0c2d4a; color:#58a6ff; border:1px solid #1f6feb; }
.badge-graduated { background:#1a2c1a; color:#56d364; border:1px solid #2ea043; }

/* ── Mentor strip ── */
.mentor-strip {
    font-size: 0.78rem;
    color: #8b949e;
    margin-top: 10px;
    padding-top: 8px;
    border-top: 1px solid #21262d;
}
.mentor-strip b { color: #c9d1d9; }

/* ── Links row ── */
.links-row { font-size: 0.78rem; margin-top: 4px; }
.links-row a { color: #58a6ff; text-decoration: none; margin-right: 14px; }
.links-row a:hover { text-decoration: underline; }

/* ── No-mentee note ── */
.no-mentee { font-size: 0.82rem; color: #6e7681; font-style: italic; padding: 6px 0; }
</style>
""", unsafe_allow_html=True)

# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

ISSUE_RE = re.compile(r"github\.com/([\w\-\.]+)/([\w\-\.]+)/issues/(\d+)", re.I)
UUID_RE  = re.compile(r"mentorship\.lfx\.linuxfoundation\.org/projects?/([\w\-]+)", re.I)

def load(path: str) -> pd.DataFrame:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    df   = pd.DataFrame(data)

    # Coerce optional columns
    for col in ("mentee_linkedin", "mentors", "org_name", "project_title"):
        if col not in df.columns:
            df[col] = ""

    # org: prefer org_name from README; fall back to GitHub issue org
    def issue_org(url):
        m = ISSUE_RE.search(str(url))
        return m.group(1) if m else ""

    df["org"] = df.apply(
        lambda r: r["org_name"] if r.get("org_name") else issue_org(r["issue_url"]),
        axis=1,
    )

    # proj_key: prefer project_title; fall back to repo#issue
    def make_proj_key(r):
        if r.get("project_title"):
            return r["project_title"]
        m = ISSUE_RE.search(str(r["issue_url"]))
        return f"{m.group(2)} #{m.group(3)}" if m else r.get("lfx_url", "")

    df["proj_key"] = df.apply(make_proj_key, axis=1)

    df = df.fillna("")
    return df


def india_badge(val) -> str:
    if val is True  or str(val).lower() == "true":
        return '<span class="badge badge-india">🇮🇳 Indian</span>'
    if val is False or str(val).lower() == "false":
        return '<span class="badge badge-not-india">✗ Not Indian</span>'
    return '<span class="badge badge-unknown">? Unknown</span>'


def status_badge(s: str) -> str:
    s = str(s).lower().strip()
    if s in ("accepted", "current"):
        return f'<span class="badge badge-accepted">✓ {s.title()}</span>'
    if s == "graduated":
        return f'<span class="badge badge-graduated">🎓 Graduated</span>'
    return ""


def mentee_card(row: pd.Series) -> str:
    """Render one mentee as an HTML card."""
    name     = row.get("mentee_name", "") or "<em>Unknown</em>"
    github   = row.get("mentee_profile", "")
    linkedin = row.get("mentee_linkedin", "")
    location = row.get("location", "")
    india    = row.get("is_indian", None)

    links = ""
    if github:
        handle = github.replace("https://github.com/", "")
        links += f'<a href="{github}" target="_blank">🐙 @{handle}</a>'
    if linkedin:
        li_handle = linkedin.rstrip("/").split("/")[-1]
        links += f'<a href="{linkedin}" target="_blank">💼 {li_handle}</a>'
    if location:
        links += f'<span style="color:#8b949e">📍 {location}</span>'

    html  = f'<div class="mentee-row">'
    html += f'<div style="flex:1">'
    html += f'  <div class="mentee-name">{name}</div>'
    if links:
        html += f'  <div class="mentee-links">{links}</div>'
    html += f'</div>'
    html += f'<div style="display:flex;gap:6px;align-items:center">'
    html += india_badge(india)
    html += f'</div></div>'
    return html


def project_block(proj_key: str, proj_df: pd.DataFrame) -> str:
    """Render one project card with all its applicants and mentors."""
    row0      = proj_df.iloc[0]
    issue_url = row0["issue_url"]
    lfx_url   = row0["lfx_url"]
    mentors   = row0.get("mentors", "") or ""
    title     = row0.get("project_title", "") or proj_key

    # Selected mentee (one record per project from the scraper)
    mentee_html = ""
    if row0.get("mentee_name"):
        mentee_html = mentee_card(row0)

    if not mentee_html:
        mentee_html = '<div class="no-mentee">⏳ No selection announced yet for this project.</div>'

    # Mentor strip
    mentor_html = ""
    if mentors:
        mentor_html = (
            f'<div class="mentor-strip">'
            f'<b>Mentor{"s" if "," in mentors else ""}:</b> {mentors}'
            f'</div>'
        )

    # Links row
    links = ""
    if issue_url:
        links += f'<a href="{issue_url}" target="_blank">🔗 GitHub Issue</a>'
    links += f'<a href="{lfx_url}" target="_blank">🌐 LFX Project</a>'

    html  = f'<div class="proj-card">'
    html += f'  <div class="proj-title">📦 {title}</div>'
    html += f'  <div class="links-row">{links}</div>'
    html += mentee_html
    html += mentor_html
    html += '</div>'
    return html


# ─────────────────────────────────────────────────────────────────────────────
# Sidebar
# ─────────────────────────────────────────────────────────────────────────────

def sidebar(df: pd.DataFrame):
    with st.sidebar:
        st.markdown("## 🌏 LFX Tracker")
        st.markdown("**CNCF · 2026 Mar–May**")
        st.divider()

        india_only  = st.toggle("🇮🇳 Indian mentees only", value=False)
        name_search = st.text_input("🔍 Search mentee name", placeholder="e.g. Rahul")
        st.divider()

        all_orgs = sorted(df["org"].unique())
        counts   = {o: df[df["org"] == o]["proj_key"].nunique() for o in all_orgs}

        st.markdown("**Organisations**")
        selected = st.multiselect(
            "orgs",
            options=all_orgs,
            default=all_orgs,
            format_func=lambda o: f"{o}  ({counts[o]} project{'s' if counts[o]!=1 else ''})",
            label_visibility="collapsed",
        )

        st.divider()
        n_indian = int(df["is_indian"].eq(True).sum())
        n_total  = len(df[df["mentee_name"] != ""])
        st.caption(f"Total named applicants: **{n_total}**")
        st.caption(f"🇮🇳 Indian detected: **{n_indian}**")

    return selected, india_only, name_search


# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────

def main():
    results_file = "results.json"
    if not Path(results_file).exists():
        st.error(
            "**`results.json` not found.**  \n"
            "Run the scraper first:\n"
            "```bash\npython scraper.py --github-token $GITHUB_TOKEN\n```"
        )
        st.stop()

    df = load(results_file)

    # ── Header ───────────────────────────────────────────────────────────────
    st.markdown("""
    <div style="padding:20px 0 10px">
        <span style="font-size:1.8rem;font-weight:800;color:#e6edf3">
            🌏 LFX Mentorship Tracker
        </span><br>
        <span style="font-size:0.9rem;color:#8b949e">
            CNCF · 2026 Mar–May Cohort &nbsp;·&nbsp;
            Organisation → Project → Mentee
        </span>
    </div>
    """, unsafe_allow_html=True)
    st.divider()

    # ── Sidebar ───────────────────────────────────────────────────────────────
    selected_orgs, india_only, name_search = sidebar(df)

    # ── Filter ───────────────────────────────────────────────────────────────
    view = df[df["org"].isin(selected_orgs)].copy()
    if india_only:
        view = view[view["is_indian"] == True]
    if name_search.strip():
        q    = name_search.strip().lower()
        view = view[view["mentee_name"].str.lower().str.contains(q, na=False)]

    if view.empty:
        st.warning("No results match the current filters.")
        st.stop()

    # ── Org loop ──────────────────────────────────────────────────────────────
    for org in [o for o in selected_orgs if o in view["org"].values]:
        org_df  = view[view["org"] == org]
        n_proj  = org_df["proj_key"].nunique()
        n_india = int(org_df["is_indian"].eq(True).sum())

        # Org header
        st.markdown(
            f'<div class="org-block">'
            f'  <div class="org-name">{org}</div>'
            f'  <div class="org-meta">'
            f'    {n_proj} project{"s" if n_proj!=1 else ""}'
            f'    &nbsp;·&nbsp; {n_india} 🇮🇳 Indian applicants detected'
            f'  </div>'
            f'</div>',
            unsafe_allow_html=True,
        )

        # Projects within this org
        for proj_key, proj_df in org_df.groupby("proj_key"):
            st.markdown(project_block(proj_key, proj_df), unsafe_allow_html=True)


if __name__ == "__main__":
    main()
