"""GitHub collection failures must not look like empty successful snapshots."""
import contextlib
import copy
import io
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.join(os.path.dirname(HERE), "plugins", "you-are-here", "skills", "you-are-here", "scripts")
sys.path.insert(0, SCRIPTS)
sys.path.insert(0, HERE)
import pmap  # noqa: E402
from test_pmap import MAP, read, sh  # noqa: E402
from test_regressions import Repo  # noqa: E402


SNAPSHOT = {
    "prs": [{"number": 1, "title": "PR", "checks": {"pass": 1}}],
    "issues": [{"number": 2, "title": "Issue"}],
    "default_branch_runs": [{"id": 3, "workflow": "CI", "sha": "abc1234",
                             "conclusion": "success", "status": "completed"}],
}
RESPONSES = {
    "repo": {"defaultBranchRef": {"name": "main"}},
    "issue": [{"number": 2, "title": "Issue", "updatedAt": "2026-01-01T00:00:00Z", "labels": []}],
    "pr": [{"number": 1, "title": "PR", "updatedAt": "2026-01-01T00:00:00Z",
            "headRefName": "feature", "isDraft": False, "reviewDecision": "APPROVED",
            "mergeStateStatus": "CLEAN", "statusCheckRollup": [{"conclusion": "SUCCESS"}]}],
    "run": [{"databaseId": 3, "workflowName": "CI", "headSha": "abc123456789",
             "conclusion": "success", "status": "completed", "createdAt": "2026-01-01T00:00:00Z",
             "event": "push"}],
}


class GitHubDelta(unittest.TestCase):
    def test_unavailable_collection_is_not_empty(self):
        for key in SNAPSHOT:
            for unavailable in ({}, {key: None}):
                with self.subTest(key=key, unavailable=unavailable):
                    old = {key: SNAPSHOT[key]}
                    self.assertEqual(pmap.github_delta(old, unavailable),
                                     "unknown (no comparable GitHub collections)")
                    self.assertEqual(pmap.github_delta(unavailable, old),
                                     "unknown (no comparable GitHub collections)")

    def test_successful_empty_lists_report_real_closures(self):
        delta = pmap.github_delta(SNAPSHOT, {key: [] for key in SNAPSHOT})
        self.assertEqual(delta, {"prs_gone": ["#1 PR (merged or closed)"], "issues_closed": ["#2"]})

    def test_partial_failure_keeps_successful_collection_changes(self):
        self.assertEqual(pmap.github_delta(SNAPSHOT, {"issues": []}), {"issues_closed": ["#2"]})
        self.assertEqual(pmap.github_delta(SNAPSHOT, {"prs": []}),
                         {"prs_gone": ["#1 PR (merged or closed)"]})


