#!/usr/bin/env python3
"""you-are-here plugin hook, shared by Claude Code, Codex and Grok.

    yah_hook.py <SessionStart|UserPromptSubmit|PostToolUse|Stop>   (hook payload on stdin)

Only acts in repos that already have a map (<repo>/.project-map/map.json), except that it suggests
drawing one when the user starts a long unattended run. Never blocks, never fails the host:
any error ends quietly with exit 0.

- SessionStart      hand the agent a short summary of the map and whether it is stale
- UserPromptSubmit  "where are we?" -> answer from the map; "run overnight" -> draw/refresh first
- PostToolUse       after commit / tag / merge / release: refresh when stale (background if auto)
- Stop              end of a turn: refresh in the background when auto is on and the map is stale

Background refreshes run the same agent CLI headless (claude -p / codex exec / grok -p), detached,
one at a time per repo, at most once per `every_minutes`, and only after `pmap.py auto on`.
"""
import json
import os
import re
import shutil
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
SKILL_DIR = os.path.join(os.path.dirname(HERE), "skills", "you-are-here")
sys.path.insert(0, os.path.join(SKILL_DIR, "scripts"))

WHERE = re.compile(r"where are we\b|what'?s (left|done)\??\s*$|how far (along )?are we|project (map|status)\b|"
                   r"status of the project|进度(到哪|怎么样|如何|怎样)|到哪(了|儿了|一步)|还剩(什么|多少|哪些)(没做|要做)?[？?]?\s*$|"
                   r"项目地图|画.{0,4}地图|做到哪", re.I)
LONG = re.compile(r"overnight|while i('| a)m (asleep|sleeping|away|out)|keep going until|for (the next )?(few |several )?hours|"
                  r"autonomous(ly)?|unattended|/goal\b|通宵|睡觉|一晚上|一整晚|长时间|自己(跑|干|做)|不用问我|我不在", re.I)
MILESTONE = re.compile(r"\bgit\s+(commit|tag|merge|push)\b|\bgh\s+(pr\s+merge|release\s+create)\b")


def payload():
    try:
        raw = sys.stdin.buffer.read() if hasattr(sys.stdin, "buffer") else sys.stdin.read().encode()
        data = json.loads(raw.decode("utf-8", "replace") or "{}")  # hosts send UTF-8, whatever the locale
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def pick(d, *keys):
    for k in keys:
        if d.get(k) not in (None, ""):
            return d[k]
    return None


def host(data=None):
    """Which agent runs this hook. Hosts don't always pass their env vars to hooks, so the payload wins:
    the transcript lives under ~/.codex, ~/.grok or ~/.claude, and Grok sends camelCase keys."""
    data = data or {}
    tp = str(pick(data, "transcript_path", "transcriptPath") or "")
    for name in ("codex", "grok", "claude"):
        if f"/.{name}/" in tp:
            return name
    if "hookEventName" in data or "sessionId" in data:
        return "grok"
    env = os.environ
    if any(k in env for k in ("CODEX_THREAD_ID", "CODEX_VERSION", "CODEX_PERMISSION_PROFILE")):
        return "codex"
    if "GROK_PLUGIN_ROOT" in env:
        return "grok"
    return "claude"


DEBUG_LOG = os.environ.get("YAH_DEBUG_LOG") or (os.path.expanduser("~/.cache/you-are-here/hook.log")
                                                if os.environ.get("YAH_DEBUG") == "1" else None)


def debug(*parts):
    if not DEBUG_LOG:
        return
    try:
        os.makedirs(os.path.dirname(DEBUG_LOG), exist_ok=True)
        with open(DEBUG_LOG, "a", encoding="utf-8") as f:
            f.write(time.strftime("%H:%M:%S ") + " ".join(str(x) for x in parts) + "\n")
    except OSError:
        pass


def emit(event, text):
    if text:
        out = json.dumps({"hookSpecificOutput": {"hookEventName": event, "additionalContext": text}})
        debug("out", event, out[:600])
        print(out)


def find_repo(cwd):
    import pmap
    ok, top = pmap.git(cwd or ".", "rev-parse", "--show-toplevel")
    return top if ok and top else None


def recent(path, minutes):
    try:
        return time.time() - os.path.getmtime(path) < minutes * 60
    except OSError:
        return False


def touch(path):
    try:
        with open(path, "a"):
            pass
        os.utime(path, None)
    except OSError:
        pass


def bg_running(d):
    info = {}
    try:
        with open(os.path.join(d, ".bg.json")) as f:
            info = json.load(f)
        os.kill(int(info["pid"]), 0)
        return True
    except (OSError, ValueError, KeyError, TypeError):
        return False


