#!/usr/bin/env python3
"""Rebuild the README screenshot from a fictional project ("lumen").

  python3 docs/demo/build.py      -> docs/demo/index.html and docs/screenshot.png (if Chrome is found)

Renders twice (an evening map, then a morning map) so the page shows what changed overnight.
"""
import copy
import json
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
PMAP = os.path.join(ROOT, "scripts", "pmap.py")
CHROME = [
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    shutil.which("google-chrome") or "", shutil.which("chromium") or "",
]

COMMITS = [  # (date, subject, file)
    ("2026-10-06T21:40:00+08:00", "feat(editor): live preview for tables", "src/editor/preview.ts"),
    ("2026-10-07T01:15:00+08:00", "feat(search): fuzzy title search", "src/search/index.ts"),
    ("2026-10-07T03:40:00+08:00", "feat(search): full-text index with tokenizer", "src/search/fulltext.ts"),
    ("2026-10-07T05:12:00+08:00", "wip(sync): conflict resolver needs server clock", "src/sync/conflict.ts"),
    ("2026-10-07T06:51:00+08:00", "feat(export): PDF page breaks and headers", "src/export/pdf.ts"),
]

MORNING = {
    "project": {"name": "lumen", "summary": "Local-first Markdown notes with sync and export."},
    "style": {"lang": "en", "theme": "dark", "accent": "#2dd4bf"},
    "generated_at": "2026-10-07T07:12:00+08:00",
    "milestones": [
        {"name": "Editor core", "status": "done", "date": "Sep 21"},
        {"name": "Search", "status": "done", "date": "Oct 7"},
        {"name": "Public beta", "status": "current", "note": "3 left"},
        {"name": "Sync v2", "status": "future"},
        {"name": "1.0", "status": "future", "proposed": True},
    ],
    "remaining": [
        {"label": "PDF export", "status": "in_progress", "note": "2 files"},
        {"label": "Sync conflicts", "status": "stuck", "note": "server clock"},
        {"label": "Onboarding", "status": "not_started"},
    ],
    "chain": {
        "nodes": [
            {"id": "clock", "label": "Server clock API", "sub": "your call", "status": "stuck", "bottleneck": True},
            {"id": "sync", "label": "Sync conflicts", "sub": "resolver waits on clock", "status": "stuck"},
            {"id": "pdf", "label": "PDF export", "sub": "2 files left", "status": "in_progress"},
            {"id": "onb", "label": "Onboarding", "sub": "3 screens", "status": "not_started"},
            {"id": "beta", "label": "Public beta", "sub": "tag + release notes", "status": "not_started"},
        ],
        "edges": [
            {"from": "clock", "to": "sync", "kind": "evidence",
             "why": "`src/sync/conflict.ts` TODO: last-writer-wins needs a trusted server timestamp"},
            {"from": "sync", "to": "pdf", "kind": "inferred", "why": "independent; listed in suggested order"},
            {"from": "pdf", "to": "onb", "kind": "inferred", "why": "onboarding demo exports a PDF"},
            {"from": "onb", "to": "beta", "kind": "evidence", "why": "ROADMAP.md: beta requires onboarding"},
        ],
    },
    "parts": [
        {"id": "editor", "name": "Editor", "status": "done", "summary": "Markdown editing, tables, live preview.",
         "evidence": ["`src/editor/`", "#12"], "paths": ["src/editor"]},
        {"id": "search", "name": "Search", "status": "done", "summary": "Fuzzy titles and full-text index.",
         "progress": {"done": 5, "total": 5, "label": "tasks"}, "evidence": ["#18", "#21"], "paths": ["src/search"]},
        {"id": "sync", "name": "Sync", "status": "stuck", "summary": "Two-device sync works; conflicts don't.",
         "waiting_on": "a decision on the server clock API", "evidence": ["`src/sync/conflict.ts`", "#27"],
         "paths": ["src/sync"]},
        {"id": "export", "name": "PDF export", "status": "in_progress", "summary": "Page breaks and headers done.",
         "progress": {"done": 7, "total": 9, "label": "files"}, "evidence": ["#24"], "paths": ["src/export"]},
        {"id": "onboarding", "name": "Onboarding", "status": "not_started", "summary": "Three intro screens.",
         "evidence": ["ROADMAP.md"], "paths": ["src/onboarding"]},
        {"id": "themes", "name": "Themes", "status": "not_started", "summary": "After beta.", "inferred": True,
         "paths": ["src/themes"]},
    ],
    "next_step": {"title": "Finish PDF export", "why": "2 files left; it does not depend on the sync decision."},
    "decisions": [
        {"question": "Use the server clock for sync conflicts?", "default": "keep local clocks, continue on PDF export",
         "deadline": "10:00"},
    ],
    "uncertain": ["PDF export tested on A4 only."],
    "panels": [
        {"title": "Open issues", "kind": "list", "items": ["#27 sync conflict on simultaneous edits", "#29 PDF footer overlaps"]},
    ],
    "sources": ["README", "ROADMAP.md", "git log"],
}


