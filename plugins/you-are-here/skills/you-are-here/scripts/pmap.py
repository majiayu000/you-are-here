#!/usr/bin/env python3
"""you-are-here: collect repo facts, then render an agent-written map.json to one HTML file.

Agent-neutral (Claude Code, Codex, Grok, ...). Python 3.8+ stdlib only.

  pmap.py collect  <repo> [--out DIR] [--no-gh]   -> DIR/facts.json (+ delta since last run)
  pmap.py validate <repo> [--out DIR]             -> check DIR/map.json against the schema
  pmap.py render   <repo> [--out DIR]             -> DIR/index.html, diffed against the last render
  pmap.py status   <repo> [--out DIR] [--json]    -> none | fresh | stale (git only, no network)
  pmap.py auto     [on|off] <repo> [--every MIN]  -> background refreshes from the plugin hooks
  pmap.py summary  <repo>                         -> a few lines about the map, for hooks
  pmap.py index    <folder> [--out FILE]          -> one page over every repo that has a map
  pmap.py style    [--theme dark|light|auto] [--accent '#2dd4bf'] [--lang zh|en]

DIR defaults to <repo>/.project-map (self-ignored via its own .gitignore).
"""
import argparse
import contextlib
import datetime as dt
import fnmatch
import html
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.parse

SCHEMA_VERSION = 1
PART_STATUS = ("done", "in_progress", "not_started", "stuck")
MS_STATUS = ("done", "current", "future")
OWNERS = ("user", "agent")
EVIDENCE_RE = re.compile(r"^(#\d+|[0-9a-f]{7,40}|`[^`\n]+`|run:\d+|https?://\S+|[\w./-]+\.[A-Za-z0-9]{1,6})$")
BUDGET = {"headline": 160, "summary": 160, "sub": 40, "next_step": 90, "uncertain_items": 5, "uncertain": 160}
EDGE_KIND = ("evidence", "inferred")
STYLE_FILE = os.path.join(
    os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config"), "you-are-here", "style.json"
)
DEFAULT_STYLE = {"theme": "dark", "accent": "#2dd4bf", "lang": "zh"}


# ---------------------------------------------------------------- utilities

def now_iso():
    return dt.datetime.now().astimezone().isoformat(timespec="seconds")


def run(cmd, cwd=None, timeout=30, env=None, strip=True):
    """Run a command; return (ok, output). Never raises. Output is stdout on success, stderr otherwise."""
    try:
        p = subprocess.run(cmd, cwd=cwd, capture_output=True, timeout=timeout, env=env)
    except (OSError, subprocess.TimeoutExpired) as e:
        return False, str(e)
    out = (p.stdout if p.returncode == 0 else (p.stderr or p.stdout)).decode("utf-8", "replace")
    return p.returncode == 0, out.strip() if strip else out


GIT_ENV = {**os.environ, "GIT_OPTIONAL_LOCKS": "0", "GIT_TERMINAL_PROMPT": "0"}


def git(repo, *args, timeout=30, strip=True):
    # read-only: no optional index refresh, raw (unquoted) paths
    return run(["git", "-C", repo, "-c", "core.quotepath=off", *args], timeout=timeout, env=GIT_ENV, strip=strip)


def load_json(path, default=None):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def write_text(path, text):
    """Atomic write through a unique temp file, safe against concurrent writers."""
    d = os.path.dirname(path)
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".tmp-", suffix=os.path.basename(path))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
        umask = os.umask(0)
        os.umask(umask)
        os.chmod(tmp, 0o666 & ~umask)  # mkstemp makes 0600; give the map normal file permissions
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise


def dump_json(path, data):
    write_text(path, json.dumps(data, ensure_ascii=False, indent=2) + "\n")


@contextlib.contextmanager
def locked(d, stale=900):
    """One collect/render at a time per output dir. A lock older than `stale` seconds is taken over."""
    lock = os.path.join(d, ".lock")
    for _ in range(2):
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, str(os.getpid()).encode())
            os.close(fd)
            break
        except FileExistsError:
            try:
                age = time.time() - os.path.getmtime(lock)
            except OSError:
                continue
            if age < stale:
                sys.exit(f"pmap: another run is using {d} (lock {int(age)}s old); try again shortly")
            os.remove(lock)
    else:
        sys.exit(f"pmap: could not take the lock in {d}")
    try:
        yield
    finally:
        try:
            os.remove(lock)
        except OSError:
            pass


def out_dir(repo, out):
    d = os.path.abspath(out) if out else os.path.join(repo, ".project-map")
    created = not os.path.exists(d)
    os.makedirs(d, exist_ok=True)
    gi = os.path.join(d, ".gitignore")
    # only self-ignore a folder we own; never drop a catch-all .gitignore into an existing dir
    if (created or not out) and not os.path.exists(gi):
        write_text(gi, "*\n")
    return d


def repo_root(path):
    ok, top = git(os.path.abspath(path), "rev-parse", "--show-toplevel")
    if not ok:
        sys.exit(f"pmap: not a git repository: {path}")
    return top


def parse_remote(url):
    """(host, owner, repo) from https / ssh / scp-style remotes, else None."""
    url = (url or "").strip()
    m = (re.match(r"^[a-z+]+://(?:[^@/]+@)?([^/:]+)(?::\d+)?/(.+?)/([^/]+?)(?:\.git)?/?$", url)
         or re.match(r"^(?:[^@/]+@)?([^/:]+):/?(.+?)/([^/]+?)(?:\.git)?/?$", url))
    return (m.group(1).lower(), m.group(2), m.group(3)) if m else None


def github_remote(url):
    """{'slug', 'web', 'gh'} for GitHub / GitHub Enterprise remotes (incl. ssh host aliases), else None."""
    r = parse_remote(url)
    if not r or "github" not in r[0]:
        return None
    host, owner, repo = r
    if host == "github.com" or "." not in host:  # "." absent: an ssh alias like github-work -> github.com
        return {"slug": f"{owner}/{repo}", "web": f"https://github.com/{owner}/{repo}", "gh": f"{owner}/{repo}"}
    return {"slug": f"{owner}/{repo}", "web": f"https://{host}/{owner}/{repo}", "gh": f"{host}/{owner}/{repo}"}


def github_slug(url):
    g = github_remote(url)
    return g["slug"] if g else None


# ---------------------------------------------------------------- collect

def ref_shas(repo):
    """Full shas of the refs a map covers: HEAD, its upstream, origin/HEAD."""
    out = {}
    for r in ("HEAD", "@{u}", "origin/HEAD"):
        ok, sha = git(repo, "rev-parse", "--verify", "-q", r + "^{commit}")
        if ok and sha:
            out[r] = sha
    return out


def tag_names(repo):
    ok, out = git(repo, "for-each-ref", "--format=%(refname:short)", "refs/tags")
    return sorted(out.splitlines()) if ok and out else []


def worktree(repo):
    """Porcelain status parsed with -z (exact paths), plus an mtime/size fingerprint per path."""
    ok, raw = git(repo, "status", "--porcelain=v1", "-z", "--untracked-files=all", strip=False)
    entries = []
    if ok and raw:
        toks = raw.split("\0")
        i = 0
        while i < len(toks):
            t = toks[i]
            i += 1
            if len(t) < 4:
                continue
            code, path = t[:2], t[3:]
            if code[0] in "RC":  # rename/copy: the next token is the source path
                i += 1
            entries.append((code, path))
    fp = {}
    for code, path in entries[:500]:
        try:
            st = os.stat(os.path.join(repo, path))
            fp[path] = [code, st.st_mtime_ns, st.st_size]
        except OSError:
            fp[path] = [code, None, None]  # a deletion still differs from an absent baseline entry
    summary = {"modified": sum(1 for c, _ in entries if c != "??"),
               "untracked": sum(1 for c, _ in entries if c == "??"),
               "paths": [p for _, p in entries[:60]]}
    return summary, fp


def log_commits(repo, rng, warnings):
    fmt = "%x02%h%x03%cI%x03%D%x03%s"
    ok, log = git(repo, "log", *rng, "--name-only", f"--format={fmt}", timeout=60)
    if not ok:
        warnings.append(f"git log failed: {log[:200]}")
        return []
    commits = []
    for block in log.split("\x02")[1:]:
        head, _, files = block.partition("\n")
        sha, date, decor, subject = (head.split("\x03") + ["", "", "", ""])[:4]
        fl = [x for x in files.splitlines() if x.strip()]
        commits.append({"sha": sha, "date": date, "refs": decor, "subject": subject,
                        "files": fl[:30], "files_total": len(fl)})
    return commits


def new_commit_range(repo, baseline, state, warnings):
    """Revisions for `git log`: everything reachable from the current refs but not from the last map's refs."""
    refs = list(ref_shas(repo).values())
    if not refs:
        return None
    old = [s for s in (baseline.get("refs") or {}).values() if git(repo, "cat-file", "-e", s + "^{commit}")[0]]
    if old:
        return refs + ["--not"] + old
    if state.get("last_render"):
        warnings.append("no usable baseline refs (history rewritten or old map): falling back to commit dates")
        return [f"--since={state['last_render']}"] + refs
    return ["-n", "80"] + refs


def collect_git(repo, baseline, state, incremental, warnings):
    f = {"root": repo, "name": os.path.basename(repo)}
    ok, head = git(repo, "rev-parse", "--short", "HEAD")
    f["head"] = head if ok else None
    ok, branch = git(repo, "symbolic-ref", "--short", "-q", "HEAD")
    f["branch"] = branch if ok else ("(detached)" if f["head"] else None)
    if not f["head"]:
        warnings.append("repository has no commits yet")
    ok, url = git(repo, "remote", "get-url", "origin")
    gr = github_remote(url) if ok else None
    f["github"] = gr["slug"] if gr else None
    f["github_web"] = gr["web"] if gr else None
    f["_gh_repo"] = gr["gh"] if gr else None
    if ok and not gr:
        warnings.append("origin is not a GitHub remote: issues/PRs/CI skipped")
    ok, ab = git(repo, "rev-list", "--left-right", "--count", "HEAD...@{u}")
    if ok and len(ab.split()) == 2:
        a, b = ab.split()
        f["upstream"] = {"ahead": int(a), "behind": int(b)}
    ok, gitdir = git(repo, "rev-parse", "--absolute-git-dir")
    fh = os.path.join(gitdir, "FETCH_HEAD") if ok else ""
    if fh and os.path.exists(fh):
        f["last_fetch"] = dt.datetime.fromtimestamp(os.path.getmtime(fh)).astimezone().isoformat(timespec="minutes")
    ok, shallow = git(repo, "rev-parse", "--is-shallow-repository")
    if ok and shallow == "true":
        warnings.append("shallow clone: history before the graft point is not visible")

    rng = new_commit_range(repo, baseline, state, warnings) if incremental else (
        ["-n", "80"] + list(ref_shas(repo).values()) if f["head"] else None)
    f["commits"] = log_commits(repo, rng, warnings) if rng else []
    f["changed_files"] = sorted({p for c in f["commits"] for p in c["files"]})

    f["worktree"], f["_worktree_fp"] = worktree(repo)
    ok, br = git(repo, "for-each-ref", "--sort=-committerdate", "--count=15",
                 "--format=%(refname:short)\x03%(committerdate:iso-strict)\x03%(upstream:track)", "refs/heads")
    f["branches"] = [dict(zip(("name", "date", "track"), l.split("\x03"))) for l in br.splitlines()] if ok and br else []
    ok, tg = git(repo, "for-each-ref", "--sort=-creatordate", "--count=12",
                 "--format=%(refname:short)\x03%(creatordate:iso-strict)", "refs/tags")
    f["tags"] = [dict(zip(("name", "date"), l.split("\x03"))) for l in tg.splitlines()] if ok and tg else []

    docs = []
    for name in sorted(os.listdir(repo)):
        if re.match(r"(?i)(readme|roadmap|changelog|todo|plan|agents|claude)\b.*\.(md|txt|rst)$", name):
            docs.append(name)
    for d in ("docs", "specs", "rfcs", "design"):
        p = os.path.join(repo, d)
        if os.path.isdir(p):
            docs += [f"{d}/{x}" for x in sorted(os.listdir(p))[:25]]
    f["doc_hints"] = docs
    return f


SKIP_DIRS = {".git", "node_modules", "vendor", "dist", "build", "target", ".venv", "venv", "__pycache__",
             ".project-map", ".next", "coverage"}
CHECKBOX = re.compile(r"^\s*[-*+]\s+\[([ xX])\]\s+(.*\S)")
TASK_HEADING = re.compile(r"^#{2,4}\s+((?:[A-Z][\w-]*-)?T\d+\b.*|Task\s+\d+.*|\d+[.)]\s+.*)$")


def plan_kind(rel):
    low = rel.lower()
    name = os.path.basename(low)
    if low.startswith(".kiro/specs/") and name == "tasks.md":
        return "kiro"
    if low.startswith("specs/") and name == "tasks.md":
        return "spec-kit"
    if name in ("plans.md", "plan.md", "execplan.md") or "/exec-plans/" in "/" + low or low.startswith(".agent/"):
        return "exec-plan"
    if name in ("task_plan.md", "progress.md"):
        return "planning-with-files"
    if name in ("todo.md", "roadmap.md", "tasks.md", "milestones.md", "checklist.md"):
        return "checklist"
    return None


def checklist(path):
    """(done, total, open items). Without checkboxes, task headings count and done is None (tracked elsewhere)."""
    done, total, open_items, headings = 0, 0, [], []
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                mm = CHECKBOX.match(line)
                if not mm:
                    hh = TASK_HEADING.match(line.rstrip())
                    if hh:
                        headings.append(hh.group(1)[:100])
                    continue
                total += 1
                if mm.group(1) in "xX":
                    done += 1
                elif len(open_items) < 8:
                    open_items.append(mm.group(2)[:100])
    except OSError:
        return None
    if not total and headings:
        return None, len(headings), headings[:8]
    return done, total, open_items


def taskmaster(path):
    data = load_json(path)
    if isinstance(data, dict) and "tasks" not in data:  # tagged format: {"master": {"tasks": [...]}}
        data = next((v for v in data.values() if isinstance(v, dict) and isinstance(v.get("tasks"), list)), {})
    tasks = (data or {}).get("tasks") if isinstance(data, dict) else None
    if not isinstance(tasks, list):
        return None
    tasks = [t for t in tasks if isinstance(t, dict)]
    done = sum(1 for t in tasks if str(t.get("status")).lower() in ("done", "completed"))
    open_items = [f"#{t.get('id')} {str(t.get('title'))[:90]} ({t.get('status')})" for t in tasks
                  if str(t.get("status")).lower() not in ("done", "completed", "cancelled")][:8]
    return done, len(tasks), open_items


def collect_plans(repo):
    """Plan files the project already keeps (spec-kit, Kiro, Taskmaster, ExecPlans, checklists)."""
    plans = []
    tm = os.path.join(repo, ".taskmaster", "tasks", "tasks.json")
    if os.path.isfile(tm):
        r = taskmaster(tm)
        if r:
            plans.append({"source": "taskmaster", "path": ".taskmaster/tasks/tasks.json",
                          "done": r[0], "total": r[1], "open": r[2]})
    for root, dirs, files in os.walk(repo):
        rel_root = os.path.relpath(root, repo)
        depth = 0 if rel_root == "." else rel_root.count(os.sep) + 1
        dirs[:] = [x for x in sorted(dirs) if x not in SKIP_DIRS and (not x.startswith(".") or x in (".kiro", ".agent"))]
        if depth >= 4:
            dirs[:] = []
        for name in sorted(files):
            if not name.lower().endswith(".md"):
                continue
            rel = name if rel_root == "." else os.path.join(rel_root, name).replace(os.sep, "/")
            kind = plan_kind(rel)
            if not kind:
                continue
            full = os.path.join(root, name)
            r = checklist(full)
            if r and r[1]:
                plans.append({"source": kind, "path": rel, "done": r[0], "total": r[1], "open": r[2],
                              "modified": dt.datetime.fromtimestamp(os.path.getmtime(full)).astimezone()
                              .isoformat(timespec="minutes")})
    plans.sort(key=lambda p: p.get("modified") or "", reverse=True)  # active plans first
    plans = plans[:30]
    for p in plans:
        if p["done"] is None:
            p["note"] = "no checkboxes: tasks listed as headings, completion tracked elsewhere"
    return plans


