#!/usr/bin/env python3
"""you-are-here: collect repo facts, then render an agent-written map.json to one HTML file.

Agent-neutral (Claude Code, Codex, Grok, ...). Python 3.8+ stdlib only.

  pmap.py collect  <repo> [--out DIR] [--no-gh]   -> DIR/facts.json (+ delta since last run)
  pmap.py validate <repo> [--out DIR]             -> check DIR/map.json against the schema
  pmap.py render   <repo> [--out DIR]             -> DIR/index.html, diffed against the last render
  pmap.py status   <repo> [--out DIR] [--json]    -> none | fresh | stale (git only, no network)
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

SCHEMA_VERSION = 1
PART_STATUS = ("done", "in_progress", "not_started", "stuck")
MS_STATUS = ("done", "current", "future")
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
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
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
    ok, raw = git(repo, "status", "--porcelain=v1", "-z", "--untracked-files=normal", strip=False)
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
    for _, path in entries[:500]:
        try:
            st = os.stat(os.path.join(repo, path))
            fp[path] = [int(st.st_mtime), st.st_size]
        except OSError:
            fp[path] = None  # deleted
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


def collect_github(repo_arg, warnings):
    if not shutil.which("gh"):
        warnings.append("gh not installed: issues/PRs/CI skipped")
        return None

    def gh_json(args, what, timeout=20):
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
    """What changed on GitHub since the last rendered map: PRs, issues, default-branch CI."""
    out = {}
    op = {p["number"]: p for p in old.get("prs") or []}
    np_ = {p["number"]: p for p in new.get("prs") or []}
    out["prs_opened"] = [f"#{n} {np_[n]['title']}" for n in sorted(np_.keys() - op.keys())]
    out["prs_gone"] = [f"#{n} {op[n]['title']} (merged or closed)" for n in sorted(op.keys() - np_.keys())]
    changed = []
    for n in sorted(np_.keys() & op.keys()):
        a, b = op[n], np_[n]
        bits = [f"{k}: {a.get(k)} -> {b.get(k)}" for k in ("checks", "review", "merge_state", "draft") if a.get(k) != b.get(k)]
        if bits:
            changed.append(f"#{n}: " + "; ".join(bits))
    out["prs_changed"] = changed
    oi = {i["number"] for i in old.get("issues") or []}
    ni = {i["number"]: i for i in new.get("issues") or []}
    out["issues_opened"] = [f"#{n} {ni[n]['title']}" for n in sorted(ni.keys() - oi)]
    out["issues_closed"] = [f"#{n}" for n in sorted(oi - ni.keys())]
    seen = {r["id"] for r in old.get("default_branch_runs") or []}
    out["new_ci_runs"] = [f"{r['workflow']} {r['sha']}: {r['conclusion'] or r['status']}"
                          for r in new.get("default_branch_runs") or [] if r["id"] not in seen]
    return {k: v for k, v in out.items() if v} or "no changes"


def path_matcher(patterns):
    """parts[].paths entries: a directory/file prefix, or a glob (`*`, `?`, `[`). Leading ./ is ignored."""
    pre, globs = [], []
    for x in patterns or []:
        if not isinstance(x, str) or not x.strip():
            continue
        x = x.strip()
        while x.startswith("./"):
            x = x[2:]
        (globs if any(c in x for c in "*?[") else pre).append(x.rstrip("/"))

    def match(f):
        return (any(f == x or f.startswith(x + "/") for x in pre if x)
                or any(fnmatch.fnmatchcase(f, g) or fnmatch.fnmatchcase(f, g.rstrip("/*") + "/*") for g in globs))
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
    gh_repo = g.pop("_gh_repo")
    if gh_repo and not no_gh:
        facts["github"] = collect_github(gh_repo, warnings)
    fp = g.pop("_worktree_fp")
    tags = tag_names(repo)
    if incremental:
        hits, unmapped = touched_parts(prev_map, g["changed_files"])
        old_fp = baseline.get("worktree_fp")
        if old_fp is not None:
            wt = sorted(p for p in fp.keys() | old_fp.keys() if fp.get(p) != old_fp.get(p))
            wt_hits, _ = touched_parts(prev_map, wt)
            for pid, n in wt_hits.items():
                hits[pid] = hits.get(pid, 0) + n
        else:
            wt = None
        facts["delta"] = {"touched_parts": hits, "unmapped_files": unmapped[:80],
                          "worktree_changed": wt[:80] if wt is not None else "unknown (no baseline yet)",
                          "new_tags": sorted(set(tags) - set(baseline.get("tags") or [])) if "tags" in baseline else [],
                          "untouched_parts": [p.get("id") for p in prev_map.get("parts") or []
                                              if isinstance(p, dict) and p.get("id") not in hits]}
        if facts.get("github") and baseline.get("github"):
            facts["delta"]["github"] = github_delta(baseline["github"], facts["github"])
    # the baseline the *next* run compares against; promoted to baseline.json when this map is rendered
    dump_json(os.path.join(d, "pending_baseline.json"),
              {"refs": ref_shas(repo), "tags": tags, "worktree_fp": fp, "github": facts.get("github")})
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
        for k in ("summary", "waiting_on"):
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
    if ms and cur != 1:
        errs.append("milestones: exactly one must be status current")
    for i, r in dicts(m, "remaining", ""):
        need(r, "label", f"remaining[{i}]")
        opt(r, "note", f"remaining[{i}]", str)
        if r.get("status") not in PART_STATUS:
            errs.append(f"remaining[{i}].status: one of {PART_STATUS}")

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

    if isinstance(m.get("next_step"), dict):
        need(m["next_step"], "title", "next_step")
        opt(m["next_step"], "why", "next_step", str)
    else:
        errs.append("next_step: required {title, why}")
    for i, q in dicts(m, "decisions", ""):
        w = f"decisions[{i}]"
        need(q, "question", w)
        need(q, "default", w)
        opt(q, "deadline", w, str)
        opt(q, "proposed", w, bool)
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
        if np_ and (op.get("done"), op.get("total")) != (np_.get("done"), np_.get("total")):
            bits.append(f"{op.get('done', '?')}/{op.get('total', '?')} → {np_['done']}/{np_['total']}")
        if bits:
            marks[p["id"]] = " · ".join(bits)
            lines.append((p.get("status"), f"{p['name']}: {' · '.join(bits)}"))
    for pid, o in old.items():
        if pid not in {p["id"] for p in (cur.get("parts") or [])}:
            lines.append(("not_started", f"{o['name']}: {L['removed']}"))
    om = {s["name"]: s.get("status") for s in (prev.get("milestones") or [])}
    for s in (cur.get("milestones") or []):
        if s["name"] in om and om[s["name"]] != s.get("status"):
            lines.append(("done", f"{L['milestone']} {s['name']}: {L['ms_' + om[s['name']]]} → {L['ms_' + s['status']]}"))
    def bottleneck(m):
        return next((n["label"] for n in ((m.get("chain") or {}).get("nodes") or []) if n.get("bottleneck")), None)
    ob, nb = bottleneck(prev), bottleneck(cur)
    if ob != nb and nb:
        lines.append(("stuck", f"{L['bottleneck']}: {ob or '—'} → {nb}"))
    onodes = {n["id"]: n for n in ((prev.get("chain") or {}).get("nodes") or [])}
    for n in ((cur.get("chain") or {}).get("nodes") or []):
        o = onodes.get(n["id"])
        if o and o.get("status") != n.get("status") and n.get("status") in PART_STATUS and o.get("status") in PART_STATUS:
            lines.append((n["status"], f"{n['label']}: {L[o['status']]} → {L[n['status']]}"))
    orem = {r["label"]: r.get("status") for r in (prev.get("remaining") or [])}
    nrem = {r["label"]: r.get("status") for r in (cur.get("remaining") or [])}
    for label in orem.keys() - nrem.keys():
        lines.append(("done", f"{L['remaining']}: {label} ✓"))
    for label in nrem.keys() - orem.keys():
        lines.append(("new", f"{L['remaining']}: + {label}"))
    if (prev.get("next_step") or {}).get("title") != (cur.get("next_step") or {}).get("title"):
        lines.append(("in_progress", f"{L['next_step']}: {cur['next_step']['title']}"))
    return marks, lines


# ---------------------------------------------------------------- render

TEXT = {
    "zh": {"done": "完成", "in_progress": "进行中", "not_started": "未开始", "stuck": "卡住",
           "ms_done": "已完成", "ms_current": "进行中", "ms_future": "未开始",
           "title": "项目地图", "updated": "更新", "commits_since": "次提交（自上次）", "commits_recent": "次近期提交",
           "you_are_here": "当前位置", "left_for": "距", "items": "项", "proposed": "提议", "inferred": "推断",
           "chain": "通往下一里程碑的阻塞链", "bottleneck": "当前瓶颈", "evidence_edge": "有证据的依赖",
           "inferred_edge": "推断的顺序", "no_edge": "未声明依赖", "other_deps": "其他依赖", "why": "依据",
           "parts": "组成部分", "of_done": "完成", "next_step": "建议的下一步", "decisions": "需要你决定",
           "default": "默认", "by": "截止", "changes": "自上次以来的变化", "first_run": "首次生成，暂无对比",
           "recent_commits": "最近提交", "uncertain": "不确定 / 推断", "details": "明细", "new": "新增",
           "removed": "已移除", "milestone": "里程碑", "waiting": "等待", "worktree": "工作区未提交",
           "sources": "来源", "warnings": "收集警告", "changed": "有变化", "remaining": "剩余项"},
    "en": {"done": "Done", "in_progress": "In progress", "not_started": "Not started", "stuck": "Stuck",
           "ms_done": "Done", "ms_current": "In progress", "ms_future": "Later",
           "title": "Project map", "updated": "Updated", "commits_since": "commits since last", "commits_recent": "recent commits",
           "you_are_here": "You are here", "left_for": "Left for", "items": "", "proposed": "proposed", "inferred": "inferred",
           "chain": "Blockers to the next milestone", "bottleneck": "Bottleneck", "evidence_edge": "Evidenced dependency",
           "inferred_edge": "Inferred order", "no_edge": "No declared dependency", "other_deps": "Other dependencies",
           "why": "Why", "parts": "Parts", "of_done": "done", "next_step": "Next step", "decisions": "Needs your call",
           "default": "Default", "by": "by", "changes": "Changed since last update", "first_run": "First map, nothing to compare",
           "recent_commits": "Recent commits", "uncertain": "Uncertain / inferred", "details": "Details", "new": "New",
           "removed": "Removed", "milestone": "Milestone", "waiting": "Waiting on", "worktree": "Uncommitted",
           "sources": "Sources", "warnings": "Collector warnings", "changed": "Changed", "remaining": "Remaining"},
}


def labels(m):
    return TEXT.get((m.get("style") or {}).get("lang") or m.get("lang") or "zh", TEXT["zh"])


class R:
    """Escaping + reference linking for user-visible text."""

    def __init__(self, web, shas):
        self.web, self.shas = web, shas

    def __call__(self, s):
        t = html.escape(str(s or ""))
        t = re.sub(r"`([^`]+)`", r"<code>\1</code>", t)
        if self.web:
            base = html.escape(self.web, quote=True)
            t = re.sub(r"(?<![\w/&;])#(\d{1,6})\b",
                       lambda m: f'<a href="{base}/issues/{m.group(1)}">#{m.group(1)}</a>', t)
            t = re.sub(r"\b([0-9a-f]{7,12})\b",
                       lambda m: f'<a href="{base}/commit/{m.group(1)}"><code>{m.group(1)}</code></a>'
                       if m.group(1)[:7] in self.shas else m.group(0), t)
        return t


def fmt_time(iso):
    try:
        return dt.datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone().strftime("%m-%d %H:%M")
    except (ValueError, AttributeError):
        return iso or ""


def cls(v, allowed, fallback):
    """A CSS class fragment from a fixed vocabulary; anything else (e.g. injected markup) falls back."""
    return v if v in allowed else fallback


def badge(status, L):
    return f'<span class="badge s-{status}">{L[status]}</span>'


def render_html(m, facts, marks, change_lines, style, prev_time):
    L = labels(m)
    g = (facts or {}).get("git", {})
    slug = g.get("github")
    web = g.get("github_web") or (f"https://github.com/{slug}" if slug else None)
    shas = {c["sha"][:7] for c in g.get("commits") or []}
    r = R(web, shas)
    P = m["project"]
    parts = m["parts"]
    n_done = sum(p["status"] == "done" for p in parts)
    n_stuck = sum(p["status"] == "stuck" for p in parts)

    # header
    meta = [f'{L["updated"]} <b>{fmt_time(m.get("generated_at") or now_iso())}</b>']
    if facts:
        n = len(g.get("commits", []))
        meta.append(f'<b>{n}</b> {L["commits_since"] if facts.get("mode") == "incremental" else L["commits_recent"]}')
        wt = g.get("worktree", {})
        if wt.get("modified") or wt.get("untracked"):
            meta.append(f'{L["worktree"]} <b>{wt.get("modified", 0) + wt.get("untracked", 0)}</b>')
    meta.append(f'<b>{n_done}/{len(parts)}</b> {L["of_done"]}')
    if n_stuck:
        meta.append(f'<span class="stuck-dot"></span><b class="c-stuck">{n_stuck} {L["stuck"]}</b>')
    repo_link = (f' <a class="slug" href="{html.escape(web, quote=True)}">{html.escape(slug)}</a>'
                 if slug and web else "")
    head = (f'<header><h1>{r(P["name"])}<span class="sub">{L["title"]}</span></h1>'
            f'<p class="summary">{r(P.get("summary", ""))}{repo_link}</p>'
            f'<p class="meta">{" · ".join(meta)}</p></header>')

    # milestone track
    track = ""
    ms = m.get("milestones") or []
    if ms:
        nodes = []
        for i, s in enumerate(ms):
            st = cls(s["status"], MS_STATUS, "future")
            seg = ""
            if i < len(ms) - 1:
                nxt = ms[i + 1]["status"]
                seg = f'<span class="seg {"solid" if nxt in ("done", "current") else "dotted"}"></span>'
            prop = f'<span class="tag">{L["proposed"]}</span>' if s.get("proposed") else ""
            here = f'<span class="here">{L["you_are_here"]}</span>' if st == "current" else ""
            nodes.append(
                f'<li class="ms ms-{st}"{" id=here" if st == "current" else ""}>{here}<span class="dot"></span>{seg}'
                f'<span class="ms-name">{r(s["name"])}</span>'
                f'<span class="ms-note">{r(s.get("date") or L["ms_" + st])}{" · " + r(s["note"]) if s.get("note") else ""}</span>{prop}</li>')
        rem = m.get("remaining") or []
        cur_name = next((s["name"] for s in ms if s["status"] == "current"), "")
        pills = "".join(f'<span class="pill p-{cls(x["status"], PART_STATUS, "not_started")}"><i></i>{r(x["label"])}'
                        f'{"<em>" + r(x["note"]) + "</em>" if x.get("note") else ""}</span>' for x in rem)
        left = (f'<div class="left"><span class="big">{len(rem)}</span><span class="cap">{L["left_for"]} {r(cur_name)} {L["items"]}</span>'
                f'<div class="pills">{pills}</div></div>') if rem else ""
        track = f'<section class="card track"><ol class="ms-list">{"".join(nodes)}</ol>{left}</section>'

    # blocker chain: nodes in order, connectors from edges between neighbours
    chain = ""
    ch = m.get("chain") or {}
    cn = ch.get("nodes") or []
    if cn:
        edges = {(e["from"], e["to"]): e for e in ch.get("edges") or []}
        used = set()
        items = []
        for i, n in enumerate(cn):
            st = cls(n.get("status"), PART_STATUS, "not_started")
            bn = f'<span class="bn">{L["bottleneck"]}</span>' if n.get("bottleneck") else ""
            items.append(f'<div class="node n-{st}{" is-bn" if n.get("bottleneck") else ""}">{bn}'
                         f'<b>{r(n["label"])}</b><span>{r(n.get("sub", ""))}</span></div>')
            if i < len(cn) - 1:
                e = edges.get((n["id"], cn[i + 1]["id"]))
                kind = cls(e["kind"], EDGE_KIND, "none") if e else "none"
                if e:
                    used.add((e["from"], e["to"]))
                tip = html.escape(e.get("why") or "", quote=True) if e else L["no_edge"]
                items.append(f'<div class="arrow a-{kind}" title="{tip}"><span></span></div>')
        names = {n["id"]: n["label"] for n in cn}
        why = []
        for e in ch.get("edges") or []:
            k = "evidence" if e["kind"] == "evidence" else "inferred"
            extra = "" if (e["from"], e["to"]) in used else f' <span class="tag">{L["other_deps"]}</span>'
            why.append(f'<li class="w-{k}"><b>{r(names[e["from"]])} → {r(names[e["to"]])}</b>{extra}'
                       f'{"：" if L is TEXT["zh"] else ": "}{r(e.get("why") or "")}</li>')
        legend = (f'<div class="legend"><span><i class="lg-e"></i>{L["evidence_edge"]}</span>'
                  f'<span><i class="lg-i"></i>{L["inferred_edge"]}</span>'
                  f'<span><i class="lg-b"></i>{L["bottleneck"]}</span></div>')
        chain = (f'<section class="card"><h2>{r(ch.get("title") or L["chain"])}</h2>'
                 f'<div class="chain">{"".join(items)}</div>{legend}'
                 f'<details class="why"><summary>{L["why"]}（{len(why)}）</summary><ul>{"".join(why)}</ul></details></section>')

    # parts
    cards = []
    order = {"stuck": 0, "in_progress": 1, "not_started": 2, "done": 3}
    for p in sorted(parts, key=lambda p: (order[p["status"]], parts.index(p))):
        prog = ""
        pr = p.get("progress")
        if pr:
            pct = max(0, min(100, round(100 * pr["done"] / pr["total"])))
            prog = (f'<div class="prog"><div class="pl"><span>{r(pr.get("label") or "")}</span>'
                    f'<span>{pr["done"]}/{pr["total"]}</span></div><div class="bar"><i style="width:{pct}%"></i></div></div>')
        chg = f'<span class="chg">{L["changed"]} · {r(marks[p["id"]])}</span>' if p["id"] in marks else ""
        inf = f'<span class="tag">{L["inferred"]}</span>' if p.get("inferred") else ""
        colon = "：" if L is TEXT["zh"] else ": "
        wait = f'<p class="wait">{L["waiting"]}{colon}{r(p["waiting_on"])}</p>' if p.get("waiting_on") else ""
        ev = " · ".join(r(x) for x in (p.get("evidence") or [])[:6])
        cards.append(f'<article class="part pt-{p["status"]}{" changed" if p["id"] in marks else ""}">'
                     f'<div class="ph">{badge(p["status"], L)}{inf}{chg}</div><h3>{r(p["name"])}</h3>'
                     f'<p>{r(p.get("summary", ""))}</p>{prog}{wait}<footer>{ev}</footer></article>')
    parts_html = (f'<section><div class="sh"><h2>{L["parts"]}</h2><span><b>{n_done}</b> / {len(parts)} {L["of_done"]}</span></div>'
                  f'<div class="parts">{"".join(cards)}</div></section>')

    # sidebar
    ns = m["next_step"]
    side = [f'<section class="card next"><h2>{L["next_step"]}</h2><p class="nt">{r(ns["title"])}</p>'
            f'<p>{r(ns.get("why", ""))}</p></section>']
    if m.get("decisions"):
        qs = "".join(
            f'<li><p class="q">{r(q["question"])}{" <span class=tag>" + L["proposed"] + "</span>" if q.get("proposed") else ""}</p>'
            f'<p class="d">{L["by"] + " " + r(q["deadline"]) + " → " if q.get("deadline") else L["default"] + " → "}'
            f'<b>{r(q["default"])}</b></p></li>' for q in m["decisions"])
        side.append(f'<section class="card decide"><h2>{L["decisions"]}</h2><ul>{qs}</ul></section>')
    since = f'<span class="since">{fmt_time(prev_time)}</span>' if prev_time else ""
    if change_lines:
        cl = "".join(f'<li><i class="dot-{cls(k, PART_STATUS + ("new",), "new")}"></i>{r(t)}</li>' for k, t in change_lines)
    else:
        cl = f'<li class="muted">{L["first_run"]}</li>'
    commits = ((facts or {}).get("git") or {}).get("commits") or []
    commits = commits[:6]
    cm = "".join(f'<li><span class="t">{fmt_time(c["date"])}</span> {r(c["sha"][:7])} {r(c["subject"])}</li>'
                 for c in commits)
    side.append(f'<section class="card changes"><h2>{L["changes"]}{since}</h2><ul>{cl}</ul>'
                + (f'<h3>{L["recent_commits"]}</h3><ul class="commits">{cm}</ul>' if cm else "") + "</section>")
    if m.get("uncertain"):
        side.append(f'<section class="card"><h2>{L["uncertain"]}</h2><ul class="plain">'
                    + "".join(f"<li>{r(x)}</li>" for x in m["uncertain"]) + "</ul></section>")

    # detail panels
    det = []
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
    details = f'<section class="details"><h2>{L["details"]}</h2>{"".join(det)}</section>' if det else ""

    foot = []
    if m.get("sources"):
        foot.append(f'{L["sources"]}：' + r("、".join(m["sources"]) if L is TEXT["zh"] else ", ".join(m["sources"])))
    if facts and facts.get("warnings"):
        foot.append(f'{L["warnings"]}：' + r("; ".join(facts["warnings"])))
    footer = "".join(f"<p>{x}</p>" for x in foot)

    return PAGE.format(
        lang="zh-CN" if L is TEXT["zh"] else "en",
        title=html.escape(f'{P["name"]} · {L["title"]}'),
        theme=html.escape(style["theme"]), accent=html.escape(style["accent"]),
        body=f'{head}{track}{chain}<div class="grid"><div class="main">{parts_html}</div>'
             f'<aside>{"".join(side)}</aside></div>{details}<footer class="foot">{footer}</footer>')


PAGE = """<!doctype html>
<html lang="{lang}" data-theme="{theme}">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<style>
:root{{--accent:{accent};--bg:#0e1013;--card:#16191e;--card2:#1b1f25;--line:#2a2f37;--fg:#e8eaed;--mut:#9aa2ad;--dim:#6b7380;
--done:#4ade80;--prog:#fbbf24;--stuck:#f87171;--todo:#6b7380;color-scheme:dark}}
:root[data-theme=light]{{--bg:#f6f7f9;--card:#fff;--card2:#f1f3f6;--line:#dfe3e8;--fg:#14171c;--mut:#566070;--dim:#8a93a0;
--done:#16a34a;--prog:#b7791f;--stuck:#dc2626;--todo:#8a93a0;color-scheme:light}}
@media (prefers-color-scheme:light){{:root[data-theme=auto]{{--bg:#f6f7f9;--card:#fff;--card2:#f1f3f6;--line:#dfe3e8;--fg:#14171c;
--mut:#566070;--dim:#8a93a0;--done:#16a34a;--prog:#b7791f;--stuck:#dc2626;--todo:#8a93a0;color-scheme:light}}}}
*{{box-sizing:border-box}}
body{{margin:0;background:var(--bg);color:var(--fg);font:14px/1.55 -apple-system,BlinkMacSystemFont,"PingFang SC","Segoe UI",system-ui,sans-serif}}
.wrap{{max-width:1240px;margin:0 auto;padding:28px 16px 40px}}
a{{color:inherit;text-decoration:none;border-bottom:1px dotted var(--dim)}} a:hover{{color:var(--accent)}}
code{{font:12px ui-monospace,SFMono-Regular,Menlo,monospace;background:var(--card2);padding:1px 5px;border-radius:4px}}
h1{{font-size:28px;margin:0;letter-spacing:-.02em}} h1 .sub{{font-size:15px;color:var(--mut);font-weight:400;margin-left:12px}}
h2{{font-size:12px;letter-spacing:.08em;text-transform:uppercase;color:var(--mut);margin:0 0 12px;font-weight:600}}
h3{{margin:0}}
.summary{{color:var(--mut);margin:6px 0 2px}} .slug{{margin-left:8px;font:12px ui-monospace,Menlo,monospace}}
.meta{{color:var(--mut);margin:0 0 20px}} .meta b{{color:var(--fg)}} .c-stuck{{color:var(--stuck)!important}}
.stuck-dot{{display:inline-block;width:8px;height:8px;border-radius:50%;background:var(--stuck);margin-right:6px}}
.card{{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:18px 20px;margin-bottom:16px}}
.tag{{font-size:11px;color:var(--mut);border:1px dashed var(--dim);border-radius:5px;padding:0 5px;margin-left:6px;white-space:nowrap}}
/* milestone track */
.track{{padding-top:22px}}
.ms-list{{list-style:none;margin:0;padding:34px 4px 4px;display:flex;overflow-x:auto;scrollbar-width:thin}}
.ms{{position:relative;flex:1 0 128px;display:flex;flex-direction:column;align-items:center;text-align:center;padding:0 6px}}
.ms .dot{{width:30px;height:30px;border-radius:50%;border:2px solid var(--line);background:var(--card2);z-index:1;display:grid;place-items:center}}
.ms-done .dot{{background:color-mix(in srgb,var(--done) 22%,var(--card));border-color:var(--done)}}
.ms-done .dot::after{{content:"";width:9px;height:5px;border:solid var(--done);border-width:0 0 2.5px 2.5px;transform:rotate(-45deg) translate(1px,-1px)}}
.ms-current .dot{{border-color:var(--accent);box-shadow:0 0 0 6px color-mix(in srgb,var(--accent) 18%,transparent)}}
.ms-current .dot::after{{content:"";width:10px;height:10px;border-radius:50%;background:var(--accent)}}
.seg{{position:absolute;top:14px;left:calc(50% + 17px);right:calc(-50% + 17px);height:3px;border-radius:2px}}
.seg.solid{{background:var(--done)}} .seg.dotted{{background:repeating-linear-gradient(90deg,var(--dim) 0 3px,transparent 3px 9px);height:2px}}
.ms-current .seg{{background:repeating-linear-gradient(90deg,var(--dim) 0 3px,transparent 3px 9px);height:2px}}
.ms-name{{margin-top:10px;font-weight:600}} .ms-current .ms-name{{color:var(--accent)}}
.ms-note{{font-size:12px;color:var(--mut)}} .ms-done .ms-note{{color:var(--done)}}
.here{{position:absolute;top:-34px;background:var(--accent);color:#06221e;font-size:12px;font-weight:600;padding:3px 9px;border-radius:7px;white-space:nowrap}}
.left{{display:flex;flex-wrap:wrap;align-items:center;gap:10px 14px;margin-top:20px;padding-top:16px;border-top:1px solid var(--line)}}
.big{{font-size:34px;font-weight:700;color:var(--accent);line-height:1}} .cap{{font-size:12px;letter-spacing:.06em;color:var(--mut);text-transform:uppercase}}
.pills{{display:flex;flex-wrap:wrap;gap:8px}}
.pill{{display:inline-flex;align-items:center;gap:6px;font-size:13px;border:1px solid var(--line);border-radius:999px;padding:3px 11px;background:var(--card2)}}
.pill i{{width:7px;height:7px;border-radius:50%;background:var(--todo)}} .pill em{{font-style:normal;color:var(--mut);font-size:11px}}
.p-stuck{{border-color:color-mix(in srgb,var(--stuck) 55%,transparent)}} .p-stuck i{{background:var(--stuck)}}
.p-in_progress{{border-color:color-mix(in srgb,var(--prog) 55%,transparent)}} .p-in_progress i{{background:var(--prog)}}
.p-done i{{background:var(--done)}} .p-done{{opacity:.6;text-decoration:line-through}}
.p-not_started{{border-style:dashed}}
/* chain */
.chain{{display:flex;align-items:stretch;overflow-x:auto;padding:14px 2px 6px}}
.node{{position:relative;flex:1 0 132px;max-width:220px;background:var(--card2);border:1px solid var(--line);border-radius:10px;padding:10px 12px}}
.node b{{display:block;font-size:14px}} .node span{{display:block;font-size:12.5px;color:var(--mut);margin-top:2px}}
.n-done{{border-color:color-mix(in srgb,var(--done) 50%,var(--line))}} .n-stuck{{border-color:var(--stuck);background:color-mix(in srgb,var(--stuck) 9%,var(--card2))}}
.n-in_progress{{border-color:color-mix(in srgb,var(--prog) 55%,var(--line))}} .n-not_started{{border-style:dashed}}
.is-bn{{border:2px solid var(--accent);box-shadow:0 0 0 4px color-mix(in srgb,var(--accent) 14%,transparent)}}
.node .bn{{position:absolute;top:-10px;left:10px;background:var(--accent);color:#06221e;font-size:11px;font-weight:700;padding:1px 7px;border-radius:5px;margin:0}}
.arrow{{flex:0 0 30px;display:flex;align-items:center;position:relative}}
.arrow span{{flex:1;height:2px;background:var(--mut)}} .arrow::after{{content:"";position:absolute;right:2px;border:5px solid transparent;border-left:7px solid var(--mut)}}
.a-inferred span{{background:repeating-linear-gradient(90deg,var(--mut) 0 4px,transparent 4px 8px)}}
.a-none span{{background:none}} .a-none::after{{display:none}}
.legend{{display:flex;flex-wrap:wrap;gap:16px;font-size:12px;color:var(--mut);margin-top:10px}}
.legend i{{display:inline-block;width:22px;height:2px;vertical-align:middle;margin-right:6px;background:var(--mut)}}
.legend .lg-i{{background:repeating-linear-gradient(90deg,var(--mut) 0 4px,transparent 4px 8px)}}
.legend .lg-b{{width:12px;height:12px;border:2px solid var(--accent);background:none;border-radius:3px}}
.why{{margin-top:8px;font-size:13px;color:var(--mut)}} .why summary{{cursor:pointer}} .why ul{{margin:8px 0 0;padding-left:18px}}
.why li{{margin:4px 0}} .why b{{color:var(--fg);font-weight:600}} .w-inferred b::before{{content:"⋯ "}}
/* grid */
.grid{{display:grid;grid-template-columns:minmax(0,1fr) 340px;gap:16px;align-items:start}}
.sh{{display:flex;justify-content:space-between;align-items:baseline}} .sh span{{color:var(--mut);font-size:13px}} .sh b{{color:var(--done)}}
.parts{{display:grid;grid-template-columns:repeat(auto-fill,minmax(230px,1fr));gap:12px}}
.part{{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:14px 16px;display:flex;flex-direction:column;gap:8px;min-height:150px}}
.part h3{{font-size:16px}} .part p{{margin:0;color:var(--mut);font-size:13px}}
.part footer{{margin-top:auto;padding-top:8px;border-top:1px solid var(--line);font-size:12px;color:var(--dim);word-break:break-word}}
.pt-stuck{{border-color:color-mix(in srgb,var(--stuck) 60%,var(--line));background:repeating-linear-gradient(135deg,color-mix(in srgb,var(--stuck) 7%,var(--card)) 0 10px,var(--card) 10px 20px)}}
.pt-not_started{{border-style:dashed;background:transparent}}
.pt-done h3{{color:var(--mut)}}
.changed{{outline:2px solid var(--accent);outline-offset:2px}}
.ph{{display:flex;flex-wrap:wrap;gap:6px;align-items:center}}
.badge{{font-size:12px;font-weight:600;padding:1px 8px;border-radius:6px;background:var(--card2);color:var(--mut)}}
.s-done{{color:var(--done);background:color-mix(in srgb,var(--done) 14%,transparent)}}
.s-in_progress{{color:var(--prog);background:color-mix(in srgb,var(--prog) 15%,transparent)}}
.s-stuck{{color:var(--stuck);background:color-mix(in srgb,var(--stuck) 15%,transparent)}}
.chg{{font-size:11.5px;color:var(--accent);font-weight:600}}
.wait{{color:var(--stuck)!important}}
.prog .pl{{display:flex;justify-content:space-between;font-size:12px;color:var(--mut)}}
.bar{{height:6px;background:var(--card2);border-radius:3px;overflow:hidden;margin-top:3px}} .bar i{{display:block;height:100%;background:var(--prog);border-radius:3px}}
.pt-done .bar i{{background:var(--done)}}
aside .card{{padding:16px 18px}}
.next{{border-color:color-mix(in srgb,var(--accent) 55%,var(--line))}} .next .nt{{font-size:18px;font-weight:650;margin:0 0 6px;color:var(--accent)}}
.next p{{margin:0;color:var(--mut);font-size:13px}}
.decide{{border-color:color-mix(in srgb,var(--stuck) 35%,var(--line))}} .decide h2{{color:var(--stuck)}}
.decide ul,.changes ul{{list-style:none;margin:0;padding:0}} .decide li{{padding:8px 0;border-top:1px solid var(--line)}} .decide li:first-child{{border:0;padding-top:0}}
.decide .q{{margin:0;font-weight:600}} .decide .d{{margin:3px 0 0;font-size:13px;color:var(--mut)}} .decide .d b{{color:var(--accent);font-weight:500}}
.changes li{{display:flex;gap:8px;align-items:baseline;font-size:13px;padding:3px 0}} .changes li>i{{flex:0 0 8px;height:8px;border-radius:50%;background:var(--accent);transform:translateY(-1px)}}
.dot-done{{background:var(--done)!important}} .dot-stuck{{background:var(--stuck)!important}} .dot-in_progress{{background:var(--prog)!important}} .dot-not_started{{background:var(--todo)!important}}
.changes h3{{font-size:12px;color:var(--mut);font-weight:600;margin:12px 0 4px}} .since{{float:right;text-transform:none;letter-spacing:0;color:var(--dim)}}
.commits li{{display:block;color:var(--mut)}} .commits .t{{color:var(--dim);font-size:12px}}
.muted{{color:var(--dim)}} .plain{{margin:0;padding-left:18px;color:var(--mut);font-size:13px}} .plain li{{margin:4px 0}}
.details{{margin-top:8px}} .details details{{background:var(--card);border:1px solid var(--line);border-radius:10px;margin-bottom:8px}}
.details summary{{cursor:pointer;padding:12px 16px;display:flex;justify-content:space-between;gap:12px}} .details summary span{{color:var(--dim);font-size:12px}}
.details details>*:not(summary){{margin:0 16px 14px}}
.tw{{overflow-x:auto}} table{{border-collapse:collapse;width:100%;font-size:13px}} th,td{{text-align:left;padding:6px 8px;border-bottom:1px solid var(--line);vertical-align:top}}
th{{color:var(--mut);font-weight:600}}
.foot{{margin-top:20px;font-size:12px;color:var(--dim)}} .foot p{{margin:4px 0}}
@media (max-width:900px){{.grid{{grid-template-columns:1fr}}}}
@media (max-width:640px){{h1{{font-size:23px}} .card{{padding:14px}}
 .chain{{flex-direction:column;overflow:visible}} .node{{max-width:none;flex:none}}
 .arrow{{flex:0 0 26px;justify-content:center}} .arrow span{{flex:0 0 2px;height:100%;width:2px}}
 .a-inferred span{{background:repeating-linear-gradient(180deg,var(--mut) 0 4px,transparent 4px 8px)}}
 .arrow::after{{right:auto;bottom:0;border:5px solid transparent;border-top:7px solid var(--mut)}}
 .ms{{flex-basis:112px}}}}
</style>
</head>
<body><div class="wrap">{body}</div>
<script>var h=document.getElementById("here");if(h&&h.parentElement.scrollWidth>h.parentElement.clientWidth)h.parentElement.scrollLeft=h.offsetLeft-h.parentElement.clientWidth/2+60;</script>
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
    state = load_json(os.path.join(d, "state.json"), {}) or {}
    prev = load_json(state["last_map"]) if state.get("last_map") else None
    marks, lines = diff_maps(prev, m)
    prev_time = (prev.get("generated_at") or state.get("last_render")) if prev else None
    page = render_html(m, facts, marks, lines, style, prev_time)
    write_text(os.path.join(d, "index.html"), page)
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    snap = os.path.join(d, "history", f"{stamp}.json")
    dump_json(snap, m)
    pend = os.path.join(d, "pending_baseline.json")
    if os.path.exists(pend):
        os.replace(pend, os.path.join(d, "baseline.json"))
    state.update({"last_map": snap, "last_render": now_iso(), "repo": repo})
    dump_json(os.path.join(d, "state.json"), state)
    print(f"changes since last render: {len(lines)}" if prev else "first render (no previous map)")
    for _, t in lines:
        print(f"  - {t}")
    print(f"html: {os.path.join(d, 'index.html')}")


def cmd_status(a):
    """Cheap staleness check (git only, no network, no model): none | fresh | stale."""
    repo = repo_root(a.repo)
    d = os.path.abspath(a.out) if a.out else os.path.join(repo, ".project-map")
    state = load_json(os.path.join(d, "state.json"), {}) or {}
    since = state.get("last_render")
    res = {"repo": repo, "map": os.path.join(d, "index.html"), "last_render": since}
    if not since or not os.path.exists(os.path.join(d, "map.json")):
        res.update(status="none", reason="no map yet")
    else:
        baseline = load_json(os.path.join(d, "baseline.json"), {}) or {}
        rng = new_commit_range(repo, baseline, state, [])
        ok, out = git(repo, "rev-list", "--count", *(rng or ["HEAD", "--not", "HEAD"])) if rng else (True, "0")
        if rng and rng[0] == "-n":  # no baseline at all: nothing to compare against
            ok, out = True, "0"
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
        age_h = (dt.datetime.now().astimezone() - dt.datetime.fromisoformat(since)).total_seconds() / 3600
        stale = commits >= a.commits or bool(new_tags) or (commits > 0 and age_h >= a.hours)
        res.update(status="stale" if stale else "fresh", commits_since=commits, new_tags=new_tags,
                   age_hours=round(age_h, 1))
        if stale:
            why = [f"{commits} new commits"] if commits else []
            why += [f"new tag {', '.join(new_tags)}"] if new_tags else []
            res["reason"] = "; ".join(why)
    if a.json:
        print(json.dumps(res, ensure_ascii=False))
    else:
        extra = f" ({res['reason']})" if res.get("reason") else ""
        print(f"{res['status']}{extra}" + (f" · last update {fmt_time(since)}" if since else ""))


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


def main():
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
    p.add_argument("--commits", type=int, default=5, help="stale after this many new commits (default 5)")
    p.add_argument("--hours", type=float, default=12, help="stale after this many hours with any new commit")
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_status)
    p = sp.add_parser("style")
    p.add_argument("--theme", choices=("dark", "light", "auto"))
    p.add_argument("--accent")
    p.add_argument("--lang", choices=("zh", "en"))
    p.set_defaults(fn=cmd_style)
    a = ap.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()
