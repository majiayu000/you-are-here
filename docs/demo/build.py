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
PMAP = os.path.join(ROOT, "plugins", "you-are-here", "skills", "you-are-here", "scripts", "pmap.py")
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
    "headline": "Search shipped overnight and PDF export is nearly done; sync is stuck on your server-clock call.",
    "milestones": [
        {"name": "Editor core", "status": "done", "date": "Sep 21"},
        {"name": "Search", "status": "done", "date": "Oct 7"},
        {"name": "Public beta", "status": "current", "note": "3 left"},
        {"name": "Sync v2", "status": "future"},
        {"name": "1.0", "status": "future", "proposed": True},
    ],
    "remaining": [
        {"label": "PDF export", "status": "in_progress", "note": "2 files", "part": "export"},
        {"label": "Sync conflicts", "status": "stuck", "note": "server clock", "part": "sync"},
        {"label": "Onboarding", "status": "not_started", "part": "onboarding"},
    ],
    "chain": {
        "nodes": [
            {"id": "clock", "label": "Server clock API", "sub": "your call", "status": "stuck", "bottleneck": True, "part": "sync"},
            {"id": "clockwork", "label": "Implement server clock", "sub": "only if you block on it", "status": "not_started",
             "if": {"decision": "clock", "option": 1}, "part": "sync"},
            {"id": "sync", "label": "Sync conflicts", "sub": "resolver waits on clock", "status": "stuck",
             "if": {"decision": "clock", "option": 1}, "part": "sync"},
            {"id": "pdf", "label": "PDF export", "sub": "2 files left", "status": "in_progress",
             "if": {"decision": "clock", "option": 0}, "part": "export"},
            {"id": "onb", "label": "Onboarding", "sub": "3 screens", "status": "not_started", "part": "onboarding"},
            {"id": "beta", "label": "Public beta", "sub": "tag + release notes", "status": "not_started"},
        ],
        "edges": [
            {"from": "clock", "to": "pdf", "kind": "inferred", "why": "default keeps local clocks and continues PDF export"},
            {"from": "pdf", "to": "onb", "kind": "inferred", "why": "onboarding demo exports a PDF"},
            {"from": "onb", "to": "beta", "kind": "evidence", "why": "ROADMAP.md: beta requires onboarding"},
            {"from": "clock", "to": "clockwork", "kind": "inferred", "why": "only if you choose to block on the server clock"},
            {"from": "clockwork", "to": "sync", "kind": "evidence",
             "why": "`src/sync/conflict.ts` TODO: last-writer-wins needs a trusted server timestamp"},
            {"from": "sync", "to": "onb", "kind": "inferred", "why": "onboarding waits until sync conflicts are decided"},
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
    "next_step": {"title": "Finish PDF export", "owner": "agent", "why": "It does not depend on the sync decision.",
                  "action": "`src/export/pdf.ts` page footers, then `src/export/fonts.ts`", "unblocks": "Onboarding demo"},
    "decisions": [
        {"id": "clock", "question": "Use the server clock for sync conflicts?",
         "default": "keep local clocks, continue on PDF export",
         "options": ["keep local clocks, continue on PDF export", "block on the server clock first"],
         "deadline": "2026-10-08T10:00:00+08:00", "reversible": True},
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
    m["next_step"] = {"title": "Finish full-text search", "owner": "agent", "why": "blocks beta"}
    m["headline"] = "Search is half done; sync and export are moving."
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
    with open(os.path.join(out, "map.json"), "w") as fh:
        json.dump(evening(), fh)
    run("render")
    scratch = os.path.join(repo, "docs", "scratch.md")
    os.makedirs(os.path.dirname(scratch), exist_ok=True)
    with open(scratch, "w") as fh:
        fh.write("not on the map\n")
    sh("git", "-C", repo, "add", ".")
    sh("git", "-C", repo, "commit", "-qm", "docs: scratch note",
       env={**env, "GIT_AUTHOR_DATE": "2026-10-07T07:00:00+08:00", "GIT_COMMITTER_DATE": "2026-10-07T07:00:00+08:00"})
    run("collect", "--no-gh")
    with open(os.path.join(out, "map.json"), "w") as fh:
        json.dump(MORNING, fh)
    run("render")
    shutil.copy(os.path.join(out, "index.html"), os.path.join(HERE, "index.html"))
    build_overview(tmp, repo, out, env)
    chrome = next((c for c in CHROME if c and os.path.exists(c)), None)
    if chrome:
        for page, png, size in (("index.html", "screenshot.png", "1280,1500"), ("all.html", "overview.png", "1000,760")):
            png = os.path.join(ROOT, "docs", png)
            sh(chrome, "--headless=new", "--disable-gpu", "--hide-scrollbars", "--force-device-scale-factor=2",
               "--virtual-time-budget=4000", f"--window-size={size}",
               f"--screenshot={png}", "file://" + os.path.join(HERE, page))
            print(png)
    else:
        print("Chrome not found: open docs/demo/index.html and take the screenshot by hand")
    shutil.rmtree(tmp)


OTHERS = {
    "ferry": {"summary": "HTTP API for booking ferry seats.", "milestone": "v2 pricing",
              "headline": "Payments provider rejected the sandbox keys overnight; refunds and invoices wait on two calls from you.",
              "parts": [("payments", "Payments", "stuck", "new sandbox keys from the provider"),
                        ("pricing", "Dynamic pricing", "in_progress", None), ("api", "Public API", "done", None)],
              "decisions": ["Refund to card or to credit?", "Ship invoices before pricing?"]},
    "orbit": {"summary": "CLI that syncs dotfiles across machines.", "milestone": "1.0",
              "headline": "Windows support merged; only the docs site is left for 1.0.",
              "parts": [("sync", "Sync engine", "done", None), ("win", "Windows support", "done", None),
                        ("docs", "Docs site", "in_progress", None)], "decisions": []},
}


def build_overview(tmp, lumen_repo, lumen_out, env):
    """Three fictional repos under one folder, rendered as the all-projects page (pmap.py index)."""
    root = os.path.join(tmp, "projects")
    shutil.copytree(lumen_repo, os.path.join(root, "lumen"))
    shutil.copytree(lumen_out, os.path.join(root, "lumen", ".project-map"))
    for name, o in OTHERS.items():
        repo = os.path.join(root, name)
        sh("git", "init", "-q", repo)
        with open(os.path.join(repo, "README.md"), "w") as fh:
            fh.write(f"# {name}\n")
        sh("git", "-C", repo, "add", ".")
        sh("git", "-C", repo, "commit", "-qm", "init", env=env)
        m = {"project": {"name": name, "summary": o["summary"]}, "style": {"lang": "en"}, "headline": o["headline"],
             "generated_at": "2026-10-07T06:40:00+08:00",
             "milestones": [{"name": o["milestone"], "status": "current"}],
             "remaining": [{"label": p[1], "status": p[2]} for p in o["parts"] if p[2] != "done"],
             "chain": {"nodes": [{"id": p[0], "label": p[1], "status": p[2], "bottleneck": i == 0}
                                 for i, p in enumerate([p for p in o["parts"] if p[2] != "done"])], "edges": []},
             "parts": [dict({"id": p[0], "name": p[1], "status": p[2], "inferred": True},
                            **({"waiting_on": p[3]} if p[3] else {})) for p in o["parts"]],
             "next_step": {"title": o["parts"][0][1] if o["parts"][0][2] != "done" else "Docs site", "owner": "user" if o["decisions"] else "agent"},
             "decisions": [{"question": q, "default": "wait"} for q in o["decisions"]]}
        sh(sys.executable, PMAP, "collect", repo, "--no-gh", env=env)
        with open(os.path.join(repo, ".project-map", "map.json"), "w") as fh:
            json.dump(m, fh)
        sh(sys.executable, PMAP, "render", repo, env=env)
    sh(sys.executable, PMAP, "index", root, "--out", os.path.join(HERE, "all.html"), "--lang", "en", "--label", "~/code", env=env)


if __name__ == "__main__":
    main()