def plan_delta(old, new):
    o = {p["path"]: p for p in old or []}
    out = []
    for p in new:
        q = o.get(p["path"])
        if not q:
            out.append(f"{p['path']}: new plan {p['done']}/{p['total']}")
        elif (q["done"], q["total"]) != (p["done"], p["total"]):
            out.append(f"{p['path']}: {q['done']}/{q['total']} -> {p['done']}/{p['total']}")
    return out


def collect_github(repo_arg, warnings):
    if not shutil.which("gh"):
        warnings.append("gh not installed: issues/PRs/CI skipped")
        return None

    def gh_json(args, what, timeout=20):
        ok, out = run(["gh", *args], timeout=timeout)
        if not ok and not re.search(r"auth|not found|could not resolve to a repository|HTTP 40[134]", out, re.I):
            time.sleep(1.5)  # transient network errors (EOF, timeouts) usually pass on a second try
            ok, out = run(["gh", *args], timeout=timeout)
        if not ok:
            warnings.append(f"gh {what} failed: {out[:200]}")
            return None
        try:
            return json.loads(out)
        except ValueError:
            warnings.append(f"gh {what}: unreadable output")
            return None

    R = ["-R", repo_arg]
    g = {}
    info = gh_json(["repo", "view", repo_arg, "--json", "defaultBranchRef"], "repo view")
    default = ((info or {}).get("defaultBranchRef") or {}).get("name")
    g["default_branch"] = default
    issues = gh_json(["issue", "list", *R, "--state", "open", "--limit", "30",
                      "--json", "number,title,labels,updatedAt"], "issue list")
    if issues is not None:
        g["issues"] = [{"number": i["number"], "title": i["title"], "updated": i["updatedAt"],
                        "labels": [l["name"] for l in i.get("labels") or []]} for i in issues]
    prs = gh_json(["pr", "list", *R, "--state", "open", "--limit", "20", "--json",
                   "number,title,headRefName,isDraft,reviewDecision,mergeStateStatus,statusCheckRollup,updatedAt"],
                  "pr list", timeout=25)
    if prs is not None:
        g["prs"] = []
        for p in prs:
            checks = {"pass": 0, "fail": 0, "pending": 0}
            for c in p.get("statusCheckRollup") or []:
                v = (c.get("conclusion") or c.get("state") or c.get("status") or "").upper()
                if v in ("SUCCESS", "NEUTRAL", "SKIPPED"):
                    checks["pass"] += 1
                elif v in ("FAILURE", "ERROR", "CANCELLED", "TIMED_OUT", "ACTION_REQUIRED"):
                    checks["fail"] += 1
                else:
                    checks["pending"] += 1
            g["prs"].append({"number": p["number"], "title": p["title"], "branch": p["headRefName"],
                             "draft": p["isDraft"], "review": p.get("reviewDecision") or None,
                             "merge_state": p.get("mergeStateStatus"), "checks": checks, "updated": p["updatedAt"]})
    if default:
        runs = gh_json(["run", "list", *R, "--branch", default, "--limit", "6", "--json",
                        "databaseId,workflowName,status,conclusion,createdAt,headSha,event"], "run list")
        if runs is not None:
            g["default_branch_runs"] = [{"id": r["databaseId"], "workflow": r["workflowName"],
                                         "status": r["status"], "conclusion": r["conclusion"],
                                         "created": r["createdAt"], "sha": r["headSha"][:7], "event": r["event"]}
                                        for r in runs]
    return g


def github_delta(old, new):
    """Compare only GitHub collections that succeeded in both snapshots."""
    comparable = {k for k in ("prs", "issues", "default_branch_runs")
                  if old.get(k) is not None and new.get(k) is not None}
    if not comparable:
        return "unknown (no comparable GitHub collections)"
    out = {}
    if "prs" in comparable:
        op = {p["number"]: p for p in old["prs"]}
        np_ = {p["number"]: p for p in new["prs"]}
        out["prs_opened"] = [f"#{n} {np_[n]['title']}" for n in sorted(np_.keys() - op.keys())]
        out["prs_gone"] = [f"#{n} {op[n]['title']} (merged or closed)" for n in sorted(op.keys() - np_.keys())]
        changed = []
        for n in sorted(np_.keys() & op.keys()):
            a, b = op[n], np_[n]
            bits = [f"{k}: {a.get(k)} -> {b.get(k)}" for k in ("checks", "review", "merge_state", "draft") if a.get(k) != b.get(k)]
            if bits:
                changed.append(f"#{n}: " + "; ".join(bits))
        out["prs_changed"] = changed
    if "issues" in comparable:
        oi = {i["number"] for i in old["issues"]}
        ni = {i["number"]: i for i in new["issues"]}
        out["issues_opened"] = [f"#{n} {ni[n]['title']}" for n in sorted(ni.keys() - oi)]
        out["issues_closed"] = [f"#{n}" for n in sorted(oi - ni.keys())]
    if "default_branch_runs" in comparable:
        seen = {r["id"] for r in old["default_branch_runs"]}
        out["new_ci_runs"] = [f"{r['workflow']} {r['sha']}: {r['conclusion'] or r['status']}"
                              for r in new["default_branch_runs"] if r["id"] not in seen]
    return {k: v for k, v in out.items() if v} or "no changes"


def clean_path(x):
    x = x.strip()
    while x.startswith("./"):
        x = x[2:]
    return x.rstrip("/")


def glob_regex(g):
    """Path glob: `*` and `?` stay inside one segment, `**` crosses segments, `[...]` is a class."""
    out, i = [], 0
    while i < len(g):
        c = g[i]
        if g.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif g.startswith("**", i):
            out.append(".*")
            i += 2
        elif c == "*":
            out.append("[^/]*")
            i += 1
        elif c == "?":
            out.append("[^/]")
            i += 1
        elif c == "[":
            j = g.find("]", i + 1)
            if j < 0:
                out.append(re.escape(c))
                i += 1
            else:
                out.append("[" + g[i + 1:j].replace("\\", "\\\\") + "]")
                i = j + 1
        else:
            out.append(re.escape(c))
            i += 1
    return re.compile("".join(out) + "(?:/.*)?$")  # a glob naming a directory covers what is inside


def glob_base(g):
    """The literal directory a glob starts in (`src/sync/**/*.ts` -> `src/sync`)."""
    segs = []
    for seg in g.split("/"):
        if any(c in seg for c in "*?["):
            break
        segs.append(seg)
    return "/".join(segs)


def path_matcher(patterns):
    """parts[].paths entries: a directory/file prefix, or a glob (`*`, `?`, `[`, `**`). Leading ./ is ignored."""
    pre, globs = [], []
    for x in patterns or []:
        if not isinstance(x, str) or not x.strip():
            continue
        x = clean_path(x)
        if any(c in x for c in "*?["):
            globs.append(glob_regex(x))
        elif x:
            pre.append(x)

    def match(f):
        return any(f == x or f.startswith(x + "/") for x in pre) or any(g.match(f) for g in globs)
    return match


def touched_parts(prev_map, changed_files):
    """Map changed files onto parts[].paths of the previous map, so the agent can skip untouched parts."""
    parts = [(p.get("id"), path_matcher(p.get("paths"))) for p in (prev_map or {}).get("parts") or []
             if isinstance(p, dict)]
    hits, unmapped = {}, []
    for f in changed_files:
        owners = [pid for pid, m in parts if m(f)]
        for pid in owners:
            hits[pid] = hits.get(pid, 0) + 1
        if not owners:
            unmapped.append(f)
    return hits, unmapped


def cmd_collect(a):
    repo = repo_root(a.repo)
    d = out_dir(repo, a.out)
    with locked(d):
        collect(repo, d, a.no_gh)


def collect(repo, d, no_gh):
    state = load_json(os.path.join(d, "state.json"), {}) or {}
    # compare against the map that was actually rendered, not a half-written map.json
    prev_map = load_json(state["last_map"]) if state.get("last_map") else None
    baseline = load_json(os.path.join(d, "baseline.json"), {}) or {}
    warnings = []
    incremental = bool(prev_map and state.get("last_render"))
    facts = {"schema": SCHEMA_VERSION, "collected_at": now_iso(),
             "mode": "incremental" if incremental else "initial",
             "since": state.get("last_render") if incremental else None}
    facts["git"] = g = collect_git(repo, baseline, state, incremental, warnings)
    g["collected_at"] = now_iso()
    gh_repo = g.pop("_gh_repo")
    if gh_repo and not no_gh:
        facts["github"] = collect_github(gh_repo, warnings)
        if any(v is not None for v in (facts["github"] or {}).values()):  # stamp only a collection that got something
            facts["github"]["collected_at"] = now_iso()
    fp = g.pop("_worktree_fp")
    tags = tag_names(repo)
    facts["plans"] = collect_plans(repo)
    since_render = state.get("last_render")
    facts["answers"] = [x for x in load_answers(d) if not since_render or (x.get("at") or "") >= since_render]
    if incremental:
        old_fp = baseline.get("worktree_fp")
        if old_fp is not None:
            wt = sorted(p for p in fp.keys() | old_fp.keys() if fp.get(p) != old_fp.get(p))
        else:
            wt = None
        hits, unmapped = touched_parts(prev_map, sorted(set(g["changed_files"]) | set(wt or [])))
        facts["delta"] = {"touched_parts": hits, "unmapped_files": unmapped[:80],
                          "worktree_changed": wt[:80] if wt is not None else "unknown (no baseline yet)",
                          "new_tags": sorted(set(tags) - set(baseline.get("tags") or [])) if "tags" in baseline else [],
                          "untouched_parts": [p.get("id") for p in prev_map.get("parts") or []
                                              if isinstance(p, dict) and p.get("id") not in hits]}
        if facts.get("github"):
            facts["delta"]["github"] = github_delta(baseline.get("github") or {}, facts["github"])
        if "plans" in baseline:
            facts["delta"]["plans"] = plan_delta(baseline["plans"], facts["plans"])
    # the baseline the *next* run compares against; promoted to baseline.json when this map is rendered
    # Keep each last successful GitHub collection through failures/skips, but never copy stale
    # data into current facts. An explicit empty list is successful and replaces the old list.
    github_baseline = dict(baseline.get("github") or {})
    github_baseline.update({k: v for k, v in (facts.get("github") or {}).items() if v is not None})
    dump_json(os.path.join(d, "pending_baseline.json"),
              {"refs": ref_shas(repo), "tags": tags, "worktree_fp": fp, "github": github_baseline or None,
               "plans": [{k: p[k] for k in ("path", "done", "total")} for p in facts["plans"]]})
    facts["warnings"] = warnings
    dump_json(os.path.join(d, "facts.json"), facts)
    state["last_collect"] = facts["collected_at"]
    state["repo"] = repo
    dump_json(os.path.join(d, "state.json"), state)

    print(f"mode: {facts['mode']}" + (f" (since {facts['since']})" if facts["since"] else ""))
    print(f"commits: {len(g['commits'])}, changed files: {len(g['changed_files'])}, "
          f"worktree: {g['worktree']['modified']} modified / {g['worktree']['untracked']} untracked")
    if "delta" in facts:
        dl = facts["delta"]
        wt = dl["worktree_changed"]
        print(f"touched parts: {dl['touched_parts'] or 'none'}; unmapped files: {len(dl['unmapped_files'])}; "
              f"worktree changed: {len(wt) if isinstance(wt, list) else wt}"
              + (f"; new tags: {', '.join(dl['new_tags'])}" if dl["new_tags"] else ""))
        if "github" in dl:
            print(f"github: {json.dumps(dl['github'], ensure_ascii=False)[:400]}")
        if dl.get("plans"):
            print("plans: " + "; ".join(dl["plans"])[:400])
    if facts["answers"]:
        print("user answers to apply: " + "; ".join(f"{x['question']} -> {x['answer']}" for x in facts["answers"])[:400])
    if facts["plans"]:
        print("plan files: " + ", ".join(f"{p['path']} {'?' if p['done'] is None else p['done']}/{p['total']}"
                                         for p in facts["plans"][:8]) + (" …" if len(facts["plans"]) > 8 else ""))
    for w in warnings:
        print(f"warning: {w}")
    print(f"facts: {os.path.join(d, 'facts.json')}")
    print(f"next: write {os.path.join(d, 'map.json')} (see references/schema.md), then run render")


# ---------------------------------------------------------------- validate