class GitHubBaseline(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.r = Repo(self.tmp.name)
        self.r.commit("src/core.py", "x = 1\n", "init")
        sh("git", "-C", self.r.dir, "remote", "add", "origin", "https://github.com/example/project.git")

    def collect(self, responses=None, no_gh=False, installed=True):
        responses = copy.deepcopy(RESPONSES if responses is None else responses)
        real_run = pmap.run

        def run(args, *a, **kw):
            if args[0] != "gh":
                return real_run(args, *a, **kw)
            response = responses.get(args[1])
            if response is None:
                return False, "HTTP 403: unavailable"
            if isinstance(response, str):
                return True, response  # simulate a successful command with unreadable JSON
            return True, json.dumps(response)

        with mock.patch.object(pmap.shutil, "which", return_value="gh" if installed else None), \
                mock.patch.object(pmap, "run", side_effect=run), contextlib.redirect_stdout(io.StringIO()):
            pmap.collect(self.r.dir, self.r.out, no_gh)
        return self.r.facts()

    def baseline(self, pending=False):
        filename = "pending_baseline.json" if pending else "baseline.json"
        return json.loads(read(os.path.join(self.r.out, filename)))["github"]

    def render(self):
        self.r.write_map(MAP)
        self.r.pmap("render")

    def seed(self):
        self.collect()
        self.render()
        return self.baseline()

    def test_collector_distinguishes_failure_from_empty_success(self):
        for failure in (None, "not JSON"):
            with self.subTest(failure=failure):
                facts = self.collect(dict(RESPONSES, issue=failure, pr=[]))
                self.assertNotIn("issues", facts["github"])
                self.assertEqual(facts["github"]["prs"], [])
                self.assertTrue(any("gh issue list" in warning for warning in facts["warnings"]))

    def test_partial_failure_preserves_only_missing_collection_until_recovery(self):
        for failed, changed, gone in (("issue", "pr", "prs_gone"), ("pr", "issue", "issues_closed")):
            with self.subTest(failed=failed):
                before = self.seed()
                responses = dict(RESPONSES, **{failed: None, changed: []})
                facts = self.collect(responses)
                missing_key = "issues" if failed == "issue" else "prs"
                changed_key = "prs" if changed == "pr" else "issues"
                self.assertNotIn(missing_key, facts["github"])
                self.assertEqual(set(facts["delta"]["github"]), {gone})
                self.assertEqual(self.baseline(), before)  # collection alone does not promote
                self.assertEqual(self.baseline(pending=True)[missing_key], before[missing_key])
                self.assertEqual(self.baseline(pending=True)[changed_key], [])
                self.render()
                self.assertEqual(self.baseline()[missing_key], before[missing_key])
                self.assertEqual(self.baseline()[changed_key], [])
                recovered = self.collect(dict(RESPONSES, **{changed: []}))
                self.assertEqual(recovered["delta"]["github"], "no changes")

    def test_empty_success_replaces_baseline_and_later_open_is_reported(self):
        self.seed()
        self.collect(dict(RESPONSES, issue=[], pr=[]))
        self.render()
        self.assertEqual(self.baseline()["issues"], [])
        self.assertEqual(self.baseline()["prs"], [])
        delta = self.collect()["delta"]["github"]
        self.assertEqual(delta, {"prs_opened": ["#1 PR"], "issues_opened": ["#2 Issue"]})

    def test_failed_ci_query_preserves_run_baseline_without_replaying_runs(self):
        before = self.seed()
        facts = self.collect(dict(RESPONSES, run=None))
        self.assertNotIn("default_branch_runs", facts["github"])
        self.render()
        self.assertEqual(self.baseline()["default_branch_runs"], before["default_branch_runs"])
        self.assertEqual(self.collect()["delta"]["github"], "no changes")

    def test_all_queries_failing_remains_unknown_and_does_not_clear_baseline(self):
        before = self.seed()
        for _ in range(2):
            facts = self.collect({})
            self.assertEqual(facts["github"], {"default_branch": None})
            self.assertEqual(facts["delta"]["github"], "unknown (no comparable GitHub collections)")
            self.assertEqual(len(facts["warnings"]), 3)
            self.render()
            self.assertEqual(self.baseline(), before)
        self.assertEqual(self.collect()["delta"]["github"], "no changes")

    def test_recovery_reports_changes_since_last_success(self):
        self.seed()
        self.collect({})
        self.render()
        recovered = copy.deepcopy(RESPONSES)
        recovered["pr"][0]["reviewDecision"] = "CHANGES_REQUESTED"
        recovered["issue"] = []
        recovered["run"][0]["databaseId"] = 4
        delta = self.collect(recovered)["delta"]["github"]
        self.assertEqual(delta, {"prs_changed": ["#1: review: APPROVED -> CHANGES_REQUESTED"],
                                 "issues_closed": ["#2"], "new_ci_runs": ["CI abc1234: success"]})
        self.render()
        self.assertEqual(self.collect(recovered)["delta"]["github"], "no changes")

    def test_missing_gh_or_no_gh_does_not_clear_baseline(self):
        for kwargs in ({"installed": False}, {"no_gh": True}):
            with self.subTest(kwargs=kwargs):
                before = self.seed()
                facts = self.collect(**kwargs)
                self.assertFalse(facts.get("github"))
                self.render()
                self.assertEqual(self.baseline(), before)
                self.assertEqual(self.collect()["delta"]["github"], "no changes")

    def test_first_unavailable_sample_does_not_invent_opened_items_on_recovery(self):
        self.collect({})
        self.render()
        recovered = self.collect()
        self.assertEqual(recovered["delta"]["github"], "unknown (no comparable GitHub collections)")
        self.render()
        self.assertEqual(self.collect()["delta"]["github"], "no changes")


if __name__ == "__main__":
    unittest.main()
