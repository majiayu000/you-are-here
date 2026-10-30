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
        e.update(PATH=self.bin + os.pathsep + e["PATH"], FAKE_LOG=self.log,
                 YAH_NOTIFY_LOG=os.path.join(self.tmp.name, "notes"), **extra)
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
        self.assertRegex(text, r"Headline \(as of [^)]+\): Parser landed")
        self.assertIn("Next step", text)

    def test_where_are_we(self):
        self.assertIn("no project map yet", self.hook("UserPromptSubmit", {"prompt": "进度到哪了？"}))
        self.draw_map()
        self.assertIn("Answer from", self.hook("UserPromptSubmit", {"prompt": "Where are we?"}))
        put(os.path.join(self.r.dir, "README.md"), "one important change\n")
        self.assertIn("Update it", self.hook("UserPromptSubmit", {"prompt": "进度到哪了？"}))

    def test_where_pattern_is_specific(self):
        sys.path.insert(0, os.path.dirname(HOOK))
        import yah_hook
        for t in ("进度到哪了？", "现在做到哪了", "Where are we?", "项目进度怎么样", "画个项目地图", "what's left?"):
            self.assertTrue(yah_hook.WHERE.search(t), t)
        for t in ("三端验证进度是 1/3", "进度条颜色改一下", "把 progress bar 改成绿色", "这个函数还剩一个 bug"):
            self.assertFalse(yah_hook.WHERE.search(t), t)

    def test_long_run_and_milestone_patterns_are_specific(self):
        sys.path.insert(0, os.path.dirname(HOOK))
        import yah_hook
        for t in ("我去睡觉了你继续", "今晚你一直跑，不用问我", "keep going until all tests pass",
                  "work on this for the next 3 hours", "我不在的时候你自己跑下去", "/goal ship v2"):
            self.assertTrue(yah_hook.LONG.search(t), t)
        for t in ("这个接口长时间没响应", "让它自己跑测试", "it has been failing for hours",
                  "autonomous driving dataset", "睡觉前帮我看下", "我不在乎格式", "一晚上没睡好"):
            self.assertFalse(yah_hook.LONG.search(t), t)
        for c in ("git tag v1.2", "git merge feat/x", "gh pr merge 12", "gh release create v1"):
            self.assertTrue(yah_hook.MILESTONE.search(c), c)
        for c in ("git commit -m x", "git push", "git tag -l", "git merge --abort"):
            self.assertFalse(yah_hook.MILESTONE.search(c), c)

    def test_severely_stale_map_is_not_quoted_at_session_start(self):
        self.draw_map()
        for i in range(20):
            self.r.commit("src/core.py", f"{i}\n", f"c{i}")
        text = self.hook("SessionStart")
        self.assertIn("too far behind to quote", text)
        self.assertNotIn("Headline", text)
        self.assertNotIn("Decision:", text)

    def test_long_run_suggests_map_and_auto(self):
        text = self.hook("UserPromptSubmit", {"prompt": "keep working on this overnight, don't ask me"})
        self.assertIn("auto on", text)

    def test_milestone_nudge_when_stale_and_auto_off(self):
        self.draw_map()
        for i in range(5):
            self.r.commit("src/core.py", f"{i}\n", f"c{i}")
        self.assertIsNone(self.hook("PostToolUse", {"tool_input": {"command": "git commit -m c4"}}))  # not a milestone
        text = self.hook("PostToolUse", {"tool_name": "Bash", "tool_input": {"command": "git tag v0.2"}})
        self.assertIn("stale", text)
        self.assertIsNone(self.hook("PostToolUse", {"tool_input": {"command": "git merge feat"}}))  # debounced
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
        self.assertIn(f"Edit({self.out}/**)", args)  # Claude ignores Write(path) rules; Edit covers writes
        self.assertFalse([a for a in args if a.startswith("Write(")])
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

    def test_failed_refresh_notifies(self):
        self.draw_map()
        self.pmap("auto", "on", self.r.dir)
        for i in range(5):
            self.r.commit("src/core.py", f"{i}\n", f"c{i}")
        self.hook("Stop", CODEX_THREAD_ID="t1")
        notes = os.path.join(self.tmp.name, "notes")
        end = time.time() + 10
        while time.time() < end and not os.path.exists(notes):
            time.sleep(0.1)
        self.assertIn("did not update the map", read(notes))  # the fake CLI never renders

    def test_answer_reaches_page_facts_and_summary(self):
        m = dict(MAP, decisions=[{"id": "ship", "question": "ship?", "default": "no"}])
        sh(sys.executable, os.path.join(PLUGIN, "skills", "you-are-here", "scripts", "pmap.py"), "collect",
           self.r.dir, "--no-gh", env=self.r.env)
        put(os.path.join(self.out, "map.json"), json.dumps(m))
        self.pmap("render", self.r.dir)
        out = self.pmap("answer", self.r.dir, "--id", "ship", "--answer", "yes, ship it").stdout
        self.assertIn("ship? -> yes, ship it", out)
        self.assertIn("User answered (not yet on the map): ship? -> yes, ship it", self.hook("SessionStart"))
        self.assertIn("user answers to apply: ship? -> yes, ship it", self.pmap("collect", self.r.dir, "--no-gh").stdout)
        self.pmap("render", self.r.dir)
        page = read(os.path.join(self.out, "index.html"))
        self.assertIn('class="answered"', page)
        self.assertIn("yes, ship it", page)

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
            p = subprocess.run([sys.executable, HOOK, "PostToolUse"], input=stdin, capture_output=True, text=True, encoding="utf-8",
                               env=self.env(), cwd=self.r.dir)
            self.assertEqual((p.returncode, p.stdout), (0, ""))

    def bg_run(self, code):
        """Run the detached wrapper the way start_refresh does: agent output lands in bg.log."""
        with open(os.path.join(self.out, "bg.log"), "a") as log:
            subprocess.run([sys.executable, HOOK, "--bg-run", self.r.dir, self.out, "claude",
                            json.dumps([sys.executable, "-c", code])], stdout=log, stderr=log, env=self.env(), check=True)
        path = os.path.join(self.out, "refresh-result.json")
        return json.loads(read(path)) if os.path.exists(path) else None

    def notes(self):
        path = os.path.join(self.tmp.name, "notes")
        return read(path).splitlines() if os.path.exists(path) else []

    def test_background_failure_backs_off_then_pauses(self):
        from unittest.mock import patch
        sys.path.insert(0, os.path.dirname(HOOK))
        import yah_hook
        self.draw_map()
        self.pmap("auto", "on", self.r.dir)
        result = self.bg_run("raise SystemExit(1)")
        self.assertEqual((result["exit_code"], result["failures"], result["paused"]), (1, 1, False))
        self.assertTrue(result["retry_after"])
        self.assertEqual(len(self.notes()), 1)
        self.assertIn("background refresh failed", self.notes()[0])
        # inside the backoff window nothing starts, quietly
        with patch.object(yah_hook.subprocess, "Popen") as launch:
            self.assertIsNone(yah_hook.start_refresh(self.r.dir, self.out, {"auto": True, "every_minutes": 20}, "stale"))
        launch.assert_not_called()
        self.assertIn("failed 1x in a row", self.hook("SessionStart"))
        # the wait doubles; retries are not notified again until they pause
        result = self.bg_run("raise SystemExit(1)")
        self.assertEqual(result["failures"], 2)
        self.assertEqual(len(self.notes()), 1)
        for _ in range(3):
            result = self.bg_run("raise SystemExit(1)")
        self.assertEqual((result["failures"], result["paused"], result["retry_after"]), (5, True, None))
        self.assertEqual(len(self.notes()), 2)
        self.assertIn("paused", self.notes()[1])
        with patch.object(yah_hook.subprocess, "Popen") as launch:
            message = yah_hook.start_refresh(self.r.dir, self.out, {"auto": True, "every_minutes": 0}, "stale")
        launch.assert_not_called()
        self.assertIn("paused after 5 failures", message)
        self.assertIn("retries are paused", self.hook("SessionStart"))
        # an explicit enable allows a new attempt
        self.pmap("auto", "on", self.r.dir)
        self.assertFalse(os.path.exists(os.path.join(self.out, "refresh-result.json")))

    def test_login_failure_says_how_to_log_in(self):
        self.draw_map()
        result = self.bg_run("print('Failed to authenticate: OAuth session expired and could not be refreshed'); "
                             "raise SystemExit(1)")
        self.assertIn("claude auth login", result["error"])
        self.assertIn("claude auth login", self.notes()[0])

    def test_success_clears_the_failure_count(self):
        self.draw_map()
        self.bg_run("raise SystemExit(1)")
        pm = os.path.join(PLUGIN, "skills", "you-are-here", "scripts", "pmap.py")
        time.sleep(1.1)  # last_render has one-second resolution
        self.assertIsNone(self.bg_run(f"import subprocess, sys; subprocess.check_call([sys.executable, {pm!r}, "
                                      f"'render', {self.r.dir!r}])"))

    def test_zero_exit_without_map_update_is_failure(self):
        from unittest.mock import patch
        sys.path.insert(0, os.path.dirname(HOOK))
        import yah_hook
        self.draw_map()
        with patch("pmap.notify_after_refresh", return_value=None):
            yah_hook.bg_run(self.r.dir, self.out, "claude", json.dumps([sys.executable, "-c", "pass"]))
        result = json.loads(read(os.path.join(self.out, "refresh-result.json")))
        self.assertEqual(result["exit_code"], 0)
        self.assertIn("did not update the map", result["error"])

    def test_hooks_json_points_at_the_script(self):
        cfg = json.loads(read(os.path.join(PLUGIN, "hooks", "hooks.json")))["hooks"]
        self.assertEqual(set(cfg), {"SessionStart", "UserPromptSubmit", "PostToolUse", "Stop"})
        for ev, groups in cfg.items():
            cmd = groups[0]["hooks"][0]["command"]
            self.assertIn("${CLAUDE_PLUGIN_ROOT}/hooks/yah_hook.py", cmd)
            self.assertTrue(cmd.endswith(ev))


if __name__ == "__main__":
    unittest.main()