def validate(m):
    errs = []

    def is_str(v):
        return isinstance(v, str) and v.strip() != ""

    def need(obj, key, where, typ=str):
        v = obj.get(key)
        if not isinstance(v, typ) or (typ is str and not v.strip()):
            errs.append(f"{where}.{key}: required {typ.__name__}")

    def opt(obj, key, where, typ):
        v = obj.get(key)
        if v is not None and not isinstance(v, typ):
            errs.append(f"{where}.{key}: must be {typ.__name__ if not isinstance(typ, tuple) else '/'.join(t.__name__ for t in typ)}")
            return False
        return True

    def str_list(obj, key, where):
        v = obj.get(key)
        if v is not None and not (isinstance(v, list) and all(is_str(x) for x in v)):
            errs.append(f"{where}.{key}: must be a list of non-empty strings")

    def dicts(obj, key, where):
        """A list of objects at obj[key] (missing/null -> []); reports type errors."""
        v = obj.get(key)
        if v is None:
            return []
        if not isinstance(v, list):
            errs.append(f"{where}{key}: must be a list")
            return []
        good = []
        for i, x in enumerate(v):
            if isinstance(x, dict):
                good.append((i, x))
            else:
                errs.append(f"{where}{key}[{i}]: must be an object")
        return good

    if not isinstance(m, dict):
        return ["map.json must be an object"]
    need(m, "project", "", dict)
    if isinstance(m.get("project"), dict):
        need(m["project"], "name", "project")
        opt(m["project"], "summary", "project", str)
    for key in ("style",):
        opt(m, key, "", dict)
    for key in ("generated_at", "lang"):
        opt(m, key, "", str)
    str_list(m, "uncertain", "")
    str_list(m, "sources", "")

    ids = set()
    parts = dicts(m, "parts", "")
    for i, p in parts:
        w = f"parts[{i}]"
        need(p, "id", w)
        need(p, "name", w)
        for k in ("summary", "waiting_on", "open"):
            opt(p, k, w, str)
        if p.get("status") not in PART_STATUS:
            errs.append(f"{w}.status: one of {PART_STATUS}")
        if p.get("status") == "stuck" and not is_str(p.get("waiting_on")):
            errs.append(f"{w}.waiting_on: required when status is stuck")
        str_list(p, "evidence", w)
        str_list(p, "paths", w)
        opt(p, "inferred", w, bool)
        if not p.get("evidence") and p.get("inferred") is not True:
            errs.append(f"{w}: give evidence[] or mark inferred: true")
        pr = p.get("progress")
        if pr is not None and not (isinstance(pr, dict) and type(pr.get("done")) is int
                                   and type(pr.get("total")) is int and pr["total"] > 0
                                   and 0 <= pr["done"] <= pr["total"] and isinstance(pr.get("label", ""), str)):
            errs.append(f"{w}.progress: {{done:int 0..total, total:int>0, label?:str}}")
        if isinstance(p.get("id"), str) and p["id"] in ids:
            errs.append(f"{w}.id: duplicate {p['id']}")
        if isinstance(p.get("id"), str):
            ids.add(p["id"])
    if not parts:
        errs.append("parts: at least one part")

    cur = 0
    ms = dicts(m, "milestones", "")
    for i, s in ms:
        w = f"milestones[{i}]"
        need(s, "name", w)
        for k in ("date", "note"):
            opt(s, k, w, str)
        opt(s, "proposed", w, bool)
        if s.get("status") not in MS_STATUS:
            errs.append(f"{w}.status: one of {MS_STATUS}")
        cur += s.get("status") == "current"
    if cur > 1:
        errs.append("milestones: at most one may be status current")
    remaining_ids = set()
    for i, r in dicts(m, "remaining", ""):
        need(r, "id", f"remaining[{i}]")
        rid = r.get("id")
        if isinstance(rid, str):
            if rid in remaining_ids:
                errs.append(f"remaining[{i}].id: duplicate {rid}")
            remaining_ids.add(rid)
        need(r, "label", f"remaining[{i}]")
        opt(r, "note", f"remaining[{i}]", str)
        if r.get("status") not in PART_STATUS:
            errs.append(f"remaining[{i}].status: one of {PART_STATUS}")
        if r.get("part") is not None and not (isinstance(r.get("part"), str) and r["part"] in ids):
            errs.append(f"remaining[{i}].part: unknown part id")

    opt(m, "headline", "", str)
    if isinstance(m.get("next_step"), dict):
        ns = m["next_step"]
        need(ns, "title", "next_step")
        for k in ("why", "action", "unblocks"):
            opt(ns, k, "next_step", str)
        if ns.get("owner") is not None and ns.get("owner") not in OWNERS:
            errs.append(f"next_step.owner: one of {OWNERS}")
    else:
        errs.append("next_step: required {title, why}")
    dec_opts = {}
    for i, q in dicts(m, "decisions", ""):
        w = f"decisions[{i}]"
        need(q, "question", w)
        need(q, "default", w)
        opt(q, "deadline", w, str)
        opt(q, "proposed", w, bool)
        opt(q, "reversible", w, bool)
        opts = q.get("options")
        if opts is not None:
            if not isinstance(opts, list) or not opts or not all(is_str(x) for x in opts):
                errs.append(f"{w}.options: non-empty list of strings")
                opts = None
            elif q.get("default") not in opts:
                errs.append(f"{w}.default: must be one of options")
        # a default that goes ahead on its own is only allowed for reversible, local choices
        if q.get("deadline") and q.get("reversible") is not True:
            errs.append(f"{w}.deadline: only for reversible choices; set reversible: true, or drop the "
                        f"deadline so it waits for the user (push, publish, merge, delete, spend...)")
        if q.get("id") is not None:
            if not is_str(q.get("id")):
                errs.append(f"{w}.id: must be a non-empty string")
            elif q["id"] in dec_opts:
                errs.append(f"{w}.id: duplicate {q['id']}")
            else:
                dec_opts[q["id"]] = opts or []

    if opt(m, "chain", "", dict):
        ch = m.get("chain") or {}
        opt(ch, "title", "chain", str)
        nids = set()
        for i, n in dicts(ch, "nodes", "chain."):
            w = f"chain.nodes[{i}]"
            need(n, "id", w)
            need(n, "label", w)
            opt(n, "sub", w, str)
            opt(n, "bottleneck", w, bool)
            if n.get("status") is not None and n.get("status") not in PART_STATUS:
                errs.append(f"{w}.status: one of {PART_STATUS}")
            if n.get("part") is not None and not (isinstance(n.get("part"), str) and n["part"] in ids):
                errs.append(f"{w}.part: unknown part id")
            iff = n.get("if")
            if iff is not None:
                if not (isinstance(iff, dict) and is_str(iff.get("decision"))
                        and type(iff.get("option")) is int):
                    errs.append(f"{w}.if: {{decision, option: int}}")
                elif iff["decision"] not in dec_opts:
                    errs.append(f"{w}.if.decision: unknown decision id")
                elif not dec_opts[iff["decision"]]:
                    errs.append(f"{w}.if.decision: that decision needs options")
                elif not 0 <= iff["option"] < len(dec_opts[iff["decision"]]):
                    errs.append(f"{w}.if.option: out of range")
            if isinstance(n.get("id"), str):
                nids.add(n["id"])
        for i, e in dicts(ch, "edges", "chain."):
            w = f"chain.edges[{i}]"
            if not all(isinstance(e.get(k), str) and e[k] in nids for k in ("from", "to")):
                errs.append(f"{w}: from/to must be node ids")
            if e.get("kind") not in EDGE_KIND:
                errs.append(f"{w}.kind: one of {EDGE_KIND}")
            opt(e, "why", w, str)
            if e.get("kind") == "evidence" and not is_str(e.get("why")):
                errs.append(f"{w}.why: required for evidence edges")

    for i, p in dicts(m, "panels", ""):
        w = f"panels[{i}]"
        need(p, "title", w)
        opt(p, "hint", w, str)
        kind = p.get("kind")
        if kind not in ("table", "list", "text"):
            errs.append(f"{w}.kind: table|list|text")
        elif kind == "table":
            if not (isinstance(p.get("columns", []), list) and isinstance(p.get("rows", []), list)
                    and all(isinstance(r, list) for r in p.get("rows", []))):
                errs.append(f"{w}: table needs columns[] and rows[[...]]")
        elif kind == "list" and not isinstance(p.get("items", []), list):
            errs.append(f"{w}.items: must be a list")
        elif kind == "text":
            opt(p, "text", w, str)
    return errs


def main_path(m):
    """Chain nodes on the path the page draws by default (fork nodes of non-default options removed)."""
    nodes = (m.get("chain") or {}).get("nodes") or []
    q = fork_decision(m)
    return nodes_for(nodes, q["id"], q["options"].index(q["default"])) if q else nodes


def lint(m):
    """Spec checks that keep maps consistent across agents. Warnings only; run after validate passes."""
    w = []
    parts = m.get("parts") or []
    # bottleneck = the earliest node on the main path that is not done yet
    path = main_path(m)
    bns = [n for n in (m.get("chain") or {}).get("nodes") or [] if n.get("bottleneck")]
    if len(bns) > 1:
        w.append("chain: more than one bottleneck; mark only the earliest node that is not done")
    first_open = next((n for n in path if n.get("status") != "done"), None)
    if bns and first_open and bns[0] is not first_open and bns[0] in path:
        w.append(f"chain: bottleneck is '{bns[0]['label']}' but the earliest open node on the path is "
                 f"'{first_open['label']}'; nodes run left to right in the order they must finish")
    order = {n["id"]: i for i, n in enumerate(path)}
    for e in (m.get("chain") or {}).get("edges") or []:
        a, b = order.get(e.get("from")), order.get(e.get("to"))
        if a is not None and b is not None and a > b:
            w.append(f"chain.edges {e['from']}->{e['to']}: points backwards; from must finish before to")
    # parts: one owner per path, evidence in a checkable form, done means done
    owners = []
    for p in parts:
        for x in p.get("paths") or []:
            x = clean_path(x)
            owners.append((x, glob_base(x), any(c in x for c in "*?["), p["id"]))
    seen_pairs = set()
    for x, bx, gx, a in owners:
        for y, by, gy, b in owners:
            if a >= b or (a, b) in seen_pairs or not bx or not by:
                continue
            # literal paths: one contains the other. With a glob: the other's base lies inside its base
            # and the glob could reach it. Two globs in the same base are reported, to be safe.
            hit = (bx == by or by.startswith(bx + "/") and (not gx or glob_regex(x).match(by) or "**" in x)
                   or bx.startswith(by + "/") and (not gy or glob_regex(y).match(bx) or "**" in y))
            if not gx and not gy:
                hit = x == y or x.startswith(y + "/") or y.startswith(x + "/")
            if hit:
                seen_pairs.add((a, b))
                w.append(f"parts {a} and {b}: paths overlap ({x} / {y}); give each file one owner")
    for p in parts:
        for ev in p.get("evidence") or []:
            if not EVIDENCE_RE.match(ev.strip()):
                w.append(f"parts[{p['id']}].evidence '{ev[:40]}': use #123, a short sha, `path`, run:ID or a URL")
        pr = p.get("progress") or {}
        if pr and pr.get("done") == pr.get("total") and p.get("status") != "done" and not p.get("open"):
            w.append(f"parts[{p['id']}]: progress is {pr['done']}/{pr['total']} but status is {p.get('status')}; "
                     "set `open` to what is still left (e.g. not merged, not verified)")
        if p.get("status") == "done" and pr and pr.get("done", 0) < pr.get("total", 0):
            w.append(f"parts[{p['id']}]: done but progress {pr['done']}/{pr['total']}")
        if len(p.get("summary") or "") > BUDGET["summary"]:
            w.append(f"parts[{p['id']}].summary: over {BUDGET['summary']} chars; move detail to panels")
    for s in m.get("milestones") or []:
        if s.get("proposed") and s.get("status") == "done":
            w.append(f"milestones '{s['name']}': proposed applies to current/future milestones, not history")
    # text budget for a 30-second morning read
    if len(m.get("headline") or "") > BUDGET["headline"]:
        w.append(f"headline: over {BUDGET['headline']} chars")
    if not m.get("headline"):
        w.append("headline: missing; one sentence on what changed and what needs the user")
    ns = m.get("next_step") or {}
    if len(ns.get("title") or "") > BUDGET["next_step"]:
        w.append(f"next_step.title: over {BUDGET['next_step']} chars")
    if not ns.get("owner"):
        w.append("next_step.owner: say who does it (user or agent)")
    for n in (m.get("chain") or {}).get("nodes") or []:
        if len(n.get("sub") or "") > BUDGET["sub"]:
            w.append(f"chain node '{n['label']}'.sub: over {BUDGET['sub']} chars")
    unc = m.get("uncertain") or []
    if len(unc) > BUDGET["uncertain_items"]:
        w.append(f"uncertain: {len(unc)} items; keep the {BUDGET['uncertain_items']} that change a decision")
    for u in unc:
        if len(u) > BUDGET["uncertain"]:
            w.append(f"uncertain '{u[:30]}…': over {BUDGET['uncertain']} chars")
    return w


def cmd_validate(a):
    repo = repo_root(a.repo)
    d = out_dir(repo, a.out)
    m = load_json(os.path.join(d, "map.json"))
    if m is None:
        sys.exit(f"pmap: missing or invalid JSON: {os.path.join(d, 'map.json')}")
    errs = validate(m)
    for e in errs:
        print(f"error: {e}")
    if errs:
        sys.exit(1)
    for x in lint(m):
        print(f"warning: {x}")
    print("map.json ok")


# ---------------------------------------------------------------- diff

def diff_maps(prev, cur):
    """Return {part_id: change-text} plus a flat list of change lines."""
    if not prev:
        return {}, []
    L = labels(cur)
    old = {p["id"]: p for p in (prev.get("parts") or [])}
    marks, lines = {}, []
    for p in (cur.get("parts") or []):
        o = old.get(p["id"])
        if not o:
            marks[p["id"]] = L["new"]
            lines.append(("new", f"{p['name']}: {L['new']}"))
            continue
        bits = []
        if o.get("status") != p.get("status"):
            bits.append(f"{L[o.get('status')]} → {L[p.get('status')]}")
        op, np_ = o.get("progress") or {}, p.get("progress") or {}
        if op and np_ and op.get("label", "") != np_.get("label", ""):
            bits.append(f"{L['scope_changed']}: {op['done']}/{op['total']} {op.get('label', '')} → {np_['done']}/{np_['total']} {np_.get('label', '')}")
        elif np_ and (op.get("done"), op.get("total")) != (np_.get("done"), np_.get("total")):
            bits.append(f"{op.get('done', '?')}/{op.get('total', '?')} → {np_['done']}/{np_['total']}")
        if bits:
            marks[p["id"]] = " · ".join(bits)
            kind = p["status"] if o.get("status") != p["status"] else "in_progress"
            lines.append((kind, f"{p['name']}: {' · '.join(bits)}"))
    for pid, o in old.items():
        if pid not in {p["id"] for p in (cur.get("parts") or [])}:
            lines.append(("not_started", f"{o['name']}: {L['removed']}"))
    om = {s["name"]: s.get("status") for s in (prev.get("milestones") or [])}
    for s in (cur.get("milestones") or []):
        if s["name"] in om and om[s["name"]] != s.get("status"):
            kind = "done" if s["status"] == "done" else "in_progress"
            lines.append((kind, f"{L['milestone']} {s['name']}: {L['ms_' + om[s['name']]]} → {L['ms_' + s['status']]}"))
    def bottleneck(m):
        return next((n["label"] for n in ((m.get("chain") or {}).get("nodes") or []) if n.get("bottleneck")), None)
    ob, nb = bottleneck(prev), bottleneck(cur)
    if ob != nb:
        lines.append(("stuck" if nb else "in_progress", f"{L['bottleneck']}: {ob or '—'} → {nb or '—'}"))
    onodes = {n["id"]: n for n in ((prev.get("chain") or {}).get("nodes") or [])}
    for n in ((cur.get("chain") or {}).get("nodes") or []):
        o = onodes.get(n["id"])
        if o and o.get("status") != n.get("status") and n.get("status") in PART_STATUS and o.get("status") in PART_STATUS:
            lines.append((n["status"], f"{n['label']}: {L[o['status']]} → {L[n['status']]}"))
    # Only identified items provide comparable evidence; never infer completion from absence.
    orem = {r["id"]: r for r in (prev.get("remaining") or []) if r.get("id")}
    nrem = {r["id"]: r for r in (cur.get("remaining") or []) if r.get("id")}
    for rid, old_item in orem.items():
        if rid not in nrem:
            lines.append(("not_started", f"{L['remaining']}: {old_item['label']}: {L['removed']}"))
    for rid, item in nrem.items():
        old_item = orem.get(rid)
        if old_item is None:
            lines.append(("new", f"{L['remaining']}: + {item['label']}"))
        elif old_item.get("status") != item.get("status"):
            lines.append((item["status"], f"{L['remaining']}: {item['label']}: {L[old_item['status']]} → {L[item['status']]}"))
        elif old_item["label"] != item["label"]:
            lines.append(("in_progress", f"{L['remaining']}: {old_item['label']} → {item['label']}"))
    if prev.get("next_step") != cur.get("next_step"):
        lines.append(("in_progress", f"{L['next_step']}: {cur['next_step']['title']}"))
    if prev.get("decisions") != cur.get("decisions"):
        lines.append(("in_progress", f"{L['decisions']}: {L['changed']}"))
    return marks, lines


# ---------------------------------------------------------------- render

