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
from html.parser import HTMLParser

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


class MapMeaning(unittest.TestCase):
    def test_remaining_ids_are_required_and_unique(self):
        m = copy.deepcopy(MAP)
        m["remaining"][0].pop("id")
        self.assertTrue(any("remaining[0].id" in e for e in pmap.validate(m)))
        m = copy.deepcopy(MAP)
        m["remaining"][1]["id"] = m["remaining"][0]["id"]
        self.assertTrue(any("duplicate" in e for e in pmap.validate(m)))

    def test_remaining_rename_remove_and_explicit_completion(self):
        old = copy.deepcopy(MAP)
        renamed = copy.deepcopy(old)
        renamed["remaining"][0]["label"] = "finish parser"
        _, changes = pmap.diff_maps(old, renamed)
        self.assertFalse(any(kind in ("done", "new") for kind, _ in changes))
        removed = copy.deepcopy(old)
        removed["remaining"].pop(0)
        _, changes = pmap.diff_maps(old, removed)
        self.assertIn(("not_started", "剩余项: parser: 已移除"), changes)
        completed = copy.deepcopy(old)
        completed["remaining"][0]["status"] = "done"
        _, changes = pmap.diff_maps(old, completed)
        self.assertIn(("done", "剩余项: parser: 进行中 → 完成"), changes)

    def test_progress_scope_change_is_not_completion(self):
        old = copy.deepcopy(MAP)
        old["parts"][0].update(status="done", progress={"done": 46, "total": 46, "label": "tests"})
        new = copy.deepcopy(old)
        new["parts"][0]["progress"] = {"done": 6, "total": 6, "label": "CI environments"}
        _, changes = pmap.diff_maps(old, new)
        self.assertEqual(len(changes), 1)
        self.assertNotEqual(changes[0][0], "done")
        self.assertIn("口径变化", changes[0][1])
        self.assertIn("46/46 tests", changes[0][1])
        self.assertIn("6/6 CI environments", changes[0][1])

    def test_brief_leads_the_page(self):
        m = copy.deepcopy(MAP)
        m["headline"] = "Parser landed; docs untouched."
        m["remaining"] = [{"id": f"r{i}", "label": f"item {i}", "status": "in_progress"} for i in range(6)]
        m["remaining"].append({"id": "blocked", "label": "blocked item", "status": "stuck", "note": "waits on the API"})
        page = pmap.render_html(m, {}, {}, [], {"theme": "dark", "accent": "#2dd4bf"}, None)
        brief = page.index('class="panel brief"')
        self.assertLess(brief, page.index('class="route track"'))  # headline, next step and decisions come first
        self.assertIn(m["headline"], page[brief:page.index('class="route track"')])
        self.assertNotIn("headline-note", page)
        self.assertIn('data-question="ship?"', page)
        self.assertIn('id="toast"', page)
        self.assertIn('<span class="rn">waits on the API</span>', page)  # stuck items say what they wait on
        self.assertEqual(page.count('class="ri r-in_progress'), 6)
        self.assertIn("展开其余 2 项", page)  # four moving items shown, the rest folded

    def test_goal_can_be_undecided_and_done_items_are_not_remaining(self):
        m = copy.deepcopy(MAP)
        m["milestones"] = m["milestones"][:1]
        m["remaining"] = []
        m["chain"] = {"nodes": [], "edges": []}
        self.assertEqual(pmap.validate(m), [])
        page = pmap.render_html(m, {}, {}, [], {"theme": "dark", "accent": "#2dd4bf"}, None)
        self.assertIn("下一目标未定", page)
        self.assertNotIn(' id=here', page)
        m = copy.deepcopy(MAP)
        m["remaining"][0]["status"] = "done"
        page = pmap.render_html(m, {}, {}, [], {"theme": "dark", "accent": "#2dd4bf"}, None)
        self.assertIn('<strong class="big">1</strong>', page)

    def test_replay_keeps_direction_only_changes(self):
        for field, value in (("next_step", {"title": "review parser", "owner": "user"}),
                             ("decisions", [{"question": "ship?", "default": "yes"}]),
                             ("chain", {"nodes": [{"id": "b", "label": "release", "status": "stuck", "bottleneck": True}]})):
            with self.subTest(field=field), tempfile.TemporaryDirectory() as history:
                old = copy.deepcopy(MAP)
                old["generated_at"] = "2026-10-12T10:00:00+08:00"
                put(os.path.join(history, "old.json"), json.dumps(old))
                new = copy.deepcopy(old)
                new[field] = value
                new["generated_at"] = "2026-10-13T10:00:00+08:00"
                frames = pmap.build_frames(history, new)
                self.assertEqual(len(frames), 2)
                self.assertEqual(frames[0]["next_step"], old["next_step"])
                self.assertEqual(frames[-1][field], value)
                put(os.path.join(history, "z-new.json"), json.dumps(new))
                repeated = pmap.build_frames(history, new)
                self.assertEqual(len(repeated), 2)
                self.assertEqual(repeated[-1]["changes"], frames[-1]["changes"])


