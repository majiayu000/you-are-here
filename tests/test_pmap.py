"""Smoke tests: python3 -m unittest discover -s tests"""
import json
import os
import subprocess
import sys
import tempfile
import unittest

PMAP = os.path.join(os.path.dirname(__file__), "..", "scripts", "pmap.py")


def put(path, text, mode="w"):
    with open(path, mode) as f:
        f.write(text)


def read(path):
    with open(path) as f:
        return f.read()


def sh(*cmd, cwd=None, env=None, check=True):
    p = subprocess.run(cmd, cwd=cwd, env=env, capture_output=True, text=True)
    if check and p.returncode:
        raise AssertionError(f"{cmd} failed:\n{p.stdout}\n{p.stderr}")
    return p


MAP = {
    "project": {"name": "demo", "summary": "demo repo"},
    "milestones": [{"name": "v0.1", "status": "done", "date": "2026-10-01"},
                   {"name": "v0.2", "status": "current", "proposed": True},
                   {"name": "v1", "status": "future"}],
    "remaining": [{"label": "parser", "status": "in_progress"}, {"label": "docs", "status": "not_started"}],
    "chain": {"nodes": [{"id": "a", "label": "parser", "status": "in_progress", "bottleneck": True},
                        {"id": "b", "label": "release", "status": "not_started"}],
              "edges": [{"from": "a", "to": "b", "kind": "inferred"}]},
    "parts": [{"id": "core", "name": "Core <script>", "status": "in_progress", "summary": "x",
               "progress": {"done": 0, "total": 4, "label": "tasks"}, "evidence": ["`src/core.py`"],
               "paths": ["src"]},
              {"id": "docs", "name": "Docs", "status": "not_started", "inferred": True, "paths": ["docs"]}],
    "next_step": {"title": "finish parser", "why": "blocks release"},
    "decisions": [{"question": "ship?", "default": "no", "deadline": "10-08 10:00"}],
}


class PmapTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = os.path.join(self.tmp.name, "repo")
        self.out = os.path.join(self.tmp.name, "out")
        self.env = {**os.environ, "XDG_CONFIG_HOME": os.path.join(self.tmp.name, "cfg")}
        os.makedirs(os.path.join(self.repo, "src"))
        sh("git", "init", "-q", self.repo)
        for k, v in (("user.email", "t@example.com"), ("user.name", "t")):
            sh("git", "-C", self.repo, "config", k, v)
        put(os.path.join(self.repo, "README.md"), "# demo\n")
        put(os.path.join(self.repo, "src", "core.py"), "x = 1\n")
        sh("git", "-C", self.repo, "add", ".")
        sh("git", "-C", self.repo, "commit", "-qm", "init")

    def tearDown(self):
        self.tmp.cleanup()

    def pmap(self, *args, check=True):
        return sh(sys.executable, PMAP, *args, self.repo, "--out", self.out, env=self.env, check=check)

    def write_map(self, m):
        put(os.path.join(self.out, "map.json"), json.dumps(m))

    def test_full_cycle(self):
        r = self.pmap("collect", "--no-gh")
        self.assertIn("mode: initial", r.stdout)
        facts = json.loads(read(os.path.join(self.out, "facts.json")))
        self.assertEqual(facts["git"]["commits"][0]["subject"], "init")
        self.assertIn("README.md", facts["git"]["doc_hints"])
        self.assertFalse(os.path.exists(os.path.join(self.repo, ".project-map")))

        self.write_map(MAP)
        r = self.pmap("render")
        self.assertIn("first render", r.stdout)
        page = read(os.path.join(self.out, "index.html"))
        self.assertNotIn("<script>Core", page.replace("Core &lt;script&gt;", ""))  # escaped
        self.assertIn("Core &lt;script&gt;", page)
        self.assertIn('style="width:0%"', page)  # 0/4 draws an empty bar

        # second round: new commit in src/, part status changes
        put(os.path.join(self.repo, "src", "core.py"), "y = 2\n", "a")
        sh("git", "-C", self.repo, "commit", "-qam", "parser done")
        r = self.pmap("collect", "--no-gh")
        self.assertIn("mode: incremental", r.stdout)
        facts = json.loads(read(os.path.join(self.out, "facts.json")))
        self.assertEqual(facts["delta"]["touched_parts"], {"core": 1})
        self.assertIn("docs", facts["delta"]["untouched_parts"])

        m2 = json.loads(json.dumps(MAP))
        m2["parts"][0].update(status="done", progress={"done": 4, "total": 4, "label": "tasks"})
        self.write_map(m2)
        r = self.pmap("render")
        self.assertIn("changes since last render: 1", r.stdout)
        page = read(os.path.join(self.out, "index.html"))
        self.assertIn('class="part pt-done changed"', page)
        self.assertIn("0/4 → 4/4", page)

    def test_status(self):
        r = self.pmap("status")
        self.assertTrue(r.stdout.startswith("none"))
        self.pmap("collect", "--no-gh")
        self.write_map(MAP)
        self.pmap("render")
        self.assertTrue(self.pmap("status").stdout.startswith("fresh"))
        sh("git", "-C", self.repo, "commit", "-q", "--allow-empty", "-m", "c2",
           env={**self.env, "GIT_COMMITTER_DATE": "2099-01-01T00:00:00+00:00"})
        self.assertTrue(self.pmap("status").stdout.startswith("fresh"))  # 1 commit < 5, age < 12h
        sh("git", "-C", self.repo, "tag", "v9")
        r = self.pmap("status", "--json")
        st = json.loads(r.stdout)
        self.assertEqual(st["status"], "stale")
        self.assertEqual(st["new_tags"], ["v9"])

    def test_validation_errors(self):
        self.pmap("collect", "--no-gh")
        bad = json.loads(json.dumps(MAP))
        bad["parts"][0]["status"] = "stuck"           # stuck without waiting_on
        bad["parts"][1].pop("inferred")                # no evidence, not inferred
        bad["milestones"][2]["status"] = "current"     # two current
        bad["chain"]["edges"][0]["kind"] = "evidence"  # evidence without why
        self.write_map(bad)
        r = self.pmap("render", check=False)
        self.assertEqual(r.returncode, 1)
        for frag in ("waiting_on", "evidence[] or mark inferred", "exactly one", "edges[0].why"):
            self.assertIn(frag, r.stdout)

    def test_style_persists(self):
        sh(sys.executable, PMAP, "style", "--theme", "light", "--accent", "#ff8800", env=self.env)
        self.pmap("collect", "--no-gh")
        self.write_map(MAP)
        self.pmap("render")
        page = read(os.path.join(self.out, "index.html"))
        self.assertIn('data-theme="light"', page)
        self.assertIn("--accent:#ff8800", page)


if __name__ == "__main__":
    unittest.main()