TEXT = {
    "zh": {"done": "完成", "in_progress": "进行中", "not_started": "未开始", "stuck": "卡住",
           "scope_changed": "口径变化", "goal_unknown": "下一目标未定", "collected": "采集", "not_collected": "未采集",
           "suggested_order": "建议顺序", "replay_context": "当时的判断", "replay_hint": "回放保留当时的下一步、瓶颈和决策；组成部分的说明与证据仍为当前版本。",
           "ms_done": "已完成", "ms_current": "进行中", "ms_future": "未开始",
           "title": "项目地图", "updated": "更新", "commits_since": "次提交（自上次）", "commits_recent": "次近期提交",
           "you_are_here": "当前位置", "left_for": "距", "items": "项", "proposed": "提议", "inferred": "推断",
           "chain": "通往下一里程碑的阻塞链", "bottleneck": "当前瓶颈", "evidence_edge": "有证据的依赖",
           "inferred_edge": "推断的顺序", "no_edge": "未声明依赖", "other_deps": "其他依赖", "why": "依据",
           "parts": "项目全貌", "of_done": "已完成", "next_step": "建议的下一步", "decisions": "需要你决定",
           "default": "默认", "by": "截止", "changes": "自上次以来的变化", "first_run": "首次生成，暂无对比",
           "recent_commits": "最近提交", "uncertain": "不确定 / 推断", "details": "明细", "new": "新增",
           "removed": "已移除", "milestone": "里程碑", "waiting": "等待", "worktree": "工作区未提交",
           "sources": "来源", "warnings": "收集警告", "changed": "有变化", "remaining": "剩余项",
           "filter_all": "全部", "filter_moving": "在动", "filter_changed": "这次变了", "filter_inferred": "推断",
           "unmapped": "不在地图上", "accept": "接受默认", "override": "改口", "copied": "已复制",
           "expired": "已过期", "default_taken": "默认已生效", "see_path": "看另一条路", "hide_path": "收回这条路",
           "part_detail": "当前模块详情", "close": "关闭", "collection": "采集依据", "scrub_then": "当时", "scrub_now": "现在", "expiring": "将到期",
           "open_link": "在 GitHub 打开", "waits": "等你确认",
           "better": "变好", "worse": "变坏", "needs_you": "等你", "owner_user": "你来做", "owner_agent": "agent 来做",
           "unblocks": "做完解锁", "how": "怎么做", "open_left": "还差", "answered": "你已回答", "since_label": "自", "nothing_new": "没有新变化", "fork_if": "若改口", "copy_hint": "复制后贴回给 agent", "copy": "复制"},
    "en": {"done": "Done", "in_progress": "In progress", "not_started": "Not started", "stuck": "Stuck",
           "scope_changed": "Measure changed", "goal_unknown": "Next goal undecided", "collected": "collected", "not_collected": "not collected",
           "suggested_order": "Suggested order", "replay_context": "At that time", "replay_hint": "Replay shows the next step, bottleneck and decisions from that snapshot; part descriptions and evidence remain current.",
           "ms_done": "Done", "ms_current": "In progress", "ms_future": "Later",
           "title": "Project map", "updated": "Updated", "commits_since": "commits since last", "commits_recent": "recent commits",
           "you_are_here": "You are here", "left_for": "Left for", "items": "", "proposed": "proposed", "inferred": "inferred",
           "chain": "Blockers to the next milestone", "bottleneck": "Bottleneck", "evidence_edge": "Evidenced dependency",
           "inferred_edge": "Inferred order", "no_edge": "No declared dependency", "other_deps": "Other dependencies",
           "why": "Why", "parts": "Parts", "of_done": "done", "next_step": "Next step", "decisions": "Needs your call",
           "default": "Default", "by": "by", "changes": "Changed since last update", "first_run": "First map, nothing to compare",
           "recent_commits": "Recent commits", "uncertain": "Uncertain / inferred", "details": "Details", "new": "New",
           "removed": "Removed", "milestone": "Milestone", "waiting": "Waiting on", "worktree": "Uncommitted",
           "sources": "Sources", "warnings": "Collector warnings", "changed": "Changed", "remaining": "Remaining",
           "filter_all": "All", "filter_moving": "Moving", "filter_changed": "Changed", "filter_inferred": "Inferred",
           "unmapped": "Not on the map", "accept": "Accept default", "override": "Override", "copied": "Copied",
           "expired": "Expired", "default_taken": "Default stands", "see_path": "Show the other path",
           "hide_path": "Hide the other path", "part_detail": "Current module details", "close": "Close", "collection": "Collection evidence", "scrub_then": "Then", "scrub_now": "Now",
           "expiring": "expiring", "open_link": "Open on GitHub", "waits": "waits for you",
           "better": "better", "worse": "worse", "needs_you": "need you", "owner_user": "You", "owner_agent": "Agent",
           "unblocks": "Unblocks", "how": "How", "open_left": "Still open", "answered": "You answered", "since_label": "since", "nothing_new": "Nothing new", "fork_if": "If you override",
           "copy_hint": "Copy, then paste back to the agent", "copy": "Copy"},
}


def labels(m):
    return TEXT.get((m.get("style") or {}).get("lang") or m.get("lang") or "zh", TEXT["zh"])


class R:
    """Escaping + reference linking for user-visible text."""

    def __init__(self, web, shas):
        self.web, self.shas = web, shas

    def __call__(self, s):
        raw = str(s or "")
        links = []

        def keep(m):  # long URLs become short labels; the full URL stays in the link
            links.append(m.group(0))
            return f"\x00{len(links) - 1}\x00"
        raw = URL_RE.sub(keep, raw)
        t = html.escape(raw)
        t = re.sub(r"`([^`]+)`", r"<code>\1</code>", t)
        if self.web:
            base = html.escape(self.web, quote=True)
            t = re.sub(r"(?<![\w/&;])#(\d{1,6})\b",
                       lambda m: f'<a href="{base}/issues/{m.group(1)}" target="_blank" rel="noopener">#{m.group(1)}</a>', t)
            t = re.sub(r"\b([0-9a-f]{7,12})\b",
                       lambda m: f'<a href="{base}/commit/{m.group(1)}" target="_blank" rel="noopener"><code>{m.group(1)}</code></a>'
                       if m.group(1)[:7] in self.shas else m.group(0), t)
        slug = "/".join(self.web.rstrip("/").split("/")[-2:]) if self.web else None
        return re.sub(r"\x00(\d+)\x00", lambda m: (f'<a href="{html.escape(links[int(m.group(1))], quote=True)}" '
                                                    f'target="_blank" rel="noopener" title="{html.escape(links[int(m.group(1))], quote=True)}">'
                                                    f'{html.escape(short_url(links[int(m.group(1))], slug))}</a>'), t)


def fmt_time(iso):
    try:
        return dt.datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone().strftime("%m-%d %H:%M")
    except (ValueError, AttributeError):
        return iso or ""


def out_dir_of(facts):
    return os.path.join((facts.get("git") or {}).get("root") or ".", ".project-map") if facts.get("out") is None else facts["out"]


def answer_text(q, answer, did, facts):
    """What the Accept / Override buttons copy: readable for the agent, with the command that records it."""
    repo = ((facts or {}).get("git") or {}).get("root") or "<repo>"
    pm = os.path.abspath(__file__)
    return (f"you-are-here decision\nQuestion: {q.get('question')}\nAnswer: {answer}\n"
            f"Record it and act on it: python3 {pm} answer {repo} --id {did} --answer \"{answer.replace(chr(34), chr(39))}\"")


def cls(v, allowed, fallback):
    """A CSS class fragment from a fixed vocabulary; anything else (e.g. injected markup) falls back."""
    return v if v in allowed else fallback


def badge(status, L):
    visual = {"done": "done", "in_progress": "progress", "stuck": "blocked", "not_started": "todo"}[status]
    label = ("已完成" if L is TEXT["zh"] else L[status]) if status == "done" else L[status]
    icon = {"done": "✓ ", "in_progress": "◌ ", "stuck": '<i class="dot"></i>', "not_started": ""}[status]
    return f'<span class="status {visual}">{icon}{label}</span>'


def attr(s):
    return html.escape(str(s if s is not None else ""), quote=True)


def part_indexes(parts):
    by_id = {p["id"]: p for p in parts if p.get("id")}
    by_name = {p["name"].casefold(): p["id"] for p in parts if p.get("name") and p.get("id")}
    return by_id, by_name


def link_of(obj, by_id, by_name):
    """Part id shared by a card, a chain node, a pill, or a change line."""
    part = obj.get("part")
    if part in by_id:
        return part
    if obj.get("id") in by_id:
        return obj["id"]
    for key in ("label", "name"):
        label = obj.get(key)
        if isinstance(label, str) and label.casefold() in by_name:
            return by_name[label.casefold()]
    return None


def parse_deadline(text, generated_at):
    """Return an ISO timestamp, or None when the deadline is not a time we can count."""
    if not text:
        return None
    s = str(text).strip()
    try:
        return dt.datetime.fromisoformat(s.replace("Z", "+00:00")).isoformat(timespec="seconds")
    except ValueError:
        pass
    base = None
    if generated_at:
        try:
            base = dt.datetime.fromisoformat(str(generated_at).replace("Z", "+00:00"))
        except ValueError:
            base = None
    if base is None:
        base = dt.datetime.now().astimezone()
    m = re.fullmatch(r"(\d{2})-(\d{2})\s+(\d{2}):(\d{2})", s)
    if m:
        mo, d, hh, mm = (int(x) for x in m.groups())
        try:
            return base.replace(month=mo, day=d, hour=hh, minute=mm, second=0, microsecond=0).isoformat(timespec="seconds")
        except ValueError:
            return None
    m = re.fullmatch(r"(\d{2}):(\d{2})", s)
    if m:
        hh, mm = (int(x) for x in m.groups())
        try:
            return base.replace(hour=hh, minute=mm, second=0, microsecond=0).isoformat(timespec="seconds")
        except ValueError:
            return None
    return None


URL_RE = re.compile(r"https?://[^\s<>\"'`]+[^\s<>\"'`.,;:!?)\]）。，；]")


def short_url(url, slug=None):
    """A readable label for a long URL: owner/repo#12, a file path, run 123, or host/path."""
    m = re.match(r"https?://[^/]+/([^/]+/[^/#?]+)/(pull|issues)/(\d+)", url)
    if m:
        return f"#{m.group(3)}" if m.group(1) == slug else f"{m.group(1).split('/')[1]}#{m.group(3)}"
    m = re.match(r"https?://[^/]+/[^/]+/[^/]+/(?:blob|tree)/[^/]+/([^#?]+)", url)
    if m:
        return m.group(1)
    m = re.match(r"https?://[^/]+/[^/]+/[^/]+/actions/runs/(\d+)", url)
    if m:
        return f"run {m.group(1)}"
    m = re.match(r"https?://[^/]+/[^/]+/[^/]+/commit/([0-9a-f]{7})", url)
    if m:
        return m.group(1)
    bare = re.sub(r"^https?://(www\.)?", "", url).rstrip("/")
    return bare if len(bare) <= 48 else bare[:45] + "…"


def classify_evidence(raw):
    s = str(raw).strip()
    if len(s) >= 2 and s[0] == "`" and s[-1] == "`":
        s = s[1:-1].strip()
    if re.fullmatch(r"https?://\S+", s):
        return "link", s, short_url(s)
    mm = re.fullmatch(r"run:(\d+)", s)
    if mm:
        return "run", mm.group(1), f"run {mm.group(1)}"
    if re.fullmatch(r"#\d{1,6}", s):
        return "issue", s[1:], "#" + s[1:]
    if re.fullmatch(r"[0-9a-fA-F]{7,12}", s):
        return "commit", s[:7].lower(), s[:7]
    return "file", s, s


def evidence_chips(items):
    out = []
    for raw in (items or [])[:6]:
        kind, ref, label = classify_evidence(raw)
        shown = f"<code>{html.escape(label)}</code>" if str(raw).strip().startswith("`") else html.escape(label)
        out.append(f'<button type="button" class="ev" data-kind="{kind}" data-ref="{attr(ref)}">{shown}</button>')
    return "".join(out)


def peek_db(facts):
    """Subjects already collected, so a click can stay on the page."""
    g = (facts or {}).get("git") or {}
    gh = (facts or {}).get("github") or {}
    commits, files, issues, prs = {}, {}, {}, {}
    for c in g.get("commits") or []:
        sha = (c.get("sha") or "")[:7].lower()
        if sha:
            commits[sha] = c.get("subject") or ""
        for f in c.get("files") or []:
            files.setdefault(f, c.get("subject") or "")
    for i in gh.get("issues") or []:
        issues[str(i.get("number"))] = i.get("title") or ""
    for p in gh.get("prs") or []:
        prs[str(p.get("number"))] = p.get("title") or ""
    return {"issues": issues, "commits": commits, "prs": prs, "files": files}


def unmapped_files(facts, parts):
    """Files the next incremental pass cannot attach to a part."""
    delta = (facts or {}).get("delta") or {}
    raw = []
    if isinstance(delta.get("unmapped_files"), list):
        raw.extend(delta["unmapped_files"])
    if isinstance(delta.get("worktree_changed"), list):
        raw.extend(delta["worktree_changed"])
    prefixes = [x.rstrip("*").rstrip("/") for p in parts for x in (p.get("paths") or []) if x]
    out = []
    for f in raw:
        f = str(f).split(" -> ")[-1].strip().strip('"')
        if not f or f in out:
            continue
        if any(f == x or f.startswith(x + "/") for x in prefixes):
            continue
        out.append(f)
    return out[:40]


def fork_decision(m):
    for q in m.get("decisions") or []:
        opts = q.get("options")
        if q.get("id") and isinstance(opts, list) and len(opts) >= 2 and q.get("default") in opts:
            return q
    return None


def nodes_for(nodes, decision_id, option_index):
    kept = []
    for n in nodes:
        iff = n.get("if")
        if not isinstance(iff, dict) or iff.get("decision") != decision_id:
            kept.append(n)
        elif iff.get("option") == option_index:
            kept.append(n)
    return kept