class DependencyMap(unittest.TestCase):
    def page(self, m):
        return pmap.render_html(m, {}, {}, [], dict(pmap.DEFAULT_STYLE), None)

    def test_graph_visible_without_expanding_supporting_records(self):
        class Outline(HTMLParser):
            def __init__(self):
                super().__init__()
                self.closed_details = 0
                self.graphs = []
                self.details = []

            def handle_starttag(self, tag, attrs):
                attrs = dict(attrs)
                if tag == "details":
                    closed = "open" not in attrs
                    self.details.append(closed)
                    self.closed_details += closed
                if tag == "svg" and attrs.get("class") == "dependency-graph":
                    self.graphs.append(self.closed_details)

            def handle_endtag(self, tag):
                if tag == "details":
                    self.closed_details -= self.details.pop()

        outline = Outline()
        outline.feed(self.page(MAP))
        self.assertEqual(outline.graphs, [0])

    def test_parallel_branches_join_without_invented_sequence(self):
        m = copy.deepcopy(MAP)
        m["chain"] = {
            "nodes": [{"id": x, "label": x, "status": "in_progress"} for x in "abcd"],
            "edges": [{"from": a, "to": b, "kind": "evidence", "why": "acceptance plan"}
                      for a, b in [("a", "b"), ("a", "c"), ("b", "d"), ("c", "d")]],
        }
        page = self.page(m)
        for a, b in [("a", "b"), ("a", "c"), ("b", "d"), ("c", "d")]:
            self.assertIn(f'data-from="{a}" data-to="{b}"', page)
        self.assertNotIn('data-from="b" data-to="c"', page)
        self.assertEqual(page.count('class="graph-edge e-evidence"'), 4)

    def test_empty_dependencies_do_not_create_a_diagram(self):
        m = copy.deepcopy(MAP)
        m["chain"] = {"nodes": [], "edges": []}
        self.assertNotIn('<svg class="dependency-graph"', self.page(m))


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

    def test_single_edit_and_same_second_edit_make_map_stale(self):
        self.r.cycle()
        path = os.path.join(self.r.dir, "src/core.py")
        put(path, "x = 2\n")
        stamp = int(time.time()) * 10**9
        os.utime(path, ns=(stamp, stamp))
        self.assertEqual(pmap.staleness(self.r.dir, self.r.out)["status"], "stale")
        self.r.cycle()
        self.assertEqual(pmap.staleness(self.r.dir, self.r.out)["status"], "fresh")
        put(path, "x = 3\n")  # same size, same second, still new work
        os.utime(path, ns=(stamp + 1000000, stamp + 1000000))
        self.assertEqual(pmap.staleness(self.r.dir, self.r.out)["status"], "stale")

    def test_deleted_file_makes_clean_map_stale(self):
        self.r.cycle()
        os.remove(os.path.join(self.r.dir, "src/core.py"))
        self.assertEqual(pmap.staleness(self.r.dir, self.r.out)["status"], "stale")
        self.r.pmap("collect", "--no-gh")
        self.assertEqual(self.r.facts()["delta"]["touched_parts"], {"core": 1})

    def test_uncommitted_new_module_and_committed_paths_are_mapped_once(self):
        self.r.cycle()
        self.r.commit("misc.txt", "one\n", "new unmapped file")
        put(os.path.join(self.r.dir, "misc.txt"), "two\n")
        os.makedirs(os.path.join(self.r.dir, "new-module"))
        put(os.path.join(self.r.dir, "new-module", "new.py"), "new\n")
        self.r.pmap("collect", "--no-gh")
        delta = self.r.facts()["delta"]
        self.assertEqual(delta["unmapped_files"], ["misc.txt", "new-module/new.py"])
        self.assertIn("new-module/new.py", delta["worktree_changed"])

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

    def test_skipped_github_collection_preserves_last_success(self):
        self.r.cycle()
        path = os.path.join(self.r.out, "baseline.json")
        baseline = json.loads(read(path))
        baseline["github"] = {"prs": [{"number": 7, "title": "still open"}], "issues": []}
        put(path, json.dumps(baseline))
        self.r.pmap("collect", "--no-gh")
        self.assertNotIn("github", self.r.facts())
        pending = json.loads(read(os.path.join(self.r.out, "pending_baseline.json")))
        self.assertEqual(pending["github"], baseline["github"])
        self.assertEqual(pmap.github_delta(baseline["github"], {"prs": None, "issues": []}), "no changes")
        self.assertIn("unknown", pmap.github_delta(baseline["github"], {"prs": None}))
        self.assertIn("prs_gone", pmap.github_delta(baseline["github"], {"prs": []}))

    def test_rerender_preserves_last_change_and_next_update_advances_it(self):
        self.r.cycle()
        m = copy.deepcopy(MAP)
        m["parts"][0]["status"] = "done"
        m["parts"][0]["progress"]["done"] = 4
        self.r.write_map(m)
        self.r.pmap("render")
        for i in range(9):  # more than the replay window
            m["style"] = {"lang": "zh", "accent": "#ff8800"}
            m["generated_at"] = f"2026-10-24T10:00:{i:02d}+08:00"
            self.r.write_map(m)
            output = self.r.pmap("render").stdout
            self.assertIn("0/4 → 4/4", output)
            self.assertIn("0/4 → 4/4", read(os.path.join(self.r.out, "index.html")))
        frames = pmap.build_frames(os.path.join(self.r.out, "history"), m)
        self.assertEqual(len(frames), 2)
        self.assertTrue(frames[-1]["changes"])
        m["parts"][1]["status"] = "in_progress"
        self.r.write_map(m)
        output = self.r.pmap("render").stdout
        self.assertIn("Docs: 未开始 → 进行中", output)
        self.assertNotIn("0/4 → 4/4", output)

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
    def make_map(self, root, name, decisions):
        r = Repo(os.path.join(root, name))
        r.out = os.path.join(r.dir, ".project-map")
        r.commit("src/core.py", "x\n", "init")
        m = copy.deepcopy(MAP)
        m["project"]["name"] = name
        m["style"] = {"lang": "en"}
        m["decisions"] = decisions
        r.cycle(m)
        return r

    def test_saved_answers_update_counts_and_overview_order(self):
        with tempfile.TemporaryDirectory() as t:
            answered = self.make_map(t, "A-answered", [
                {"id": "ship", "question": "Ship?", "default": "no"},
                {"question": "Use compact layout?", "default": "no"},
            ])
            pending = self.make_map(t, "Z-pending", [
                {"id": "review", "question": "Review?", "default": "no"},
            ])
            original_map = read(os.path.join(answered.out, "map.json"))
            self.assertEqual(pmap.index_row(answered.dir)["waits"], 2)
            answered.pmap("answer", "--id", "ship", "--answer", "yes")
            self.assertEqual(pmap.index_row(answered.dir)["waits"], 1)
            answered.pmap("answer", "--question", "Use compact layout?", "--answer", "yes")
            # Answers take effect without requiring another render or changing map.json.
            self.assertEqual(pmap.index_row(answered.dir)["waits"], 0)
            answered.pmap("render")
            self.assertEqual(read(os.path.join(answered.out, "map.json")), original_map)
            self.assertEqual(read(os.path.join(answered.out, "index.html")).count('<li class="answered"'), 2)
            out = os.path.join(t, "all.html")
            res = sh(sys.executable, PMAP, "index", t, "--out", out, "--lang", "en", "--json",
                     env=answered.env).stdout
            rows = json.loads(res[:res.rindex("]") + 1])
            self.assertEqual({x["name"]: x["waits"] for x in rows}, {"A-answered": 0, "Z-pending": 1})
            self.assertEqual({x["status"] for x in rows}, {"fresh"})
            page = read(out)
            self.assertIn("2 projects · 1 waiting on you", page)
            self.assertLess(page.index("Z-pending"), page.index("A-answered"))
            self.assertEqual(page.count('<span class="c you">'), 1)
            self.assertIn('<span class="c you"><b>1</b>', page)
            self.assertEqual(pmap.index_row(pending.dir)["waits"], 1)

    def test_project_headline_excludes_answered_decisions(self):
        with tempfile.TemporaryDirectory() as t:
            r = self.make_map(t, "headline", [
                {"id": "manual", "question": "Manual?", "default": "no"},
                {"question": "Another choice?", "default": "no"},
                {"id": "auto", "question": "Compact layout?", "default": "no",
                 "deadline": "2099-01-01T00:00:00+00:00", "reversible": True},
            ])
            self.assertEqual(read(os.path.join(r.out, "index.html")).count('<li data-decision="'), 3)  # open decisions
            selections = [("--id", "manual"), ("--question", "Another choice?"), ("--id", "auto")]
            for count, selection in enumerate(selections, 1):
                r.pmap("answer", *selection, "--answer", "yes")
                r.pmap("render")
                page = read(os.path.join(r.out, "index.html"))
                self.assertEqual(page.count('<li class="answered"'), count)
                self.assertEqual(page.count('<li data-decision="'), 3 - count)

    def test_saved_answers_exclude_only_matched_deadline_defaults(self):
        with tempfile.TemporaryDirectory() as t:
            r = self.make_map(t, "mixed", [
                {"id": "manual", "question": "Manual?", "default": "no"},
                {"question": "Still waiting?", "default": "no"},
                {"id": "auto", "question": "Compact layout?", "default": "no",
                 "deadline": "2099-01-01T00:00:00+00:00", "reversible": True},
                {"question": "Light theme?", "default": "no",
                 "deadline": "2099-01-01T00:00:00+00:00", "reversible": True},
            ])
            before = pmap.index_row(r.dir)
            self.assertEqual((before["waits"], before["auto_defaults"]), (2, 2))
            self.assertIn('<span>2 expiring</span>', read(os.path.join(r.out, "index.html")))
            r.pmap("answer", "--id", "manual", "--answer", "yes")
            r.pmap("answer", "--id", "auto", "--answer", "yes")
            partial = pmap.index_row(r.dir)
            self.assertEqual((partial["waits"], partial["auto_defaults"]), (1, 1))
            r.pmap("render")
            page = read(os.path.join(r.out, "index.html"))
            self.assertIn('<span>1 expiring</span>', page)
            self.assertNotIn('<span>2 expiring</span>', page)
            r.pmap("answer", "--question", "Light theme?", "--answer", "yes")
            after = pmap.index_row(r.dir)
            self.assertEqual((after["waits"], after["auto_defaults"]), (1, 0))
            r.pmap("render")
            self.assertNotIn(' expiring</span>', read(os.path.join(r.out, "index.html")))

    def test_index_reuses_id_or_trimmed_question_answer_matching(self):
        with tempfile.TemporaryDirectory() as t:
            r = self.make_map(t, "matching", [{"id": "choice", "question": "Keep layout?", "default": "no"}])
            cases = [
                ([{"id": "choice", "question": "Earlier wording?", "answer": "yes", "at": "2020-01-01T00:00:00Z"}], 0),
                ([{"question": "  Keep layout?  ", "answer": "yes"}], 0),
                ([{"id": "old-id", "question": "Keep layout?", "answer": "yes"}], 0),
                ([{"id": "other", "question": "Unrelated?", "answer": "yes"}], 1),
                ([{"id": "choice", "answer": "old"}, {"id": "choice", "answer": "new"}], 0),
                ([None, 7, "junk", {}, {"id": "choice", "answer": ""}], 1),
                ([{"id": "choice", "answer": "yes"}, {"id": "choice", "answer": ""}], 0),
            ]
            for answers, expected in cases:
                with self.subTest(answers=answers):
                    put(os.path.join(r.out, "answers.json"), json.dumps(answers))
                    self.assertEqual(pmap.index_row(r.dir)["waits"], expected)
                    r.pmap("render")
                    page = read(os.path.join(r.out, "index.html"))
                    self.assertEqual(page.count('<li class="answered"'), 1 - expected)
                    self.assertEqual(page.count('<li data-decision="'), expected)
                    if len(answers) == 2 and answers[1].get("answer") == "new":
                        self.assertIn('<b class="def">new</b>', page)

    def test_positional_answer_id_still_requires_question_for_no_id_decision(self):
        with tempfile.TemporaryDirectory() as t:
            r = self.make_map(t, "positional", [{"question": "Keep layout?", "default": "no"}])
            entry = {"id": "d0", "answer": "yes"}
            put(os.path.join(r.out, "answers.json"), json.dumps([entry]))
            self.assertEqual(pmap.index_row(r.dir)["waits"], 1)
            entry["question"] = "Keep layout?"
            put(os.path.join(r.out, "answers.json"), json.dumps([entry]))
            self.assertEqual(pmap.index_row(r.dir)["waits"], 0)

    def test_index_keeps_pending_counts_for_unreadable_or_empty_answers(self):
        with tempfile.TemporaryDirectory() as t:
            r = self.make_map(t, "no-answers", [{"question": "Pending?", "default": "no"}])
            self.assertEqual(pmap.index_row(r.dir)["waits"], 1)  # no answers.json
            for contents in ("not JSON", "[]", "{}", "null", '"invalid entries"'):
                with self.subTest(contents=contents):
                    put(os.path.join(r.out, "answers.json"), contents)
                    self.assertEqual(pmap.index_row(r.dir)["waits"], 1)

    def test_answered_decisions_do_not_count_as_waiting_or_expiring(self):
        with tempfile.TemporaryDirectory() as tmp:
            r = Repo(tmp)
            r.out = os.path.join(r.dir, ".project-map")
            r.commit("src/core.py", "x\n", "init")
            m = copy.deepcopy(MAP)
            m["decisions"].append({"question": "merge?", "default": "wait"})
            r.cycle(m)
            before = pmap.index_row(r.dir)
            self.assertEqual((before["waits"], before["auto_defaults"]), (1, 1))
            put(os.path.join(r.out, "answers.json"), json.dumps([
                {"question": q["question"], "answer": "no", "at": pmap.now_iso()} for q in m["decisions"]]))
            after = pmap.index_row(r.dir)
            self.assertEqual((after["waits"], after["auto_defaults"]), (0, 0))

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
