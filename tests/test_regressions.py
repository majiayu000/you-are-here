"""Regression tests for review findings: python3 -m unittest discover -s tests"""
import concurrent.futures
import copy
import json
import os
import random
import subprocess
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SCRIPTS = os.path.join(ROOT, "plugins", "you-are-here", "skills", "you-are-here", "scripts")
PMAP = os.path.join(SCRIPTS, "pmap.py")
sys.path.insert(0, SCRIPTS)
sys.path.insert(0, HERE)
import pmap  # noqa: E402
from test_pmap import MAP, put, read, sh  # noqa: E402


class Repo:
    def __init__(self, tmp):
        self.dir = os.path.join(tmp, "repo")
        self.out = os.path.join(tmp, "out")
        self.env = {**os.environ, "XDG_CONFIG_HOME": os.path.join(tmp, "cfg"),
                    "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
                    "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com"}
        os.makedirs(self.dir)
        sh("git", "init", "-q", self.dir)

    def commit(self, path, text, msg, date=None):
        full = os.path.join(self.dir, path)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        put(full, text, "a")
        sh("git", "-C", self.dir, "add", "-A")
        env = dict(self.env)
        if date:
            env.update(GIT_AUTHOR_DATE=date, GIT_COMMITTER_DATE=date)
        sh("git", "-C", self.dir, "commit", "-qm", msg, env=env)

    def pmap(self, *args, check=True):
        return sh(sys.executable, PMAP, *args, self.dir, "--out", self.out, env=self.env, check=check)

    def facts(self):
        return json.loads(read(os.path.join(self.out, "facts.json")))

    def write_map(self, m):
        put(os.path.join(self.out, "map.json"), json.dumps(m))

    def cycle(self, m=MAP):
        self.pmap("collect", "--no-gh")
        self.write_map(m)
        self.pmap("render")