def chain_row(nodes, edges, L, r, by_id, by_name, alt=False):
    # Prerequisites occupy columns; parallel work shares a column. Only supplied
    # edges become arrows, including branches and joins between non-neighbours.
    ranks, columns = {}, {}
    for n in nodes:
        rank = max((ranks[e["from"]] + 1 for e in edges
                    if e.get("to") == n["id"] and e.get("from") in ranks), default=0)
        ranks[n["id"]] = rank
        columns.setdefault(rank, []).append(n)
    width, height, gap, row_gap, pad = 174, 126, 54, 36, 24
    # Reserve enough lines for existing Chinese/Latin labels, instead of clipping
    # longer real project descriptions to the dimensions of a short demo label.
    for n in nodes:
        label_units = sum(2 if ord(c) > 255 else 1 for c in n["label"])
        sub_units = sum(2 if ord(c) > 255 else 1 for c in n.get("sub") or "")
        height = max(height, 74 + ((label_units + 17) // 18) * 21 + ((sub_units + 23) // 24) * 17)
    rows = max((len(col) for col in columns.values()), default=1)
    canvas_w = (max(columns, default=0) + 1) * (width + gap) - gap + 2 * pad
    canvas_h = rows * (height + row_gap) - row_gap + 2 * pad
    positions = {}
    for rank, col in columns.items():
        top = pad + (rows - len(col)) * (height + row_gap) / 2
        for row, n in enumerate(col):
            positions[n["id"]] = (pad + rank * (width + gap), top + row * (height + row_gap))
    marker = "chain-tip-alt" if alt else "chain-tip"
    lines, items, detours = [], [], 0
    for e in edges:
        if e.get("from") not in positions or e.get("to") not in positions:
            continue  # the other decision path has different nodes
        ax, ay = positions[e["from"]]
        bx, by = positions[e["to"]]
        x1, y1, x2, y2 = ax + width, ay + height / 2, bx, by + height / 2
        if ranks[e["to"]] == ranks[e["from"]] + 1:
            mid = (x1 + x2) / 2
            path = f"M{x1},{y1} C{mid},{y1} {mid},{y2} {x2},{y2}"
        else:
            detours += 1
            bottom = canvas_h + detours * 22
            path = f"M{x1},{y1} H{x1 + 18} V{bottom} H{x2 - 18} V{y2} H{x2}"
        kind = cls(e.get("kind"), EDGE_KIND, "inferred")
        why = e.get("why") or L["inferred_edge"]
        lines.append(f'<path class="graph-edge e-{kind}" data-from="{attr(e["from"])}" '
                     f'data-to="{attr(e["to"])}" d="{path}" marker-end="url(#{marker})">'
                     f'<title>{html.escape(why)}</title></path>')
    for n in nodes:
        st = cls(n.get("status"), PART_STATUS, "not_started")
        bn = f'<span class="bn">{html.escape(L["bottleneck"])}</span>' if n.get("bottleneck") else ""
        lid = link_of(n, by_id, by_name)
        link = f' data-link="{attr(lid)}"' if lid else ""
        iff = n.get("if") if isinstance(n.get("if"), dict) else None
        fork = " fork-only" if iff else (" fork-shared" if alt else "")
        x, y = positions[n["id"]]
        items.append(f'<foreignObject x="{x}" y="{y}" width="{width}" height="{height}">'
            f'<div class="chain-node n-{st}{" is-bn" if n.get("bottleneck") else ""} linkable{fork}"{link} '
            f'xmlns="http://www.w3.org/1999/xhtml" role="button" tabindex="0" '
            f'data-node="{attr(n["id"])}">{bn}<span class="graph-state">{L[st]}</span>'
            f'<b>{r(n["label"])}</b><span>{r(n.get("sub") or "")}</span></div></foreignObject>')
    canvas_h += detours * 22 + (12 if detours else 0)
    return (f'<svg class="dependency-graph" viewBox="0 0 {canvas_w} {canvas_h}" '
            f'style="min-width:{min(canvas_w, max(640, canvas_w * .78)):.0f}px;max-width:{canvas_w}px" '
            f'role="group" aria-label="{attr(L["chain"])}">'
            f'<defs><marker id="{marker}" viewBox="0 0 10 10" refX="9" refY="5" '
            f'markerWidth="6" markerHeight="6" orient="auto"><path d="M0 0L10 5L0 10z"/></marker></defs>'
            f'{"".join(lines)}{"".join(items)}</svg>')


def frame_from(m):
    parts = {}
    for p in m.get("parts") or []:
        pr = p.get("progress") or {}
        parts[p["id"]] = {"status": p.get("status"), "done": pr.get("done"), "total": pr.get("total"), "label": pr.get("label")}
    return {
        "t": m.get("generated_at") or "",
        "label": fmt_time(m.get("generated_at")) if m.get("generated_at") else "",
        "parts": parts,
        "milestones": [{"name": s.get("name"), "status": s.get("status")} for s in m.get("milestones") or []],
        "headline": m.get("headline") or "",
        "next_step": m.get("next_step") or {},
        "chain": m.get("chain") or {},
        "decisions": m.get("decisions") or [],
        "changes": [],
    }


def build_frames(history_dir, current):
    """Previous snapshots plus the map being rendered. Change text is between neighbours."""
    src = []
    if history_dir and os.path.isdir(history_dir):
        for name in sorted(os.listdir(history_dir)):
            if not name.endswith(".json"):
                continue
            prev = load_json(os.path.join(history_dir, name))
            if isinstance(prev, dict) and prev.get("parts"):
                src.append(prev)
    src.append(current)
    frames, prev_m = [], None
    for item in src:
        fr = frame_from(item)
        if prev_m is not None:
            _, lines = diff_maps(prev_m, item)
            fr["changes"] = [t for _, t in lines]
        fields = ("parts", "milestones", "headline", "next_step", "chain", "decisions")
        same = frames and all(frames[-1][k] == fr[k] for k in fields)
        if same:
            fr["changes"] = frames[-1]["changes"]
            frames[-1] = fr
        else:
            frames.append(fr)
        prev_m = item
    return frames[-8:]  # bound distinct updates, not repeated renderings of the same map


def render_analysis(report, lang, collected_at=None):
    if not report:
        return ""
    def label(cn, en):
        return cn if lang == "zh" else en
    def esc(value):
        return html.escape(str(value))
    def count(value):
        return f"{value:,}" if value is not None else label("未采集", "Unavailable")
    def compact(value):
        return f"{value / 10000:,.1f} 万" if value is not None and lang == "zh" and value >= 10000 else count(value)
    source, saved_run = report['original'], report['skill_run']
    # Collection belongs to an invocation; a later collect must not inherit its token count.
    try:
        start = dt.datetime.fromisoformat(saved_run['start'].replace('Z', '+00:00'))
        end = dt.datetime.fromisoformat((saved_run.get('end') or report.get('generated_at')).replace('Z', '+00:00'))
        collected = dt.datetime.fromisoformat(collected_at.replace('Z', '+00:00'))
        current_run = start.replace(microsecond=0) <= collected <= end
    except (KeyError, AttributeError, TypeError, ValueError):
        current_run = False
    run = saved_run if current_run else {**saved_run, 'usage_available': False, 'models': [], 'start': None, 'end': None}
    def value(item, key):
        return item['tokens'][key] if item.get('usage_available') else None
    rows = []
    for key, name in [('input', label("非缓存输入", "Uncached input")), ('cache_read', label("缓存读取", "Cache read")),
                      ('cache_write', label("缓存写入", "Cache write")), ('output', label("输出（含推理）", "Output (includes reasoning)")),
                      ('reasoning', label("其中：推理", "Of output: reasoning")), ('total', label("合计", "Total"))]:
        rows.append(f'<tr><td>{name}</td><td>{count(value(source, key))}</td><td>{count(value(run, key))}</td></tr>')
    models = [' · '.join(x['name'] for x in item['models']) or label("未采集", "Unavailable") for item in (source, run)]
    rows.append(f'<tr><td>{label("模型", "Model")}</td><td>{esc(models[0])}</td><td>{esc(models[1])}</td></tr>')
    rows.append(f'<tr><td>{label("统计区间", "Interval")}</td><td>{esc(fmt_time(source.get("start")))} → {esc(fmt_time(source.get("end")))}</td>'
                f'<td>{esc(fmt_time(run.get("start")))} → {esc(fmt_time(run.get("end")))}</td></tr>')
    notes = list(report.get('notes', []))
    if not current_run:
        previous = compact(saved_run['tokens']['total']) if saved_run.get('usage_available') else count(None)
        recorded = fmt_time(saved_run.get('start')) or label("日期未知", "date unknown")
        notes.append(label(f"上次 Skill 记录（{recorded}）：{previous} tokens；未匹配本次采集，本轮用量未采集。",
                           f"Previous Skill record ({recorded}): {previous} tokens; it does not match this collection. Current usage is unavailable."))
    elif not run.get('complete'):
        notes.append(label("本轮尚未结束，用量仅截至统计时。", "This run is ongoing; usage is a snapshot."))
    if run.get('counter_matches') is False:
        notes.append(label("逐请求合计与回合累计值不一致，此处显示逐请求日志合计。", "Request sum differs from the turn counter; showing request records."))
    if source.get('missing_usage_requests'):
        notes.append(label(f"原会话 {source['missing_usage_requests']} 个响应缺少用量，未计入。", f"{source['missing_usage_requests']} original responses have no usage."))
    original = label("原始会话", "Original session")
    current = label("本轮 Skill 使用", "Current Skill run")
    dated = f' · {esc(fmt_time(run.get("start")))}' if current_run else ''
    return (f'<details class="session-usage" id="session-usage"><summary><b>{label("Token 用量", "Token usage")}</b>'
            f'<span class="usage-preview">{original} <strong>{compact(value(source, "total"))}</strong>'
            f'<span class="usage-divider">/</span>{current} <strong>{compact(value(run, "total"))}</strong>{dated}</span>'
            f'<span class="usage-toggle">{label("明细", "Details")} ＋</span></summary>'
            f'<div class="usage-content"><div class="tw"><table><thead><tr><th>{label("类型", "Type")}</th>'
            f'<th>{original}{label("（含子任务）", " (includes subagents)")}</th><th>{current}</th></tr></thead>'
            f'<tbody>{"".join(rows)}</tbody></table></div><ul>{"".join(f"<li>{esc(x)}</li>" for x in notes)}</ul>'
            f'<p class="usage-source">{esc(source["source"])}<br>{esc(saved_run["source"])}<br>turn_id: {esc(saved_run["turn_id"])}</p></div></details>')


def render_html(m, facts, marks, change_lines, style, prev_time, frames=None, analysis=None):
    L = labels(m)
    g = (facts or {}).get("git", {})
    slug = g.get("github")
    web = g.get("github_web") or (f"https://github.com/{slug}" if slug else None)
    shas = {c["sha"][:7] for c in g.get("commits") or []}
    r = R(web, shas)
    P = m["project"]
    parts = m["parts"]
    by_id, by_name = part_indexes(parts)
    n_stuck = sum(p["status"] == "stuck" for p in parts)
    stray = unmapped_files(facts, parts)
    fork = fork_decision(m)

    # The accepted demo layout: compact identity, a prominent route, then the overview.
    def localized(zh, en):
        return zh if L is TEXT["zh"] else en

    collected = []
    meta = [f'<span>{L["updated"]} <strong>{fmt_time(m.get("generated_at") or now_iso())}</strong></span>']
    if facts:
        for name, source in (("Git", g), ("GitHub", facts.get("github") or {})):
            stamp = source.get("collected_at")
            collected.append(f'{name} {L["collected"]} <b>{r(fmt_time(stamp))}</b>' if stamp else f'{name} {L["not_collected"]}')
        n = len(g.get("commits", []))
        meta.append(f'<span><strong>{n}</strong> {L["commits_since"] if facts.get("mode") == "incremental" else L["commits_recent"]}</span>')
        wt = g.get("worktree", {})
        if wt.get("modified") or wt.get("untracked"):
            meta.append(f'<span>{L["worktree"]} <strong>{wt.get("modified", 0) + wt.get("untracked", 0)}</strong></span>')
    if n_stuck:
        meta.append(f'<span class="blocked-count"><i class="dot"></i>{n_stuck} {L["stuck"]}</span>')
    repo_link = (f'<a class="demo-tag" href="{attr(web)}" target="_blank" rel="noopener">{html.escape(slug)} ↗</a>'
                 if slug and web else f'<span class="demo-tag">{L["title"]}</span>')
    head = (f'<div class="topline"><div class="brandline">'
            '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" aria-hidden="true">'
            '<circle cx="12" cy="12" r="8.5" stroke="currentColor"/><circle cx="12" cy="12" r="3" fill="currentColor"/>'
            '<path d="M12 1v4M12 19v4M1 12h4M19 12h4" stroke="currentColor"/></svg>'
            f'<span>YOU ARE HERE</span><span class="slash">/</span><span>{L["title"]}</span></div>{repo_link}</div>'
            f'<header class="header"><div class="identity"><h1>{r(P["name"])}<span class="brand-dot">.</span></h1>'
            f'<p class="summary">{r(P.get("summary", ""))}</p></div>'
            f'<div class="meta">{"<span class=divider></span>".join(meta)}</div></header>')
    headline = m.get("headline")

    ms = m.get("milestones") or []
    has_current = any(s["status"] == "current" for s in ms)
    reached = max((i for i, stage in enumerate(ms) if stage["status"] == "done"), default=-1)
    last_position = next((i for i, stage in enumerate(ms) if stage["status"] == "current"), reached)
    nodes = []
    for i, stage in enumerate(ms):
        st = cls(stage["status"], MS_STATUS, "future")
        reached_class = " reached" if not has_current and i == reached else ""
        here_text = L["you_are_here"] if st == "current" else localized("已到这里", "Reached here")
        date = stage.get("date") or ""
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", date):
            date = date[5:].replace("-", ".")
        note = f'{L["ms_" + st]} · {date}' if date else L["ms_" + st]
        prop = f'<span class="tag">{L["proposed"]}</span>' if stage.get("proposed") else ""
        nodes.append(
            f'<li class="milestone ms ms-{st} {st}{reached_class}" data-stage="{i}" '
            f'data-name="{attr(stage["name"])}" data-now="{st}" role="button" tabindex="0" '
            f'aria-haspopup="dialog" title="{attr(stage.get("note") or "")}"'
            f'{" id=here" if st == "current" else ""}>'
            f'<span class="here">{here_text}</span><span class="node">'
            '<svg viewBox="0 0 24 24" fill="none" aria-hidden="true"><path d="m6 12 4 4 8-9" '
            'stroke="currentColor" stroke-width="2.8" stroke-linecap="round" stroke-linejoin="round"/></svg></span>'
            f'<h2>{r(stage["name"])}</h2><p class="ms-note">{r(note)}</p>{prop}</li>')
    rem = [x for x in m.get("remaining") or [] if x["status"] != "done"]
    cur_name = next((stage["name"] for stage in ms if stage["status"] == "current"), "")
    pills = []
    for item in rem:
        lid = link_of(item, by_id, by_name)
        link = f' data-link="{attr(lid)}" role="button" tabindex="0"' if lid else ""
        visual = {"in_progress": "progress", "stuck": "blocked"}.get(item["status"], "")
        pills.append(f'<span class="pill {visual} p-{item["status"]}{" linkable" if lid else ""}"{link}>'
                     f'<i class="dot"></i>{r(item["label"])}</span>')
    remaining = (f'<div class="remaining"><span class="remaining-label">{L["left_for"]} {r(cur_name)} '
                 f'<strong class="big">{len(rem)}</strong> {L["items"]}</span>{"".join(pills)}</div>') if rem else ""
    if not has_current:
        remaining = f'<div class="remaining goal-unknown"><span class="remaining-label">{L["goal_unknown"]}</span></div>' + remaining
    offset = 50 / max(1, len(ms))
    completed = max(0, last_position) / max(1, len(ms) - 1) * 100
    track = (f'<section class="route track"><ol class="milestones" style="--stages:{max(1,len(ms))}">'
             f'<li class="rail" role="presentation" aria-hidden="true" style="left:{offset}%;right:{offset}%;--completed:{completed}%"></li>'
             f'{"".join(nodes)}</ol>{remaining}</section>') if ms else f'<section class="route">{remaining}</section>'

    # blocker chain. A decision with options splits a second, quieter path.
    chain = ""
    ch = m.get("chain") or {}
    cn = ch.get("nodes") or []
    bottleneck_label = next((n["label"] for n in cn if n.get("bottleneck")), "")
    forked = False
    if cn:
        edges = ch.get("edges") or []
        main_nodes = cn
        alt_nodes = []
        alt_html = ""
        forked = bool(fork) and any(isinstance(n.get("if"), dict) and n["if"].get("decision") == fork["id"] for n in cn)
        if forked:
            opts = fork["options"]
            di = opts.index(fork["default"])
            other = next(k for k in range(len(opts)) if k != di)
            main_nodes = nodes_for(cn, fork["id"], di)
            alt_nodes = nodes_for(cn, fork["id"], other)
            sep = "：" if L is TEXT["zh"] else ": "
            alt_html = (f'<p class="fork-label">{html.escape(L["fork_if"])}{sep}{r(opts[other])}</p>'
                        f'<div class="chain chain-alt">{chain_row(alt_nodes, edges, L, r, by_id, by_name, alt=True)}</div>')
        names = {n["id"]: n["label"] for n in cn}
        extra = []
        for e in edges:
            pair = (e.get("from"), e.get("to"))
            if pair[0] not in names or pair[1] not in names:
                continue
            k = "evidence" if e.get("kind") == "evidence" else "inferred"
            sep = "：" if L is TEXT["zh"] else ": "
            extra.append(f'<li class="w-{k}"><b>{r(names[pair[0]])} → {r(names[pair[1]])}</b>'
                         f' <span class="tag">{html.escape(L["evidence_edge"] if k == "evidence" else L["inferred_edge"])}</span>'
                         f'{sep}{r(e.get("why", ""))}</li>')
        extra_html = (f'<details class="edge-evidence"><summary>{localized("关系依据", "Dependency evidence")} · {len(extra)}</summary>'
                      f'<ul class="why">{"".join(extra)}</ul></details>') if extra else ""
        legend = (f'<div class="legend"><span><i class="lg-e"></i>{L["evidence_edge"]}</span>'
                  f'<span><i class="lg-i"></i>{L["inferred_edge"]}</span>'
                  f'<span><i class="lg-b"></i>{L["bottleneck"]}</span></div>')
        chain_title = L["chain"] if any(e.get("kind") == "evidence" for e in edges) else L["suggested_order"]
        chain = (f'<section class="card chain-card"><h2>{r(ch.get("title") or chain_title)}</h2>'
                 f'{legend}<div class="chain chain-default">{chain_row(main_nodes, edges, L, r, by_id, by_name)}</div>'
                 f'{alt_html}<p class="graph-hint">{localized("可横向滑动查看完整路径 · 点击节点定位模块", "Scroll for the full path · select a node to locate its part")}</p>'
                 f'{extra_html}</section>')

    # Keep every part in the supplied order; reveal the full evidence on demand.
    cards, part_details = [], []
    for p in parts:
        prog = ""
        pr = p.get("progress")
        if pr:
            pct = max(0, min(100, round(100 * pr["done"] / pr["total"])))
            prog = (f'<div class="prog"><div class="pl"><span>{r(pr.get("label") or "")}</span>'
                    f'<span>{pr["done"]}/{pr["total"]}</span></div><div class="bar"><i style="width:{pct}%"></i></div></div>')
        chg = f'<span class="chg">{L["changed"]} · {r(marks[p["id"]])}</span>' if p["id"] in marks else ""
        inf = f'<span class="tag">{L["inferred"]}</span>' if p.get("inferred") else ""
        changed = p["id"] in marks
        stuck = p["status"] == "stuck" and p.get("waiting_on")
        summary = f'<p>{r(p.get("summary", ""))}</p>' if p.get("summary") else ""
        if p.get("open"):
            summary += f'<p class="open-left"><span>{L["open_left"]}</span> {r(p["open"])}</p>'
        infer = " data-inferred" if p.get("inferred") else ""
        pst = cls(p["status"], PART_STATUS, "not_started")
        brief = (p.get("waiting_on") if stuck else
                 f'{L["open_left"]} {p["open"]}' if p.get("open") else p.get("summary", ""))
        visual = {"done": "done", "in_progress": "progress", "stuck": "blocked", "not_started": "todo"}[pst]
        card_badge = badge(pst, L)
        meter = ""
        if pr and pst != "done":
            card_badge = card_badge.replace('</span>', f' · {pr["done"]}/{pr["total"]} {r(pr.get("label") or "")}</span>')
            meter = (f'<div class="mini-progress" role="progressbar" aria-label="{attr(pr.get("label") or p["name"])}" '
                     f'aria-valuemin="0" aria-valuemax="{pr["total"]}" aria-valuenow="{pr["done"]}">'
                     f'<span style="width:{pct}%"></span></div>')
        card = (f'<article class="part {visual} pt-{pst}{" changed" if changed else ""} linkable" '
                f'data-link="{attr(p["id"])}" data-status="{pst}" data-now="{pst}"{infer} '
                f'role="button" tabindex="0" aria-haspopup="dialog">'
                f'<span class="part-title"><h3>{r(p["name"])}</h3><span class="part-arrow">↗</span></span>'
                f'{card_badge}{inf}{meter}<p class="part-note">{r(brief)}</p></article>')
        cards.append(card)
        wait = f'<p class="wait">{r(p["waiting_on"])}</p>' if stuck else ""
        part_details.append(f'<template data-detail="{attr(p["id"])}"><h3>{r(p["name"])}</h3>'
                            f'<div class="ph">{badge(p["status"], L)}{inf}{chg}</div>'
                            f'{summary}{wait}{prog}<footer>{evidence_chips(p.get("evidence"))}</footer></template>')
    highlight = (f'<button type="button" class="highlight-button" data-filter="changed" aria-pressed="false">'
                 f'<i class="change-marker"></i>{localized("突出本轮变化", "Highlight changes")}</button>')
    stray_html = ""
    if stray:
        chips = "".join(f"<li><code>{html.escape(f)}</code></li>" for f in stray)
        stray_html = (f'<div class="unmap"><span class="tag">{html.escape(L["unmapped"])}</span>'
                      f'<ul class="plain">{chips}</ul></div>')
    n_done = sum(p["status"] == "done" for p in parts)
    columns = 4 if len(parts) > 6 else 3
    parts_html = (f'<section class="panel parts-panel"><div class="panel-heading"><div class="panel-title">'
                  f'<h2>{L["parts"]}</h2><span class="completion"><strong>{n_done}</strong> / {len(parts)} {L["of_done"]}</span>'
                  f'</div>{highlight}</div><div class="parts" style="--columns:{columns}">{"".join(cards)}</div>'
                  f'<div class="parts-caption"><span class="legend"><i class="change-marker"></i>'
                  f'{localized("青绿标记 = 本轮有变化", "Accent mark = changed this round")}</span>'
                  f'<span>{localized("点击模块，查看任务与依据 ↗", "Click a part for tasks & evidence ↗")}</span></div></section>')

    # Desktop actions occupy the right column; CSS places the next step first on mobile.
    ns = m["next_step"]
    owner = ns.get("owner") if ns.get("owner") in OWNERS else None
    who = f'<span class="owner o-{owner}" title="{attr(L["owner_" + owner])}">↗</span>' if owner else ""
    how = (f'<p class="how"><span>{L["how"]}</span> {r(ns["action"])}</p>' if ns.get("action") else "")
    unl = (f'<p class="unl"><span>{L["unblocks"]}</span> {r(ns["unblocks"])}</p>' if ns.get("unblocks") else "")
    next_html = (f'<section class="panel action-panel next-panel next"><div class="eyebrow">{L["next_step"]}{who}</div><h2 class="nt">{r(ns["title"])}</h2>'
                 f'<p>{r(ns.get("why") or "")}</p>'
                 + (f'<details><summary>{L["how"]}</summary>{how}{unl}</details>' if how or unl else "") + "</section>")
    side = [next_html]
    answers = load_answers(out_dir_of(facts)) if facts else []
    if m.get("decisions"):
        qs = []
        for i, q in enumerate(m["decisions"]):
            ans = answer_for(q, answers)
            did = q.get("id") or f"d{i}"
            iso = parse_deadline(q.get("deadline"), m.get("generated_at"))
            prop = f' <span class="tag">{L["proposed"]}</span>' if q.get("proposed") else ""
            if q.get("reversible") is not True:
                prop += f' <span class="tag waits">{html.escape(L["waits"])}</span>'
            if q.get("deadline"):
                stamp = f' data-deadline="{attr(iso)}"' if iso else ""
                shown = fmt_time(iso) if iso else q["deadline"]
                when = f'{html.escape(L["by"])} <time{stamp}>{r(shown)}</time> <span class="count"></span>→ '
            else:
                when = html.escape(L["default"]) + " → "
            copy = attr(answer_text(q, str(q.get("default") or ""), did, facts)).replace("\n", "&#10;")
            alt = ""
            opts = q.get("options") if isinstance(q.get("options"), list) else []
            if forked and fork.get("id") == q.get("id"):
                di = opts.index(q["default"])
                other = next(k for k in range(len(opts)) if k != di)
                alt = (f'<button type="button" class="act altpath" data-fork="{attr(q["id"])}" '
                       f'data-option="{other}" data-show="{attr(L["see_path"])}" data-hide="{attr(L["hide_path"])}">'
                       f'{html.escape(L["see_path"])}</button>')
            if ans:
                qs.append(f'<li class="answered" data-decision="{attr(did)}"><p class="q">{r(q["question"])}</p>'
                          f'<p class="d">{L["answered"]} → <b class="def">{r(ans["answer"])}</b> '
                          f'<span class="t">{fmt_time(ans.get("at"))}</span></p></li>')
                continue
            qs.append(
                f'<li data-decision="{attr(did)}">'
                f'<p class="q">{r(q["question"])}{prop}</p>'
                f'<p class="d">{when}<b class="def">{r(q["default"])}</b></p>'
                f'<p class="taken">{html.escape(L["default_taken"])}</p>'
                f'<div class="acts">'
                f'<button type="button" class="act accept" data-copy="{copy}">{html.escape(L["accept"])}</button>'
                f'{alt}'
                f'<button type="button" class="act override">{html.escape(L["override"])}</button>'
                f'<span class="copied">{html.escape(L["copied"])}</span></div>'
                f'<form class="override-form" hidden>'
                f'<input type="text" maxlength="240" aria-label="{attr(L["override"])}">'
                f'<button type="submit">{html.escape(L["copy"])}</button></form>'
                f'<p class="hint">{html.escape(L["copy_hint"])}</p></li>')
        side.append(f'<section class="panel action-panel decision-panel decide"><div class="eyebrow">{L["decisions"]}</div><ul>{"".join(qs)}</ul></section>')
    elif headline:
        side.append(f'<section class="panel action-panel current-panel"><div class="eyebrow">{localized("当前进展", "Where things stand")}</div>'
                    f'<h2>{r(headline)}</h2><p>{L["goal_unknown"] if not has_current else r(cur_name)}</p></section>')
    since = f'<span class="since">{fmt_time(prev_time)}</span>' if prev_time else ""
    if change_lines:
        rows = []
        for k, t in change_lines:
            name = str(t).split(":", 1)[0].strip()
            lid = by_name.get(name.casefold())
            link = f' data-link="{attr(lid)}" role="button" tabindex="0"' if lid else ""
            lk = "linkable" if lid else ""
            rows.append(f'<li class="{lk}"{link}><i class="dot-{cls(k, PART_STATUS + ("new",), "new")}"></i>{r(t)}</li>')
        cl = "".join(rows)
    else:
        cl = f'<li class="muted">{L["nothing_new"] if prev_time else L["first_run"]}</li>'
    seen_subjects, commits = set(), []
    for c in ((facts or {}).get("git") or {}).get("commits") or []:
        if c.get("subject") in seen_subjects:
            continue
        seen_subjects.add(c.get("subject"))
        commits.append(c)
    commits = commits[:6]
    cm = "".join(f'<li><span class="t">{fmt_time(c["date"])}</span> {r(c["sha"][:7])} {r(c["subject"])}</li>'
                 for c in commits)
    side.append(f'<section class="panel changes-panel"><h2 class="eyebrow"><i class="change-marker"></i>{L["changes"]}</h2>'
                f'<ul class="change-list">{cl}</ul>{since}</section>')

    # detail panels
    det = []
    if m.get("uncertain"):
        det.append(f'<details><summary><b>{L["uncertain"]} · {len(m["uncertain"])}</b></summary><ul class="plain">'
                   + "".join(f"<li>{r(x)}</li>" for x in m["uncertain"]) + "</ul></details>")
    if cm:
        det.append(f'<details><summary><b>{L["recent_commits"]}</b></summary><ul class="commits">{cm}</ul></details>')
    if headline and m.get("decisions"):
        det.append(f'<p class="headline-note">{r(headline)}</p>')
    for p in m.get("panels") or []:
        if p["kind"] == "table":
            th = "".join(f"<th>{r(c)}</th>" for c in p.get("columns") or [])
            tr = "".join("<tr>" + "".join(f"<td>{r(c)}</td>" for c in row) + "</tr>" for row in p.get("rows") or [])
            body = f'<div class="tw"><table><thead><tr>{th}</tr></thead><tbody>{tr}</tbody></table></div>'
        elif p["kind"] == "list":
            body = '<ul class="plain">' + "".join(f"<li>{r(x)}</li>" for x in p.get("items") or []) + "</ul>"
        else:
            body = f'<p>{r(p.get("text") or "")}</p>'
        det.append(f'<details{"" if p.get("collapsed", True) else " open"}><summary><b>{r(p["title"])}</b>'
                   f'<span>{r(p.get("hint", ""))}</span></summary>{body}</details>')
    collection = " · ".join(collected)
    if collection or stray_html:
        det.append(f'<details><summary><b>{L["collection"]}</b></summary><p>{collection}</p>{stray_html}</details>')
    details = f'<section class="details"><h2>{L["details"]}</h2>{"".join(det)}</section>' if det else ""

    foot = []
    if m.get("sources"):
        foot.append(f'{L["sources"]}：' + r("、".join(m["sources"]) if L is TEXT["zh"] else ", ".join(m["sources"])))
    if facts and facts.get("warnings"):
        foot.append(f'{L["warnings"]}：' + r("; ".join(facts["warnings"])))
    footer = "".join(f"<p>{x}</p>" for x in foot)

    expiring = sum(1 for q in (m.get("decisions") or []) if q.get("deadline") and not answer_for(q, answers))
    pin_bits = [f'<b>{html.escape(L["you_are_here"])}</b>']
    if bottleneck_label:
        pin_bits.append(f'<span>{r(bottleneck_label)}</span>')
    pin_bits.append(f'<span>{r(m["next_step"]["title"])}</span>')
    if expiring:
        pin_bits.append(f'<span>{expiring} {html.escape(L["expiring"])}</span>')
    if stray:
        pin_bits.append(f'<span>{len(stray)} {html.escape(L["unmapped"])}</span>')
    pin = f'<div class="pin" id="pin" hidden>{"".join(pin_bits)}</div>'
    use_frames = frames if frames else [frame_from(m)]
    scrub = ""
    if len(use_frames) >= 2:
        scrub = (f'<div class="scrub"><span>{html.escape(L["scrub_then"])}</span>'
                 f'<input type="range" id="scrub" min="0" max="{len(use_frames) - 1}" value="{len(use_frames) - 1}" '
                 f'aria-label="{attr(L["changes"])}">'
                 f'<span>{html.escape(L["scrub_now"])}</span><span id="scrubcap"></span></div>')
        title = "历史回放" if L is TEXT["zh"] else "History replay"
        scrub = (f'<section class="details"><details class="history-details">'
                 f'<summary><b>{title}</b></summary>{scrub}</details></section>')
    payload = {
        "slug": slug,
        "web": web,
        "answer_tpl": answer_text({"question": "{Q}"}, "{A}", "{ID}", facts),
        "peek": peek_db(facts),
        "milestones": ms,
        "frames": use_frames,
        "labels": {k: L[k] for k in ("next_step", "bottleneck", "decisions", "default", "owner_user", "owner_agent", "how", "unblocks")},
    }
    payload["labels"].update(expired=L["expired"], open=L["open_link"], part_detail=L["part_detail"],
                             you_are_here=L["you_are_here"], reached=localized("已到这里", "Reached here"),
                             milestone=localized("里程碑", "Milestone"))
    data = json.dumps(payload, ensure_ascii=False).replace("<", "\\u003c")
    replay = (f'<section class="card replay-context" id="replay-context" hidden><h2>{L["replay_context"]}</h2>'
              f'<p class="muted">{L["replay_hint"]}</p><div id="replay-text"></div></section>')
    body = (f'{pin}{head}{track}{replay}<div class="workspace grid"><div class="main">{chain}{parts_html}</div>'
            f'<aside class="aside">{"".join(side)}</aside></div>{render_analysis(analysis, "zh" if L is TEXT["zh"] else "en", (facts or {}).get("collected_at"))}'
            f'<footer class="footer"><details class="sources"><summary>{localized("查看依据与历史", "Evidence & history")}</summary>'
            f'<div class="source-content">{scrub}{details}<div class="foot">{footer}</div></div></details>'
            f'<div class="signature">{localized("少一点追问，<span>多一点方向感。</span>", "Less chasing. <span>More direction.</span>")}</div></footer>'
            f'{"".join(part_details)}<dialog id="part-dialog" aria-labelledby="part-detail-title"><div class="dialog-content">'
            f'<div class="dialog-topline"><span class="eyebrow" id="part-detail-title">{L["part_detail"]}</span>'
            f'<button type="button" class="close" id="close-part" aria-label="{L["close"]}">×</button></div>'
            f'<div id="part-detail"></div></div></dialog>'
            f'<div id="peek" hidden role="dialog"><p id="peek-text"></p>'
            f'<a id="peek-link" target="_blank" rel="noopener" hidden></a></div>')
    page = PAGE.format(
        lang="zh-CN" if L is TEXT["zh"] else "en",
        title=html.escape(f'{P["name"]} · {L["title"]}'),
        theme=html.escape(style["theme"]),
        body=body, data=data, css=page_css(html.escape(style["accent"])))
    return page.replace("__SCRIPT__", asset("page.js"))


ASSETS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "assets")


def asset(name):
    with open(os.path.join(ASSETS, name), encoding="utf-8") as f:
        return f.read()


def page_css(accent):
    return asset("page.css").replace("__ACCENT__", accent)


PAGE = """<!doctype html>
<html lang="{lang}" data-theme="{theme}">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<style>
{css}</style>
</head>
<body><main class="page">{body}</main>
<script type="application/json" id="yah">{data}</script>
<script>__SCRIPT__</script>
</body></html>
"""




def load_style(m):
    s = dict(DEFAULT_STYLE)
    s.update({k: v for k, v in (load_json(STYLE_FILE, {}) or {}).items() if v})
    s.update({k: v for k, v in (m.get("style") or {}).items() if v})
    if s["theme"] not in ("dark", "light", "auto"):
        s["theme"] = "dark"
    if not re.fullmatch(r"#[0-9a-fA-F]{3,8}", s["accent"]):
        s["accent"] = DEFAULT_STYLE["accent"]
    return s


def cmd_render(a):
    repo = repo_root(a.repo)
    d = out_dir(repo, a.out)
    with locked(d):
        render(repo, d)


def map_content(m):
    return {k: v for k, v in m.items() if k not in ("generated_at", "style")}


def render(repo, d):
    m = load_json(os.path.join(d, "map.json"))
    if m is None:
        sys.exit(f"pmap: missing or invalid JSON: {os.path.join(d, 'map.json')}")
    errs = validate(m)
    if errs:
        for e in errs:
            print(f"error: {e}")
        sys.exit(1)
    style = load_style(m)
    m.setdefault("style", {})["lang"] = style["lang"]
    m.setdefault("generated_at", now_iso())
    facts = load_json(os.path.join(d, "facts.json"))
    if isinstance(facts, dict):
        facts["out"] = d  # in memory only: where answers.json lives
    state = load_json(os.path.join(d, "state.json"), {}) or {}
    prev = load_json(state["last_map"]) if state.get("last_map") else None
    comparison = prev
    history = os.path.join(d, "history")
    if prev and map_content(prev) == map_content(m):
        comparison = None
        for name in reversed(sorted(os.listdir(history))):
            if not name.endswith(".json"):
                continue
            candidate = load_json(os.path.join(history, name))
            if isinstance(candidate, dict) and map_content(candidate) != map_content(m):
                comparison = candidate
                break
    marks, lines = diff_maps(comparison, m)
    prev_time = comparison.get("generated_at") if comparison else None
    frames = build_frames(os.path.join(d, "history"), m)
    analysis = load_json(os.path.join(d, "analysis.json"))
    page = render_html(m, facts, marks, lines, style, prev_time, frames, analysis=analysis)
    write_text(os.path.join(d, "index.html"), page)
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    snap = os.path.join(d, "history", f"{stamp}.json")
    dump_json(snap, m)
    pend = os.path.join(d, "pending_baseline.json")
    if os.path.exists(pend):
        os.replace(pend, os.path.join(d, "baseline.json"))
    state.update({"last_map": snap, "last_render": now_iso(), "repo": repo})
    dump_json(os.path.join(d, "state.json"), state)
    for x in lint(m):
        print(f"warning: {x}")
    print(f"changes since previous content update: {len(lines)}" if prev else "first render (no previous map)")
    for _, t in lines:
        print(f"  - {t}")
    print(f"html: {os.path.join(d, 'index.html')}")


SEVERE_COMMITS = 20  # this far behind, the map's content no longer describes the project
SEVERE_HOURS = 72


def staleness(repo, d, commits_at=1, hours=12, files_at=1):
    """Cheap staleness check (git only, no network, no model): status none | fresh | stale.
    A stale map is also `severe` when it is far enough behind that its content should not be quoted."""
    state = load_json(os.path.join(d, "state.json"), {}) or {}
    since = state.get("last_render")
    facts = load_json(os.path.join(d, "facts.json"), {}) or {}
    res = {"repo": repo, "map": os.path.join(d, "index.html"), "last_render": since, "scope": "git",
           "github_collected_at": (facts.get("github") or {}).get("collected_at")}
    if not since or not os.path.exists(os.path.join(d, "map.json")):
        res.update(status="none", reason="no map yet")
        return res
    baseline = load_json(os.path.join(d, "baseline.json"), {}) or {}
    rng = new_commit_range(repo, baseline, state, [])
    commits = 0
    if rng and rng[0] != "-n":  # "-n": no baseline at all, nothing to compare against
        ok, out = git(repo, "rev-list", "--count", *rng)
        commits = int(out) if ok and out.strip().isdigit() else 0
    if "tags" in baseline:
        new_tags = sorted(set(tag_names(repo)) - set(baseline["tags"]))
    else:  # map rendered by an older version: fall back to tag dates
        new_tags = []
        ok, out = git(repo, "for-each-ref", "--format=%(refname:short)\x03%(creatordate:iso-strict)", "refs/tags")
        t0 = dt.datetime.fromisoformat(since)
        for line in (out.splitlines() if ok and out else []):
            name, _, date = line.partition("\x03")
            try:
                if dt.datetime.fromisoformat(date.replace("Z", "+00:00")) > t0:
                    new_tags.append(name)
            except ValueError:
                pass
    files = 0
    if baseline.get("worktree_fp") is not None:
        _, fp = worktree(repo)
        old = baseline["worktree_fp"]
        files = sum(1 for p in fp.keys() | old.keys() if fp.get(p) != old.get(p))
    age_h = (dt.datetime.now().astimezone() - dt.datetime.fromisoformat(since)).total_seconds() / 3600
    stale = (commits >= commits_at or bool(new_tags) or files >= files_at
             or ((commits or files) and age_h >= hours))
    severe = bool(stale and (commits >= SEVERE_COMMITS or ((commits or files) and age_h >= SEVERE_HOURS)))
    res.update(status="stale" if stale else "fresh", severe=severe, commits_since=commits, new_tags=new_tags,
               worktree_changed=files, age_hours=round(age_h, 1))
    why = [f"{commits} new commits"] if commits else []
    why += [f"{files} files edited"] if files else []
    why += [f"new tag {', '.join(new_tags)}"] if new_tags else []
    if why:
        res["reason"] = "; ".join(why)
    return res


def cmd_status(a):
    repo = repo_root(a.repo)
    d = os.path.abspath(a.out) if a.out else os.path.join(repo, ".project-map")
    res = staleness(repo, d, a.commits, a.hours, a.files)
    if a.json:
        print(json.dumps(res, ensure_ascii=False))
    else:
        extra = f" ({res['reason']})" if res.get("reason") else ""
        since = res.get("last_render")
        external = fmt_time(res.get("github_collected_at")) or "not collected"
        severe = " · severely behind, do not quote the map" if res.get("severe") else ""
        print(f"{res['status']}{extra}{severe} · git only" + (f" · last update {fmt_time(since)}" if since else "")
              + f" · GitHub last collected: {external} (not checked by status)")


AUTO_DEFAULTS = {"auto": False, "every_minutes": 20, "agent": "same", "notify": True}


def send_notification(title, body):
    """Desktop notification: macOS Notification Center, Linux notify-send. YAH_NOTIFY_LOG redirects to a file."""
    log = os.environ.get("YAH_NOTIFY_LOG")
    if log:
        with open(log, "a", encoding="utf-8") as f:
            f.write(f"{title}\t{body}\n")
        return True
    if sys.platform == "darwin" and shutil.which("osascript"):
        esc = lambda t: str(t).replace("\\", "\\\\").replace('"', '\\"')
        return run(["osascript", "-e", f'display notification "{esc(body)}" with title "{esc(title)}"'], timeout=10)[0]
    if shutil.which("notify-send"):
        return run(["notify-send", str(title), str(body)], timeout=10)[0]
    return False


def notify_after_refresh(repo, d, before):
    """After a background refresh: tell the user when something got worse, needs them, or the refresh failed."""
    if not load_config(d).get("notify", True):
        return None
    state = load_json(os.path.join(d, "state.json"), {}) or {}
    name = os.path.basename(repo)
    if state.get("last_render") == before:
        send_notification(f"you-are-here · {name}", "Background refresh did not update the map; see .project-map/bg.log")
        return "failed"
    snaps = sorted(f for f in os.listdir(os.path.join(d, "history")) if f.endswith(".json"))
    cur = load_json(os.path.join(d, "history", snaps[-1])) if snaps else None
    prev = load_json(os.path.join(d, "history", snaps[-2])) if len(snaps) > 1 else None
    if not cur:
        return None
    _, lines = diff_maps(prev, cur)
    worse = sum(1 for k, _ in lines if k == "stuck")
    old_q = {q.get("question") for q in (prev or {}).get("decisions") or []}
    new_q = [q for q in cur.get("decisions") or [] if q.get("question") not in old_q]
    if not worse and not new_q:
        return None
    L = labels(cur)
    bits = ([f"{worse} {L['worse']}"] if worse else []) + ([f"{len(new_q)} {L['needs_you']}"] if new_q else [])
    send_notification(f"you-are-here · {name}: {' · '.join(bits)}", cur.get("headline") or cur["next_step"]["title"])
    return " · ".join(bits)


REFRESH_BACKOFF_MAX_MINUTES = 6 * 60
REFRESH_PAUSE_AFTER = 5  # failures in a row before retries stop until `auto on`
AUTH_FAILURE = re.compile(r"failed to authenticate|not logged in|please run /login|invalid api key|oauth (session|token) "
                          r"(has )?expired", re.I)
LOGIN_HINT = {"claude": "claude auth login", "codex": "codex login", "grok": "grok login"}


def refresh_failure(d):
    """The last background-refresh failure, or None once a later render replaced it."""
    result = load_json(os.path.join(d, "refresh-result.json"), {}) or {}
    last_render = (load_json(os.path.join(d, "state.json"), {}) or {}).get("last_render")
    return result if result.get("error") and result.get("last_render") == last_render else None


def refresh_failure_error(agent, code, output):
    """One line for the user: a login problem says how to log in, anything else points at bg.log."""
    if AUTH_FAILURE.search(output or ""):
        return f"the {agent} CLI is not logged in; run `{LOGIN_HINT.get(agent, agent + ' login')}` in a terminal"
    if code:
        return f"{agent} exited {code}; see bg.log"
    return "the refresh did not update the map; see bg.log"


def record_refresh_failure(repo, d, agent, code, error):
    """Count failures in a row and back off: every_minutes, x2 each time, capped; pause after REFRESH_PAUSE_AFTER.
    Notifies on the first failure and when retries pause, not on every retry."""
    cfg = load_config(d)
    n = int((refresh_failure(d) or {}).get("failures") or 0) + 1
    wait = min(cfg.get("every_minutes", 20) * 2 ** (n - 1), REFRESH_BACKOFF_MAX_MINUTES)
    paused = n >= REFRESH_PAUSE_AFTER
    now = dt.datetime.now().astimezone()
    result = {"agent": agent, "finished_at": now.isoformat(timespec="seconds"), "exit_code": code,
              "last_render": (load_json(os.path.join(d, "state.json"), {}) or {}).get("last_render"),
              "error": error, "failures": n, "paused": paused,
              "retry_after": None if paused else (now + dt.timedelta(minutes=wait)).isoformat(timespec="seconds")}
    dump_json(os.path.join(d, "refresh-result.json"), result)
    if not (n == 1 or paused) or not cfg.get("notify", True):
        return f"failed {n}x, quiet"
    then = (f"retries paused after {n} failures; fix it, then run `pmap.py auto on`" if paused
            else f"next try in {wait} min at the earliest")
    send_notification(f"you-are-here · {os.path.basename(repo)}: background refresh failed", f"{error} ({then})")
    return f"failed {n}x"


def refresh_retry_blocked(d):
    """Why a new background refresh must wait after failures, or None when it may start."""
    fail = refresh_failure(d)
    if not fail:
        return None
    if fail.get("paused"):
        return "paused"
    try:
        if dt.datetime.now().astimezone() < dt.datetime.fromisoformat(fail.get("retry_after") or ""):
            return "backoff"
    except ValueError:
        pass
    return None


def load_config(d):
    return {**AUTO_DEFAULTS, **(load_json(os.path.join(d, "config.json"), {}) or {})}


def cmd_auto(a):
    """Per-repo switch for background refreshes started by the plugin hooks."""
    repo = repo_root(a.repo)
    d = out_dir(repo, a.out)
    cfg = load_config(d)
    if a.state in ("on", "off"):
        cfg["auto"] = a.state == "on"
    if a.every:
        cfg["every_minutes"] = a.every
    if a.agent:
        cfg["agent"] = a.agent
    if a.state or a.every or a.agent:
        dump_json(os.path.join(d, "config.json"), cfg)
    if a.state == "on":
        result_path = os.path.join(d, "refresh-result.json")
        if os.path.exists(result_path):
            os.remove(result_path)  # an explicit enable allows one new attempt
    print(json.dumps(cfg))


def pending_answer_lines(d):
    """Decisions the user answered after the last render."""
    since = (load_json(os.path.join(d, "state.json"), {}) or {}).get("last_render") or ""
    return [f"User answered (not yet on the map): {x.get('question')} -> {x.get('answer')}"
            for x in load_answers(d) if (x.get("at") or "") >= since]


def summary_text(repo, d, budget=1800):
    """A few lines about the current map, for hooks to hand to the agent at session start."""
    m = load_json(os.path.join(d, "map.json"))
    st = staleness(repo, d)
    if not isinstance(m, dict) or st["status"] == "none":
        return None
    lines = [f"you-are-here: project map {os.path.join(d, 'index.html')} "
             f"({st['status']}, git only{', ' + st['reason'] if st.get('reason') else ''}).",
             f"GitHub last collected: {st.get('github_collected_at') or 'not collected'}. Collect again before claims about current PR/CI status."]
    fail = refresh_failure(d)
    if fail:
        then = ("Automatic retries are paused; fix the cause, then refresh in this session or run `pmap.py auto on`."
                if fail.get("paused") else f"Next automatic try no earlier than {fail.get('retry_after')}.")
        lines.append(f"Background refresh failed {fail.get('failures') or 1}x in a row "
                     f"(last {fail.get('finished_at')}): {fail['error']}. {then}")
    as_of = fmt_time(st.get("last_render")) or "an unknown time"
    if st.get("severe"):
        lines.append(f"The map was last updated {as_of} and is too far behind to quote: do not treat its headline, "
                     "PR/CI state, progress or decisions as current. Refresh it with the you-are-here skill before "
                     "relying on it.")
        lines += pending_answer_lines(d)  # newer than the map, so still worth passing on
        return "\n".join(lines)[:budget]
    if isinstance(m.get("headline"), str):
        lines.append(f"Headline (as of {as_of}): {m['headline']}")
    cur = next((s.get("name") for s in m.get("milestones") or [] if isinstance(s, dict) and s.get("status") == "current"), None)
    rem = [r.get("label") for r in m.get("remaining") or [] if isinstance(r, dict) and r.get("status") != "done"]
    if cur:
        lines.append(f"Current milestone: {cur}; {len(rem)} left: {', '.join(map(str, rem[:6]))}")
    bn = next((n.get("label") for n in (m.get("chain") or {}).get("nodes") or [] if isinstance(n, dict) and n.get("bottleneck")), None)
    if bn:
        lines.append(f"Bottleneck: {bn}")
    ns = m.get("next_step") if isinstance(m.get("next_step"), dict) else {}
    if ns.get("title"):
        lines.append(f"Next step ({ns.get('owner') or 'unassigned'}): {ns['title']}")
    for q in (m.get("decisions") or [])[:4]:
        if isinstance(q, dict) and q.get("question"):
            mode = f"default '{q.get('default')}' goes ahead {q.get('deadline')}" if q.get("deadline") and q.get("reversible") is True \
                else f"waits for the user (default '{q.get('default')}')"
            lines.append(f"Decision: {q['question']} - {mode}")
    lines += pending_answer_lines(d)
    stuck = [p.get("name") for p in m.get("parts") or [] if isinstance(p, dict) and p.get("status") == "stuck"]
    if stuck:
        lines.append(f"Stuck: {', '.join(map(str, stuck))}")
    text = "\n".join(lines)
    return text[:budget]


INDEX_SKIP = SKIP_DIRS | {"Library", "Applications", ".Trash"}


def find_maps(root, depth=4):
    """Repos under root that have a map: <repo>/.project-map/map.json (default output dir only)."""
    found = []
    root = os.path.abspath(os.path.expanduser(root))
    for cur, dirs, files in os.walk(root):
        rel = os.path.relpath(cur, root)
        level = 0 if rel == "." else rel.count(os.sep) + 1
        if os.path.isfile(os.path.join(cur, ".project-map", "map.json")):
            found.append(cur)
        dirs[:] = [x for x in sorted(dirs) if x not in INDEX_SKIP and not x.startswith(".") and not x.endswith(".wt")]
        if level >= depth:
            dirs[:] = []
    return found


def index_row(repo):
    d = os.path.join(repo, ".project-map")
    m = load_json(os.path.join(d, "map.json"), {}) or {}
    if validate(m):
        return {"repo": repo, "name": os.path.basename(repo), "broken": True}
    st = staleness(repo, d)
    cur = next((x for x in m.get("milestones") or [] if x.get("status") == "current"), None)
    rem = [x for x in m.get("remaining") or [] if x.get("status") != "done"]
    bn = next((n for n in (m.get("chain") or {}).get("nodes") or [] if n.get("bottleneck")), None)
    answers = load_answers(d)
    decisions = [q for q in m.get("decisions") or [] if not answer_for(q, answers)]
    return {
        "repo": repo, "name": (m.get("project") or {}).get("name") or os.path.basename(repo),
        "page": os.path.join(d, "index.html"), "headline": m.get("headline") or "",
        "milestone": cur.get("name") if cur else "", "left": len(rem),
        "bottleneck": bn.get("label") if bn else "", "waits": sum(1 for q in decisions if not q.get("deadline")),
        "auto_defaults": sum(1 for q in decisions if q.get("deadline")),
        "stuck": sum(1 for x in m.get("parts") or [] if x.get("status") == "stuck"),
        "status": st["status"], "reason": st.get("reason") or "", "updated": m.get("generated_at") or st.get("last_render"),
        "next": (m.get("next_step") or {}).get("title") or "", "owner": (m.get("next_step") or {}).get("owner") or "",
        "auto": bool(load_config(d).get("auto")),
    }


def render_index(rows, root, style, label=None):
    L = TEXT.get(style.get("lang") or "zh", TEXT["zh"])
    zh = L is TEXT["zh"]
    rows = sorted(rows, key=lambda x: (-x.get("waits", 0), -x.get("stuck", 0), x.get("status") != "stale",
                                       str(x.get("updated") or "")), reverse=False)
    cards = []
    for x in rows:
        href = "file://" + urllib.parse.quote(x.get("page", ""))
        if x.get("broken"):
            cards.append(f'<li class="row broken"><b>{html.escape(x["name"])}</b> <span>map.json '
                         f'{"无效，重新画一张" if zh else "is invalid; redraw it"}</span></li>')
            continue
        chips = []
        if x["waits"]:
            chips.append(f'<span class="c you"><b>{x["waits"]}</b> {L["needs_you"]}</span>')
        if x["stuck"]:
            chips.append(f'<span class="c stuck"><b>{x["stuck"]}</b> {L["stuck"]}</span>')
        stale = x["status"] == "stale"
        chips.append(f'<span class="c {"old" if stale else "ok"}">'
                     f'{("过时" if zh else "stale") if stale else ("最新" if zh else "fresh")}'
                     f'{" · " + html.escape(x["reason"]) if stale and x["reason"] else ""}</span>')
        if x["auto"]:
            chips.append(f'<span class="c">{"自动更新" if zh else "auto"}</span>')
        ms = (f'<p class="ms">{html.escape(x["milestone"])} · {"还剩" if zh else "left"} <b>{x["left"]}</b>'
              f'{" · " + L["bottleneck"] + " " + html.escape(x["bottleneck"]) if x["bottleneck"] else ""}</p>')
        nxt = (f'<p class="nx">{L["next_step"]}{" (" + L["owner_" + x["owner"]] + ")" if x["owner"] in OWNERS else ""}: '
               f'{html.escape(x["next"])}</p>') if x["next"] else ""
        cards.append(f'<li class="row"><a href="{html.escape(href, quote=True)}"><div class="top"><b>{html.escape(x["name"])}</b>'
                     f'<span class="t">{fmt_time(x["updated"]) if x["updated"] else ""}</span></div>'
                     f'<p class="hl">{html.escape(x["headline"])}</p>{ms}{nxt}<div class="chips">{"".join(chips)}</div></a></li>')
    title = "所有项目" if zh else "All projects"
    waiting = sum(x.get("waits", 0) for x in rows)
    sub = (f'{len(rows)} 个项目 · {waiting} 件事等你 · {sum(1 for x in rows if x.get("status") == "stale")} 张地图过时'
           if zh else f'{len(rows)} projects · {waiting} waiting on you · {sum(1 for x in rows if x.get("status") == "stale")} stale maps')
    home = os.path.expanduser("~")
    shown = label or (("~" + root[len(home):]) if root.startswith(home) else root)
    body = (f'<header><h1>{title}</h1><p class="sub">{html.escape(shown)} · {sub}</p></header>'
            f'<ul class="rows">{"".join(cards) or "<li>" + ("还没有地图" if zh else "No maps yet") + "</li>"}</ul>')
    css = page_css(style["accent"]).split("*{box-sizing")[0]
    return (f'<!doctype html><html lang="{"zh-CN" if zh else "en"}" data-theme="{html.escape(style["theme"])}"><head>'
            f'<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">'
            f'<title>{title} · you-are-here</title><style>{css}' + INDEX_CSS + '</style></head>'
            f'<body><div class="wrap">{body}</div></body></html>')


INDEX_CSS = """*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.55 -apple-system,BlinkMacSystemFont,"PingFang SC","Segoe UI",system-ui,sans-serif}
.wrap{max-width:980px;margin:0 auto;padding:28px 16px 40px} h1{margin:0;font-size:26px} .sub{color:var(--mut);margin:4px 0 20px}
.rows{list-style:none;margin:0;padding:0;display:grid;gap:10px}
.row a{display:block;color:inherit;text-decoration:none;background:var(--card);border:1px solid var(--line);border-radius:12px;padding:14px 16px}
.row a:hover,.row a:focus-visible{border-color:var(--accent);outline:none}
.top{display:flex;justify-content:space-between;gap:12px} .top b{font-size:16px} .t{color:var(--dim);font-size:12px}
.hl{margin:6px 0;font-size:15px} .ms,.nx{margin:2px 0;color:var(--mut);font-size:13px} .ms b{color:var(--fg)}
.chips{display:flex;flex-wrap:wrap;gap:6px;margin-top:8px}
.c{font-size:12px;border:1px solid var(--line);border-radius:999px;padding:1px 9px}
.c.you{border-color:color-mix(in srgb,var(--stuck) 50%,var(--line))} .c.you b,.c.stuck b{color:var(--stuck)}
.c.old{color:var(--prog)} .c.ok{color:var(--done)} .broken{color:var(--mut);padding:10px 16px}
"""


def cmd_index(a):
    root = os.path.abspath(os.path.expanduser(a.root))
    rows = []
    for repo in find_maps(root, a.depth):
        try:
            rows.append(index_row(repo))
        except Exception as e:  # one bad repo must not hide the others
            rows.append({"repo": repo, "name": os.path.basename(repo), "broken": True, "error": repr(e)})
    out = os.path.abspath(os.path.expanduser(a.out)) if a.out else os.path.join(
        os.path.expanduser("~/.cache/you-are-here"), "index.html")
    style = load_style({"style": {"lang": a.lang}} if a.lang else {})
    write_text(out, render_index(rows, root, style, a.label))
    if a.json:
        print(json.dumps(rows, ensure_ascii=False, indent=1))
    for x in rows:
        if not x.get("broken"):
            print(f"{x['name']}: {x['status']}, {x['waits']} waiting, {x['stuck']} stuck - {x['headline'][:70]}")
    print(f"index: {out}")


def load_answers(d):
    data = load_json(os.path.join(d, "answers.json"), []) or []
    return [x for x in data if isinstance(x, dict) and x.get("answer")]


def answer_for(q, answers):
    """The latest answer to a decision, matched by id or by question text."""
    hit = None
    for x in answers:
        if (q.get("id") and x.get("id") == q.get("id")) or (x.get("question") or "").strip() == (q.get("question") or "").strip():
            hit = x
    return hit


def cmd_answer(a):
    """Record the user's answer to a decision, so every agent (and background refreshes) can act on it."""
    repo = repo_root(a.repo)
    d = out_dir(repo, a.out)
    m = load_json(os.path.join(d, "map.json"), {}) or {}
    q = None
    for i, x in enumerate(m.get("decisions") or []):
        if (a.id and (x.get("id") == a.id or f"d{i}" == a.id)) or (a.question and x.get("question", "").strip() == a.question.strip()):
            q = x
    if not q and not a.question:
        sys.exit("pmap: no decision with that id in map.json; pass --question with the question text")
    entry = {"id": (q or {}).get("id") or a.id, "question": (q or {}).get("question") or a.question,
             "answer": a.answer, "at": now_iso()}
    with locked(d):
        answers = load_json(os.path.join(d, "answers.json"), []) or []
        answers.append(entry)
        dump_json(os.path.join(d, "answers.json"), answers)
    print(f"recorded: {entry['question']} -> {entry['answer']}")


def cmd_summary(a):
    repo = repo_root(a.repo)
    d = os.path.abspath(a.out) if a.out else os.path.join(repo, ".project-map")
    t = summary_text(repo, d)
    print(t if t else "no map yet")


def cmd_style(a):
    s = load_json(STYLE_FILE, {}) or {}
    changed = False
    for k in ("theme", "accent", "lang"):
        v = getattr(a, k)
        if v:
            s[k] = v
            changed = True
    if changed:
        dump_json(STYLE_FILE, s)
    print(json.dumps({**DEFAULT_STYLE, **s}, ensure_ascii=False))
    print(f"({'saved to' if changed else 'stored at'} {STYLE_FILE}{'' if s else ', not set yet: using defaults'})")


def utf8_console():
    """Windows consoles default to a legacy code page; map text (Chinese, arrows) must still print."""
    for stream in (sys.stdout, sys.stderr):
        try:
            if (stream.encoding or "").lower().replace("-", "") != "utf8":
                stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


def main():
    utf8_console()
    ap = argparse.ArgumentParser(prog="pmap", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sp = ap.add_subparsers(dest="cmd", required=True)
    for name, fn in (("collect", cmd_collect), ("validate", cmd_validate), ("render", cmd_render)):
        p = sp.add_parser(name)
        p.add_argument("repo", nargs="?", default=".")
        p.add_argument("--out", help="output dir (default <repo>/.project-map)")
        if name == "collect":
            p.add_argument("--no-gh", action="store_true", help="skip GitHub issues/PRs/CI")
        p.set_defaults(fn=fn)
    p = sp.add_parser("status", help="is the map missing, fresh or stale? (no network)")
    p.add_argument("repo", nargs="?", default=".")
    p.add_argument("--out")
    p.add_argument("--commits", type=int, default=1, help="stale after this many new commits (default 1)")
    p.add_argument("--hours", type=float, default=12, help="stale after this many hours with any new change")
    p.add_argument("--files", type=int, default=1, help="stale after this many edited files (default 1)")
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_status)
    p = sp.add_parser("auto", help="turn background refreshes from the plugin hooks on/off for this repo")
    p.add_argument("state", nargs="?", choices=("on", "off"))
    p.add_argument("repo", nargs="?", default=".")
    p.add_argument("--out")
    p.add_argument("--every", type=int, help="minimum minutes between background refreshes (default 20)")
    p.add_argument("--agent", choices=("same", "claude", "codex", "grok"), help="CLI that runs the refresh")
    p.set_defaults(fn=cmd_auto)
    p = sp.add_parser("summary", help="a few lines about the map, for hooks")
    p.add_argument("repo", nargs="?", default=".")
    p.add_argument("--out")
    p.set_defaults(fn=cmd_summary)
    p = sp.add_parser("answer", help="record the user's answer to a decision on the map")
    p.add_argument("repo", nargs="?", default=".")
    p.add_argument("--out")
    p.add_argument("--id", help="decision id (or d0, d1 ... by position)")
    p.add_argument("--question", help="decision question text, when there is no id")
    p.add_argument("--answer", required=True)
    p.set_defaults(fn=cmd_answer)
    p = sp.add_parser("index", help="one page over every repo under a folder that has a map")
    p.add_argument("root", nargs="?", default=".")
    p.add_argument("--out", help="output HTML (default ~/.cache/you-are-here/index.html)")
    p.add_argument("--depth", type=int, default=4, help="how deep to look for repos (default 4)")
    p.add_argument("--lang", choices=("zh", "en"))
    p.add_argument("--label", help="folder name to show on the page (default: the path, ~ for home)")
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_index)
    p = sp.add_parser("style")
    p.add_argument("--theme", choices=("dark", "light", "auto"))
    p.add_argument("--accent")
    p.add_argument("--lang", choices=("zh", "en"))
    p.set_defaults(fn=cmd_style)
    a = ap.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()