def refresh_command(agent, repo, d):
    prompt = (f"Use the you-are-here skill (its SKILL.md is at {SKILL_DIR}/SKILL.md) to update the project map for "
              f"{repo}: run its steps with --out {d}. This is an unattended background refresh: never ask questions, "
              f"do not run `pmap.py style` with flags, treat the repo as read-only and write only inside {d}. "
              f"If a hook or permission policy denies a command itself, stop and say so; warnings printed while the "
              f"shell starts (profile scripts, version managers) are not denials when the command's own output is fine.")
    pm = os.path.join(SKILL_DIR, "scripts", "pmap.py")
    if agent == "codex":
        exe = shutil.which("codex")
        return exe and [exe, "exec", "--skip-git-repo-check", "--ephemeral", "-s", "workspace-write", "-C", d,
                        "-c", "sandbox_workspace_write.network_access=true", prompt]
    if agent == "grok":
        exe = shutil.which("grok")
        return exe and [exe, "-p", prompt, "--cwd", repo]
    exe = shutil.which("claude")
    allowed = [f"Bash(python3 {pm}:*)", "Bash(git log:*)", "Bash(git show:*)", "Bash(git diff:*)",
               "Bash(gh run view:*)", "Bash(gh pr view:*)", "Bash(gh issue view:*)", "Read", "Grep", "Glob",
               f"Write({d}/**)", f"Edit({d}/**)"]
    return exe and [exe, "-p", prompt, "--allowedTools", *allowed]


def start_refresh(repo, d, cfg, why, data=None):
    """Detached headless refresh. Returns a short note for the agent, or None when skipped."""
    if bg_running(d):
        return None
    if recent(os.path.join(d, ".bg.json"), cfg.get("every_minutes", 20)):
        return None
    agent = cfg.get("agent") if cfg.get("agent") in ("claude", "codex", "grok") else host(data)
    cmd = refresh_command(agent, repo, d)
    if not cmd:
        return f"you-are-here: could not find the `{agent}` CLI for a background map refresh."
    log = open(os.path.join(d, "bg.log"), "a")
    log.write(f"\n--- {time.strftime('%Y-%m-%d %H:%M:%S')} {agent} refresh ({why})\n")
    log.flush()
    p = subprocess.Popen(cmd, cwd=repo if agent != "codex" else d, stdin=subprocess.DEVNULL, stdout=log,
                         stderr=log, start_new_session=True, env={**os.environ, "YAH_BG": "1"})
    with open(os.path.join(d, ".bg.json"), "w") as f:
        json.dump({"pid": p.pid, "agent": agent, "started": time.time(), "why": why}, f)
    return f"you-are-here: map is stale ({why}); a background refresh started (log: {d}/bg.log)."


def main():
    if os.environ.get("YAH_BG") == "1":  # inside a background refresh: stay out of the way
        return
    event = sys.argv[1] if len(sys.argv) > 1 else ""
    data = payload()
    debug("in", event, host(data), json.dumps(data)[:800])
    import pmap
    repo = find_repo(pick(data, "cwd") or os.getcwd())
    if not repo:
        return
    d = os.path.join(repo, ".project-map")
    has_map = os.path.exists(os.path.join(d, "map.json"))
    cfg = pmap.load_config(d) if os.path.isdir(d) else dict(pmap.AUTO_DEFAULTS)
    pm = f"python3 {os.path.join(SKILL_DIR, 'scripts', 'pmap.py')}"

    if event == "SessionStart":
        if has_map:
            emit(event, pmap.summary_text(repo, d))
        return

    if event == "UserPromptSubmit":
        prompt = str(pick(data, "prompt", "user_prompt", "userPrompt") or "")
        if WHERE.search(prompt):
            if has_map:
                st = pmap.staleness(repo, d)
                emit(event, f"you-are-here: the project map is {st['status']}"
                            f"{' (' + st['reason'] + ')' if st.get('reason') else ''}. "
                            + ("Answer from " + os.path.join(d, "map.json") + "." if st["status"] == "fresh" else
                               "Update it with the you-are-here skill (incremental), then answer from it."))
            else:
                emit(event, "you-are-here: no project map yet for this repo. Draw one with the you-are-here skill, "
                            "then answer from it.")
        elif LONG.search(prompt):
            emit(event, "you-are-here: before this long unattended run, draw or refresh the project map with the "
                        "you-are-here skill (start the you-are-here subagent in the background if you have one), "
                        f"and turn on automatic refreshes with `{pm} auto on {repo}`. Put choices that need the user "
                        "into the map's decisions.")
        return

    if not has_map:
        return

    if event == "PostToolUse":
        ti = pick(data, "tool_input", "toolInput") or {}
        cmd = ti.get("command") if isinstance(ti, dict) else ti
        if isinstance(cmd, list):
            cmd = " ".join(map(str, cmd))
        if not isinstance(cmd, str) or not MILESTONE.search(cmd):
            return
        st = pmap.staleness(repo, d)
        if st["status"] != "stale":
            return
        if cfg.get("auto"):
            emit(event, start_refresh(repo, d, cfg, st.get("reason") or "stale", data))
        elif not recent(os.path.join(d, ".nudge"), 30):
            touch(os.path.join(d, ".nudge"))
            emit(event, f"you-are-here: the project map is stale ({st.get('reason')}). At a stopping point, refresh it "
                        "with the you-are-here skill (in the background if you can).")
        return

    if event == "Stop" and cfg.get("auto"):
        st = pmap.staleness(repo, d)
        if st["status"] == "stale":
            start_refresh(repo, d, cfg, st.get("reason") or "stale", data)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:  # a map helper must never break the host session
        debug("error", repr(e))
    sys.exit(0)