def evening():
    m = copy.deepcopy(MORNING)
    m["generated_at"] = "2026-10-06T22:30:00+08:00"
    m["milestones"][1].update(status="current", date=None, note="in progress")
    m["milestones"][2].update(status="future", note=None)
    parts = {p["id"]: p for p in m["parts"]}
    parts["search"].update(status="in_progress", progress={"done": 2, "total": 5, "label": "tasks"})
    parts["sync"].update(status="in_progress")
    parts["sync"].pop("waiting_on")
    parts["export"]["progress"] = {"done": 3, "total": 9, "label": "files"}
    m["next_step"] = {"title": "Finish full-text search", "why": "blocks beta"}
    return m


def sh(*cmd, **kw):
    subprocess.run(cmd, check=True, capture_output=True, text=True, **kw)


def main():
    tmp = tempfile.mkdtemp()
    repo, out = os.path.join(tmp, "lumen"), os.path.join(tmp, "out")
    env = {**os.environ, "XDG_CONFIG_HOME": os.path.join(tmp, "cfg"),
           "GIT_AUTHOR_NAME": "demo", "GIT_AUTHOR_EMAIL": "demo@example.com",
           "GIT_COMMITTER_NAME": "demo", "GIT_COMMITTER_EMAIL": "demo@example.com"}
    sh("git", "init", "-q", repo)
    for date, subject, path in COMMITS:
        f = os.path.join(repo, path)
        os.makedirs(os.path.dirname(f), exist_ok=True)
        with open(f, "a") as fh:
            fh.write(subject + "\n")
        sh("git", "-C", repo, "add", ".")
        sh("git", "-C", repo, "commit", "-qm", subject, env={**env, "GIT_AUTHOR_DATE": date, "GIT_COMMITTER_DATE": date})
    run = lambda *a: sh(sys.executable, PMAP, *a, repo, "--out", out, env=env)
    run("collect", "--no-gh")
    for m in (evening(), MORNING):
        with open(os.path.join(out, "map.json"), "w") as fh:
            json.dump(m, fh)
        run("render")
    shutil.copy(os.path.join(out, "index.html"), os.path.join(HERE, "index.html"))
    chrome = next((c for c in CHROME if c and os.path.exists(c)), None)
    if chrome:
        png = os.path.join(ROOT, "docs", "screenshot.png")
        sh(chrome, "--headless=new", "--disable-gpu", "--hide-scrollbars", "--force-device-scale-factor=2",
           "--window-size=1280,1180", f"--screenshot={png}", "file://" + os.path.join(HERE, "index.html"))
        print(png)
    else:
        print("Chrome not found: open docs/demo/index.html and take the screenshot by hand")
    shutil.rmtree(tmp)


if __name__ == "__main__":
    main()
