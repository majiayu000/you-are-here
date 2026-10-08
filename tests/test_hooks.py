"""Plugin hook tests with fake agent CLIs: python3 -m unittest discover -s tests"""
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
PLUGIN = os.path.join(ROOT, "plugins", "you-are-here")
HOOK = os.path.join(PLUGIN, "hooks", "yah_hook.py")
sys.path.insert(0, HERE)
from test_pmap import MAP, put, read, sh  # noqa: E402
from test_regressions import Repo  # noqa: E402

FAKE = """#!/bin/sh
# fake agent CLI: record how it was called, then exit
printf '%s\\n' "$0" "$@" > "$FAKE_LOG-tmp.$$" && mv "$FAKE_LOG-tmp.$$" "$FAKE_LOG.$$"
"""


@unittest.skipIf(os.name == "nt", "fake agent CLIs are POSIX shell scripts")
class Hooks(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.r = Repo(self.tmp.name)
        self.r.commit("src/core.py", "x\n", "init")
        self.bin = os.path.join(self.tmp.name, "bin")
        os.makedirs(self.bin)
        for name in ("claude", "codex", "grok"):
            put(os.path.join(self.bin, name), FAKE)
            os.chmod(os.path.join(self.bin, name), 0o755)
        self.log = os.path.join(self.tmp.name, "calls")
        self.out = os.path.realpath(os.path.join(self.r.dir, ".project-map"))  # hooks use the default output dir

    def tearDown(self):
        self.tmp.cleanup()

    def env(self, **extra):
        e = {k: v for k, v in self.r.env.items() if not k.startswith(("CODEX_", "GROK_", "YAH_"))}
        e.update(PATH=self.bin + os.pathsep + e["PATH"], FAKE_LOG=self.log, **extra)
        return e

    def hook(self, event, data=None, **env):
        payload = {"cwd": self.r.dir, "hook_event_name": event, **(data or {})}
        p = subprocess.run([sys.executable, HOOK, event], input=json.dumps(payload), capture_output=True,
                           text=True, env=self.env(**env))
        self.assertEqual(p.returncode, 0, p.stderr)
        return json.loads(p.stdout)["hookSpecificOutput"]["additionalContext"] if p.stdout.strip() else None

    def draw_map(self):
        sh(sys.executable, os.path.join(PLUGIN, "skills", "you-are-here", "scripts", "pmap.py"), "collect",
           self.r.dir, "--no-gh", env=self.r.env)
        m = dict(MAP, headline="Parser landed; docs untouched.")
        put(os.path.join(self.out, "map.json"), json.dumps(m))
        sh(sys.executable, os.path.join(PLUGIN, "skills", "you-are-here", "scripts", "pmap.py"), "render",
           self.r.dir, env=self.r.env)

    def pmap(self, *args):
        return sh(sys.executable, os.path.join(PLUGIN, "skills", "you-are-here", "scripts", "pmap.py"), *args,
                  env=self.r.env)

    def calls(self, wait=10.0):
        end = time.time() + wait
        while time.time() < end:
            files = [f for f in os.listdir(self.tmp.name) if f.startswith("calls.")]
            if files:
                return [read(os.path.join(self.tmp.name, f)).splitlines() for f in files]
            time.sleep(0.05)
        return []

    def test_quiet_without_a_map(self):
        for ev in ("SessionStart", "PostToolUse", "Stop"):
            self.assertIsNone(self.hook(ev, {"tool_input": {"command": "git commit -m x"}}))
        self.assertIsNone(self.hook("UserPromptSubmit", {"prompt": "fix the parser"}))

    def test_session_start_summary(self):
        self.draw_map()
        text = self.hook("SessionStart")
        self.assertIn("you-are-here: project map", text)
        self.assertIn("fresh", text)
        self.assertIn("Headline: Parser landed", text)
        self.assertIn("Next step", text)

    def test_where_are_we(self):
        self.assertIn("no project map yet", self.hook("UserPromptSubmit", {"prompt": "进度到哪了？"}))
        self.draw_map()
        self.assertIn("Answer from", self.hook("UserPromptSubmit", {"prompt": "Where are we?"}))

    def test_where_pattern_is_specific(self):
        sys.path.insert(0, os.path.dirname(HOOK))
        import yah_hook
        for t in ("进度到哪了？", "现在做到哪了", "Where are we?", "项目进度怎么样", "画个项目地图", "what's left?"):
            self.assertTrue(yah_hook.WHERE.search(t), t)
        for t in ("三端验证进度是 1/3", "进度条颜色改一下", "把 progress bar 改成绿色", "这个函数还剩一个 bug"):
            self.assertFalse(yah_hook.WHERE.search(t), t)

    def test_long_run_suggests_map_and_auto(self):
        text = self.hook("UserPromptSubmit", {"prompt": "keep working on this overnight, don't ask me"})
        self.assertIn("auto on", text)

    def test_milestone_nudge_when_stale_and_auto_off(self):
        self.draw_map()
        for i in range(5):
            self.r.commit("src/core.py", f"{i}\n", f"c{i}")
        text = self.hook("PostToolUse", {"tool_name": "Bash", "tool_input": {"command": "git commit -m c4"}})
        self.assertIn("stale", text)
        self.assertIsNone(self.hook("PostToolUse", {"tool_input": {"command": "git commit -m again"}}))  # debounced
        self.assertIsNone(self.hook("PostToolUse", {"tool_input": {"command": "ls"}}))
        self.assertEqual(self.calls(0.3), [])

    def test_auto_refresh_runs_same_cli_detached_once(self):
        self.draw_map()
        self.pmap("auto", "on", self.r.dir)
        for i in range(5):
            self.r.commit("src/core.py", f"{i}\n", f"c{i}")
        self.assertIsNone(self.hook("Stop", CODEX_THREAD_ID="t1"))
        calls = self.calls()
        self.assertEqual(len(calls), 1)
        self.assertEqual(os.path.basename(calls[0][0]), "codex")
        self.assertEqual(calls[0][1:3], ["exec", "--skip-git-repo-check"])
        self.assertIn(self.out, calls[0])
        self.assertTrue(any("SKILL.md" in a for a in calls[0]))
        # a second turn inside every_minutes does not start another one
        os.remove(os.path.join(self.tmp.name, [f for f in os.listdir(self.tmp.name) if f.startswith("calls.")][0]))
        self.hook("Stop", CODEX_THREAD_ID="t1")
        self.assertEqual(self.calls(0.5), [])
        self.assertIn("refresh", read(os.path.join(self.out, "bg.log")))

    def test_claude_refresh_limits_writes_to_the_map_dir(self):
        self.draw_map()
        self.pmap("auto", "on", self.r.dir, "--agent", "claude")
        for i in range(5):
            self.r.commit("src/core.py", f"{i}\n", f"c{i}")
        text = self.hook("PostToolUse", {"tool_input": {"command": "gh pr merge 3"}}, CODEX_THREAD_ID="t1")
        self.assertIn("background refresh started", text)
        args = self.calls()[0]
        self.assertEqual(os.path.basename(args[0]), "claude")
        self.assertIn(f"Write({self.out}/**)", args)
        self.assertFalse([a for a in args if a in ("Write", "Edit", "Bash")])

    def test_host_from_payload(self):
        sys.path.insert(0, os.path.dirname(HOOK))
        import yah_hook
        self.assertEqual(yah_hook.host({"transcript_path": "/u/.codex/sessions/x.jsonl"}), "codex")
        self.assertEqual(yah_hook.host({"transcript_path": "/u/.claude/projects/x.jsonl"}), "claude")
        self.assertEqual(yah_hook.host({"hookEventName": "stop", "transcriptPath": "/u/.grok/s/x"}), "grok")
        self.assertEqual(yah_hook.host({"hookEventName": "stop"}), "grok")

    def test_codex_payload_without_env_picks_codex(self):
        self.draw_map()
        self.pmap("auto", "on", self.r.dir)
        for i in range(5):
            self.r.commit("src/core.py", f"{i}\n", f"c{i}")
        self.hook("Stop", {"transcript_path": os.path.expanduser("~/.codex/sessions/2026/x.jsonl"), "turn_id": "t"})
        self.assertEqual(os.path.basename(self.calls()[0][0]), "codex")

    def test_background_run_does_not_recurse(self):
        self.draw_map()
        self.pmap("auto", "on", self.r.dir)
        for i in range(5):
            self.r.commit("src/core.py", f"{i}\n", f"c{i}")
        self.assertIsNone(self.hook("Stop", YAH_BG="1"))
        self.assertIsNone(self.hook("SessionStart", YAH_BG="1"))
        self.assertEqual(self.calls(0.3), [])

    def test_bad_input_never_fails_the_host(self):
        for stdin in ("", "not json", "[1,2]"):
            p = subprocess.run([sys.executable, HOOK, "PostToolUse"], input=stdin, capture_output=True, text=True,
                               env=self.env(), cwd=self.r.dir)
            self.assertEqual((p.returncode, p.stdout), (0, ""))

    def test_hooks_json_points_at_the_script(self):
        cfg = json.loads(read(os.path.join(PLUGIN, "hooks", "hooks.json")))["hooks"]
        self.assertEqual(set(cfg), {"SessionStart", "UserPromptSubmit", "PostToolUse", "Stop"})
        for ev, groups in cfg.items():
            cmd = groups[0]["hooks"][0]["command"]
            self.assertIn("${CLAUDE_PLUGIN_ROOT}/hooks/yah_hook.py", cmd)
            self.assertTrue(cmd.endswith(ev))


if __name__ == "__main__":
    unittest.main()