class Worktree(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.r = Repo(self.tmp.name)
        self.r.commit("src/core.py", "x = 1\n", "init")

    def tearDown(self):
        self.tmp.cleanup()

    def test_first_status_line_keeps_its_path(self):
        put(os.path.join(self.r.dir, "src", "core.py"), "y\n", "a")  # " M src/core.py" is the first line
        self.r.pmap("collect", "--no-gh")
        self.assertEqual(self.r.facts()["git"]["worktree"]["paths"], ["src/core.py"])

    def test_uncommitted_edit_reaches_delta(self):
        self.r.cycle()
        put(os.path.join(self.r.dir, "src", "core.py"), "y\n", "a")
        self.r.pmap("collect", "--no-gh")
        dl = self.r.facts()["delta"]
        self.assertEqual(dl["worktree_changed"], ["src/core.py"])
        self.assertEqual(dl["touched_parts"], {"core": 1})

    def test_unicode_and_space_paths(self):
        self.r.cycle()
        self.r.commit("src/另一个 文件.py", "w\n", "unicode commit")
        put(os.path.join(self.r.dir, "src", "中文 file.py"), "z\n")
        self.r.pmap("collect", "--no-gh")
        f = self.r.facts()
        self.assertIn("src/另一个 文件.py", f["git"]["changed_files"])
        self.assertIn("src/中文 file.py", f["delta"]["worktree_changed"])
        self.assertEqual(f["delta"]["touched_parts"], {"core": 2})
        self.assertEqual(f["delta"]["unmapped_files"], [])


class Baseline(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.r = Repo(self.tmp.name)
        self.r.commit("src/core.py", "x = 1\n", "init")

    def tearDown(self):
        self.tmp.cleanup()

    def test_commit_between_collect_and_render_is_not_lost(self):
        self.r.cycle()
        self.r.pmap("collect", "--no-gh")
        self.r.commit("src/core.py", "mid\n", "MIDDLE commit")  # lands while the agent writes the map
        self.r.write_map(MAP)
        self.r.pmap("render")
        self.assertIn("1 new commits", self.r.pmap("status", "--commits", "1").stdout)
        self.r.pmap("collect", "--no-gh")
        self.assertEqual([c["subject"] for c in self.r.facts()["git"]["commits"]], ["MIDDLE commit"])

    def test_old_dated_commit_is_seen(self):
        self.r.cycle()
        self.r.commit("src/core.py", "old\n", "pushed late", date="2020-01-01T00:00:00+00:00")
        self.r.pmap("collect", "--no-gh")
        self.assertEqual([c["subject"] for c in self.r.facts()["git"]["commits"]], ["pushed late"])

    def test_already_covered_commits_are_not_repeated(self):
        self.r.cycle()
        self.r.pmap("collect", "--no-gh")
        self.assertEqual(self.r.facts()["git"]["commits"], [])

    def test_lightweight_tag_on_old_commit(self):
        self.r.cycle()
        sh("git", "-C", self.r.dir, "tag", "v1.0")
        st = json.loads(self.r.pmap("status", "--json").stdout)
        self.assertEqual((st["status"], st["new_tags"]), ("stale", ["v1.0"]))
        self.r.pmap("collect", "--no-gh")
        self.assertEqual(self.r.facts()["delta"]["new_tags"], ["v1.0"])

    def test_delta_uses_rendered_snapshot_not_working_map(self):
        self.r.cycle()
        broken = copy.deepcopy(MAP)
        broken["parts"][0]["paths"] = ["nowhere"]
        broken["parts"][0]["status"] = "bogus"  # fails validation, never rendered
        self.r.write_map(broken)
        self.assertNotEqual(self.r.pmap("render", check=False).returncode, 0)
        put(os.path.join(self.r.dir, "src", "core.py"), "y\n", "a")
        self.r.pmap("collect", "--no-gh")
        self.assertEqual(self.r.facts()["delta"]["touched_parts"], {"core": 1})

    def test_empty_repo(self):
        with tempfile.TemporaryDirectory() as t:
            r = Repo(t)
            out = r.pmap("collect", "--no-gh").stdout
            self.assertIn("repository has no commits yet", out)
            g = r.facts()["git"]
            self.assertEqual((g["head"], g["commits"]), (None, []))


RICH = copy.deepcopy(MAP)  # every optional field the page uses, for the fuzz test
RICH["decisions"] = [{"id": "ship", "question": "ship?", "default": "no", "options": ["no", "yes"],
                      "deadline": "2026-10-08T10:00:00+08:00", "reversible": True, "proposed": True},
                     {"question": "merge?", "default": "wait"}]
RICH["chain"]["nodes"][0]["part"] = "core"
RICH["chain"]["nodes"].append({"id": "alt", "label": "hold", "status": "not_started",
                               "if": {"decision": "ship", "option": 1}, "part": "docs"})
RICH["remaining"][0]["part"] = "core"
RICH["panels"] = [{"title": "t", "kind": "table", "columns": ["a"], "rows": [["1"]]},
                  {"title": "l", "kind": "list", "items": ["x"]}, {"title": "x", "kind": "text", "text": "y"}]
RICH["uncertain"] = ["u"]
RICH["sources"] = ["git log"]


class Safety(unittest.TestCase):
    def test_chain_status_injection_rejected_and_escaped(self):
        m = copy.deepcopy(MAP)
        evil = 'x"><img src=x onerror=alert(1)>'
        m["chain"]["nodes"][0]["status"] = evil
        self.assertTrue(any("chain.nodes[0].status" in e for e in pmap.validate(m)))
        page = pmap.render_html(m, None, {}, [], dict(pmap.DEFAULT_STYLE), None)  # even if validation is bypassed
        self.assertNotIn("<img", page)

    def test_hostile_text_is_escaped(self):
        m = copy.deepcopy(MAP)
        m["project"]["name"] = "<script>alert(1)</script>"
        m["chain"]["edges"][0]["why"] = '"><svg onload=alert(1)>'
        page = pmap.render_html(m, None, {}, [], dict(pmap.DEFAULT_STYLE), None)
        self.assertNotIn("<script>alert", page)
        self.assertNotIn("<svg onload", page)

    def test_validate_pass_implies_render_ok(self):
        """Fuzz: break one field at a time; anything validate accepts must render."""
        rnd = random.Random(7)
        junk = [None, 1, "", "x", [], {}, [1], [None], {"a": 1}, True, -1, [[1]], ["", None]]
        paths = []

        def walk(o, p):
            paths.append(p)
            if isinstance(o, dict):
                for k, v in o.items():
                    walk(v, p + [k])
            elif isinstance(o, list):
                for i, v in enumerate(o):
                    walk(v, p + [i])
        walk(RICH, [])
        self.assertEqual(pmap.validate(RICH), [])
        for _ in range(1500):
            m = copy.deepcopy(RICH)
            p = rnd.choice(paths[1:])
            o = m
            for k in p[:-1]:
                o = o[k]
            o[p[-1]] = copy.deepcopy(rnd.choice(junk))
            if pmap.validate(m):
                continue
            try:
                frames = [pmap.frame_from(RICH), pmap.frame_from(m)]
                pmap.render_html(m, None, {}, [], dict(pmap.DEFAULT_STYLE), None, frames)
                pmap.diff_maps(RICH, m)
                pmap.diff_maps(m, RICH)
            except Exception as e:  # noqa: BLE001
                self.fail(f"validate passed but render crashed at {p}: {e!r}")

    def test_out_into_existing_dir_writes_no_gitignore(self):
        with tempfile.TemporaryDirectory() as t:
            r = Repo(t)
            r.commit("a.txt", "a\n", "init")
            sh(sys.executable, PMAP, "collect", r.dir, "--out", r.dir, "--no-gh", env=r.env)
            self.assertFalse(os.path.exists(os.path.join(r.dir, ".gitignore")))
            r.pmap("collect", "--no-gh")  # a fresh out dir is ours: it ignores itself
            self.assertEqual(read(os.path.join(r.out, ".gitignore")), "*\n")

    def test_concurrent_renders_do_not_corrupt(self):
        with tempfile.TemporaryDirectory() as t:
            r = Repo(t)
            r.commit("src/core.py", "x\n", "init")
            r.cycle()

            def go(_):
                p = subprocess.run([sys.executable, PMAP, "render", r.dir, "--out", r.out],
                                   capture_output=True, text=True, encoding="utf-8", env=r.env)
                return p.returncode, p.stderr
            with concurrent.futures.ThreadPoolExecutor(6) as ex:
                res = list(ex.map(go, range(12)))
            for code, err in res:
                self.assertNotIn("Traceback", err)
                if code:
                    self.assertIn("another run", err)
            ok = sum(1 for c, _ in res if c == 0)
            self.assertGreaterEqual(ok, 1)
            self.assertEqual(len(os.listdir(os.path.join(r.out, "history"))), ok + 1)
            json.loads(read(os.path.join(r.out, "state.json")))
            self.assertFalse([f for f in os.listdir(r.out) if f.startswith(".tmp-") or f == ".lock"])


class Notify(unittest.TestCase):
    def test_worse_or_new_decision_notifies(self):
        with tempfile.TemporaryDirectory() as t:
            r = Repo(t)
            r.commit("src/core.py", "x\n", "init")
            r.cycle()
            before = json.loads(read(os.path.join(r.out, "state.json")))["last_render"]
            m = copy.deepcopy(MAP)
            m["parts"][0].update(status="stuck", waiting_on="a key")
            m["decisions"].append({"question": "new call?", "default": "wait"})
            r.write_map(m)
            time.sleep(1.1)
            r.pmap("render")
            log = os.path.join(t, "notes")
            os.environ["YAH_NOTIFY_LOG"] = log
            try:
                self.assertEqual(pmap.notify_after_refresh(r.dir, r.out, before), "1 变坏 · 1 等你")
            finally:
                del os.environ["YAH_NOTIFY_LOG"]
            self.assertIn("1 变坏 · 1 等你", read(log))


class Index(unittest.TestCase):
    def test_index_lists_repos_needing_you_first(self):
        with tempfile.TemporaryDirectory() as t:
            for name, waits in (("calm", 0), ("busy", 2)):
                r = Repo(os.path.join(t, name))
                r.commit("src/core.py", "x\n", "init")
                m = copy.deepcopy(MAP)
                m["project"]["name"] = name
                m["headline"] = f"{name} headline"
                m["decisions"] = [{"question": f"q{i}", "default": "no"} for i in range(waits)]
                os.makedirs(os.path.join(r.dir, ".project-map"))
                sh(sys.executable, PMAP, "collect", r.dir, "--no-gh", env=r.env)
                put(os.path.join(r.dir, ".project-map", "map.json"), json.dumps(m))
                sh(sys.executable, PMAP, "render", r.dir, env=r.env)
            out = os.path.join(t, "all.html")
            res = sh(sys.executable, PMAP, "index", t, "--out", out, "--json", env=r.env).stdout
            rows = json.loads(res[:res.rindex("]") + 1])
            self.assertEqual(sorted(x["name"] for x in rows), ["busy", "calm"])
            page = read(out)
            self.assertLess(page.index("busy headline"), page.index("calm headline"))  # needs-you first
            self.assertIn("2</b>", page)


class Remotes(unittest.TestCase):
    def test_github_remote_forms(self):
        cases = {
            "https://github.com/o/r.git": ("o/r", "https://github.com/o/r"),
            "git@github.com:o/r.git": ("o/r", "https://github.com/o/r"),
            "git@github-work:o/r.git": ("o/r", "https://github.com/o/r"),
            "ssh://git@github.com:22/o/r.git": ("o/r", "https://github.com/o/r"),
            "https://github.acme.io/team/r": ("team/r", "https://github.acme.io/team/r"),
        }
        for url, (slug, web) in cases.items():
            g = pmap.github_remote(url)
            self.assertEqual((g["slug"], g["web"]), (slug, web), url)
        self.assertEqual(pmap.github_remote("git@github.acme.io:team/r.git")["gh"], "github.acme.io/team/r")
        for url in ("https://gitlab.com/o/r.git", "", "not a url"):
            self.assertIsNone(pmap.github_remote(url), url)

    def test_paths_globs_and_dot_slash(self):
        m = {"parts": [{"id": "a", "paths": ["./src/a"]}, {"id": "b", "paths": ["src/**/*.ts"]}]}
        hits, unmapped = pmap.touched_parts(m, ["src/a/x.py", "src/b/c/y.ts", "README.md"])
        self.assertEqual((hits, unmapped), ({"a": 1, "b": 1}, ["README.md"]))

    def test_long_urls_become_short_links(self):
        r = pmap.R("https://github.com/o/app", set())
        out = r("see https://github.com/o/lib/pull/7 and https://github.com/o/app/blob/abc123/docs/x.md.")
        self.assertIn('>lib#7</a>', out)
        self.assertIn('>docs/x.md</a>.', out)
        self.assertIn('href="https://github.com/o/lib/pull/7"', out)
        self.assertEqual(pmap.short_url("https://github.com/o/app/pull/3", "o/app"), "#3")
        self.assertEqual(pmap.classify_evidence("run:42")[:2], ("run", "42"))
        self.assertNotIn("onmouseover=alert", r('x https://e.x/"onmouseover=alert(1)').split("</a>")[0])

    def test_glob_star_stays_in_one_directory(self):
        one = pmap.path_matcher(["src/services/*.ts"])
        self.assertTrue(one("src/services/a.ts"))
        self.assertFalse(one("src/services/ledger/b.ts"))
        self.assertFalse(pmap.path_matcher(["src"])("src-tauri/main.rs"))
        self.assertTrue(pmap.path_matcher(["src/**"])("src/x/y/z.ts"))

    def test_overlap_lint_understands_globs(self):
        base = {"chain": {}, "headline": "h", "next_step": {"title": "t", "owner": "agent"}}
        ok = dict(base, parts=[{"id": "a", "paths": ["src/services/*.ts"]}, {"id": "b", "paths": ["src/services/ledger"]}])
        bad = dict(base, parts=[{"id": "a", "paths": ["src/services/**"]}, {"id": "b", "paths": ["src/services/ledger"]}])
        self.assertFalse([w for w in pmap.lint(ok) if "overlap" in w])
        self.assertTrue([w for w in pmap.lint(bad) if "overlap" in w])

    def test_open_field_answers_full_progress(self):
        base = {"chain": {}, "headline": "h", "next_step": {"title": "t", "owner": "agent"}}
        p = {"id": "a", "name": "A", "status": "in_progress", "progress": {"done": 3, "total": 3}, "evidence": ["#1"]}
        self.assertTrue([w for w in pmap.lint(dict(base, parts=[p])) if "progress is 3/3" in w])
        self.assertFalse([w for w in pmap.lint(dict(base, parts=[dict(p, open="not merged")])) if "progress" in w])


@unittest.skipIf(os.name == "nt", "the installer links skills with symlinks")
class Installer(unittest.TestCase):
    def run_install(self, home, *args, src=ROOT):
        return subprocess.run([sys.executable, os.path.join(src, "install.py"), *args],
                              capture_output=True, text=True, encoding="utf-8", env={**os.environ, "HOME": home})

    def test_symlinked_source_is_idempotent_and_uninstalls(self):
        with tempfile.TemporaryDirectory() as t:
            home = os.path.join(t, "home")
            link = os.path.join(t, "src-link")
            os.makedirs(home)
            os.symlink(ROOT, link)
            self.assertEqual(self.run_install(home, "--all", src=link).returncode, 0)
            again = self.run_install(home, "--all", src=link).stdout
            self.assertNotIn("skip", again)
            self.run_install(home, "--uninstall", src=link)
            left = [os.path.join(dp, f) for dp, dn, fs in os.walk(home) for f in fs + dn if "you-are-here" in f]
            self.assertEqual(left, [])

    def test_codex_toml_parses_with_windows_path(self):
        try:
            import tomllib
        except ImportError:
            self.skipTest("tomllib needs Python 3.11+")
        text = read(os.path.join(ROOT, "plugins", "you-are-here", "codex", "you-are-here.toml")).replace("{{SKILL_DIR}}", r"C:\Users\me\skills\you-are-here")
        self.assertIn(r"C:\Users", tomllib.loads(text)["developer_instructions"])

    def test_broken_markers_stop_the_installer(self):
        with tempfile.TemporaryDirectory() as home:
            agents = os.path.join(home, ".codex", "AGENTS.md")
            os.makedirs(os.path.dirname(agents))
            for body in ("mine\n<!-- you-are-here:begin -->\nhalf\n",
                         "<!-- you-are-here:begin -->\na\n<!-- you-are-here:end -->\n" * 2):
                put(agents, body)
                for args in (("--rules",), ("--uninstall",)):
                    p = self.run_install(home, *args)
                    self.assertNotEqual(p.returncode, 0, (body, args))
                    self.assertIn("markers", p.stderr)
                    self.assertEqual(read(agents), body)


if __name__ == "__main__":
    unittest.main()
