import json
import re
import subprocess
import tempfile
import unittest
from pathlib import Path

from downstream import refresh
from downstream.build import git
from downstream.controller import (
    Client,
    Controller,
    Remote,
    apply,
    issue_marker_text,
    parse_marker,
    refresh_at,
    run_mode,
)
from downstream.delivery import ACTIONS_APP

REPOSITORY = "BenchBox-dev/benchbox-sqlglot"
TAG = "v1.1.0"
BRANCH = "refresh/upstream-1.1.0"
CLEAN = "VALUE = 'upstream'\nMIDDLE = 1\nOTHER = 'changed'\n"
CONFLICTING = "VALUE = 'rewritten upstream'\nMIDDLE = 1\nOTHER = 'upstream'\n"


def run(root, *args):
    return subprocess.run(
        ["git", "-C", str(root), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


class FakeApi(Client):
    def __init__(self, main, remote_head):
        super().__init__("token", REPOSITORY)
        self.main = main
        self.remote_head = remote_head
        self.pulls = {}
        self.issues = {}
        self.runs = {}
        self.statuses = {}
        self.writes = []
        self.rules = [
            {
                "type": "required_status_checks",
                "parameters": {
                    "required_status_checks": [{"context": "build", "integration_id": ACTIONS_APP}]
                },
            }
        ]
        self.counter = 0

    def get(self, path, params=None):
        if path == "branches/main":
            return {
                "commit": {"sha": self.main},
                "protection": {
                    "required_status_checks": {
                        "enforcement_level": "off",
                        "contexts": [],
                        "checks": [],
                    }
                },
            }
        if path == "rules/branches/main":
            return self.rules
        if match := re.fullmatch(r"pulls/(\d+)", path):
            return self.pulls[int(match.group(1))]
        if match := re.fullmatch(r"commits/(\w+)/status", path):
            return {"statuses": self.statuses.get(match.group(1), [])}
        raise AssertionError(f"unexpected read {path}")

    def pages(self, path, key, params=None):
        match = re.fullmatch(r"commits/(\w+)/check-runs", path)
        assert match, path
        return self.runs.get(match.group(1), [])

    def lists(self, path, params=None):
        if path == "pulls":
            return list(self.pulls.values())
        if path == "issues":
            return [i for i in self.issues.values() if i["state"] == "open"]
        raise AssertionError(f"unexpected listing {path}")

    def send(self, method, path, body=None):
        self.writes.append((method, path, body))
        self.counter += 1
        if (method, path) == ("POST", "pulls"):
            number = len(self.pulls) + 1
            self.pulls[number] = {
                "number": number,
                "state": "open",
                "draft": False,
                "merged": False,
                "labels": [],
                "body": body["body"],
                "mergeable": True,
                "mergeable_state": "clean",
                "head": {
                    "ref": body["head"],
                    "sha": self.remote_head(body["head"]),
                    "repo": {"full_name": REPOSITORY},
                },
            }
            return {"number": number}
        if (method, path) == ("POST", "issues"):
            number = 100 + len(self.issues)
            self.issues[number] = {
                "number": number,
                "state": "open",
                "user": {"type": "Bot"},
                "title": body["title"],
                "body": body["body"],
            }
            return {"number": number}
        if match := re.fullmatch(r"pulls/(\d+)", path):
            self.pulls[int(match.group(1))].update(body)
            return {}
        if match := re.fullmatch(r"pulls/(\d+)/merge", path):
            pull = self.pulls[int(match.group(1))]
            assert body["sha"] == pull["head"]["sha"]
            pull.update(state="closed", merged=True)
            return {"merged": True}
        if match := re.fullmatch(r"issues/(\d+)", path):
            self.issues[int(match.group(1))].update(body)
            return {}
        raise AssertionError(f"unexpected write {method} {path}")


class Env:
    def __init__(self, test, upstream_runtime=CLEAN):
        self.test = test
        self.directory = Path(tempfile.mkdtemp())
        test.addCleanup(lambda: subprocess.run(["rm", "-rf", str(self.directory)], check=False))
        self.source = self.directory / "source"
        self.source.mkdir()
        self.build(upstream_runtime)
        self.remote_dir = self.directory / "remote.git"
        run(self.directory, "init", "-q", "--bare", "-b", "main", str(self.remote_dir))
        run(self.source, "push", "-q", str(self.remote_dir), "main")
        self.out = self.directory / "out"
        self.out.mkdir()
        self.api = FakeApi(
            run(self.source, "rev-parse", "HEAD"),
            lambda branch: self.remote_sha(branch),
        )
        self.latest = None

    def build(self, upstream_runtime):
        root = self.source
        run(root, "init", "-q", "-b", "main")
        run(root, "config", "user.name", "Test Fixture")
        run(root, "config", "user.email", "fixture@example.invalid")
        (root / "runtime.py").write_text("VALUE = 'upstream'\nMIDDLE = 1\nOTHER = 'upstream'\n")
        self.commit(["runtime.py"], "Record upstream runtime")
        run(root, "tag", "v1.0.0")
        base = git(root, "rev-parse", "HEAD")
        (root / "runtime.py").write_text("VALUE = 'downstream'\nMIDDLE = 1\nOTHER = 'upstream'\n")
        patch = subprocess.check_output(["git", "-C", str(root), "diff", "--binary"])
        downstream = root / "downstream"
        downstream.mkdir()
        (downstream / "runtime.patch").write_bytes(patch)
        (downstream / "README.md").write_text("The base is upstream 1.0.0 and the entries.\n")
        data = {
            "schema": 1,
            "version": "1.0.0.1",
            "upstream": {"repository": str(root), "tag": "v1.0.0", "sha": base},
            "patches": [
                {
                    "id": "runtime",
                    "status": "carried",
                    "paths": ["runtime.py"],
                    "patch": "downstream/runtime.patch",
                    "upstream_state": "not_offered",
                    "upstream_reason": "Fixture patch",
                }
            ],
        }
        (downstream / "patches.json").write_text(json.dumps(data))
        self.commit(
            [
                "runtime.py",
                "downstream/runtime.patch",
                "downstream/README.md",
                "downstream/patches.json",
            ],
            "Record downstream patch",
        )
        run(root, "switch", "-qc", "upstream", "v1.0.0")
        (root / "runtime.py").write_text(upstream_runtime)
        self.commit(["runtime.py"], "Extend upstream runtime")
        run(root, "tag", TAG)
        run(root, "switch", "-q", "main")

    def commit(self, paths, message):
        run(self.source, "add", "--", *paths)
        run(self.source, "commit", "-qm", message)

    def remote_sha(self, branch):
        result = subprocess.run(
            [
                "git",
                "-C",
                str(self.remote_dir),
                "rev-parse",
                "--verify",
                "--quiet",
                f"refs/heads/{branch}",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        return result.stdout.strip() or None

    def controller(self, retry=False, refresher=None):
        extra = {"refresher": refresher} if refresher else {}
        return Controller(
            self.api,
            self.source,
            Remote(str(self.remote_dir)),
            self.directory / "scratch",
            self.out / "refresh.bundle",
            run_url="https://example.invalid/run/1",
            retry=retry,
            **extra,
        )

    def plan(self, **kwargs):
        return self.controller(**kwargs).plan()

    def apply(self, plan, mode="enabled", ops=None):
        if ops is not None:
            plan = {**plan, "actions": ops}
        return apply(self.controller(), plan, mode, self.out / "refresh.bundle")

    def publish(self):
        plan = self.plan()
        self.test.assertEqual(plan["outcome"], "refreshed", plan)
        result = self.apply(plan)
        self.test.assertNotIn("halted", result)
        return plan, result

    def advance_main(self):
        (self.source / "notes.txt").write_text("main moved\n")
        self.commit(["notes.txt"], "chore: move main")
        self.api.main = git(self.source, "rev-parse", "HEAD")

    def human_push(self, branch, parent=None, force=False):
        clone = Path(tempfile.mkdtemp(dir=self.directory))
        run(self.directory, "clone", "-q", str(self.remote_dir), str(clone))
        run(clone, "config", "user.name", "Human")
        run(clone, "config", "user.email", "human@example.invalid")
        if parent is None and self.remote_sha(branch):
            run(clone, "checkout", "-q", "-b", branch, f"origin/{branch}")
        else:
            run(clone, "checkout", "-q", "-b", branch, parent or "origin/main")
        (clone / "human.txt").write_text("manual change\n")
        run(clone, "add", "human.txt")
        run(clone, "commit", "-qm", "manual change")
        args = ["push", "-q", "origin", f"{branch}:{branch}"]
        run(clone, *(args + (["--force"] if force else [])))
        return run(clone, "rev-parse", "HEAD")

    def pass_checks(self, sha, app=ACTIONS_APP, conclusion="success", status="completed"):
        self.api.runs[sha] = [
            {"name": "build", "status": status, "conclusion": conclusion, "app": {"id": app}}
        ]

    def seed_issue(self, base, outcome, user="Bot"):
        number = 500 + len(self.api.issues)
        self.api.issues[number] = {
            "number": number,
            "state": "open",
            "user": {"type": user},
            "title": "seed",
            "body": issue_marker_text({"base": base, "outcome": outcome, "tag": TAG}),
        }
        return number


def ops(plan):
    return [action["op"] for action in plan["actions"]]


class TestUpstreamController(unittest.TestCase):
    def test_clean_refresh_plans_and_applies_a_push_and_a_pull_request(self):
        env = Env(self)
        stale = env.seed_issue("0" * 40, "conflict")
        plan = env.plan()
        self.assertEqual(plan["outcome"], "refreshed")
        self.assertEqual(ops(plan), ["push_branch", "open_pr", "close_issues"])
        self.assertEqual(env.api.writes, [])
        self.assertIsNone(env.remote_sha(BRANCH))
        self.assertTrue((env.out / "refresh.bundle").is_file())
        result = env.apply(plan)
        self.assertEqual([r["status"] for r in result["performed"]], ["pushed", "opened", "closed"])
        head = env.remote_sha(BRANCH)
        marker = parse_marker(env.api.pulls[1]["body"])
        self.assertEqual(marker["head"], head)
        self.assertEqual(marker["base"], env.api.main)
        self.assertEqual(marker["tree"], git(env.source, "rev-parse", f"{head}^{{tree}}"))
        self.assertEqual(env.api.issues[stale]["state"], "closed")
        self.assertEqual(git(env.source, "rev-parse", f"{head}^1"), env.api.main)
        self.assertEqual(git(env.source, "branch", "--list", "refresh/*"), "")

    def test_unchanged_conflict_is_skipped_and_retry_reattempts(self):
        env = Env(self, CONFLICTING)
        calls = []

        def counting(*args, **kwargs):
            calls.append(args[2])
            return refresh_at(*args, **kwargs)

        plan = env.plan(refresher=counting)
        self.assertEqual(plan["outcome"], "conflict")
        self.assertEqual(ops(plan), ["upsert_issue"])
        self.assertEqual(plan["actions"][0]["key"]["outcome"], "conflict")
        env.apply(plan)
        self.assertEqual(len(env.api.issues), 1)
        self.assertEqual(len(calls), 1)
        skipped = env.plan(refresher=counting)
        self.assertEqual((skipped["outcome"], skipped["actions"]), ("skipped", []))
        self.assertEqual(len(calls), 1)
        again = env.plan(retry=True, refresher=counting)
        self.assertEqual(len(calls), 2)
        self.assertEqual(again["outcome"], "conflict")
        self.assertEqual(env.apply(again)["performed"][0]["status"], "unchanged")
        self.assertEqual(len(env.api.issues), 1)
        env.advance_main()
        moved = env.plan(refresher=counting)
        self.assertEqual(len(calls), 3)
        self.assertEqual(ops(moved), ["upsert_issue"])
        env.apply(moved)
        self.assertEqual(len(env.api.issues), 1)
        key = json.loads(
            re.search(r"issue (\{.*?\}) -->", next(iter(env.api.issues.values()))["body"]).group(1)
        )
        self.assertEqual(key["base"], env.api.main)

    def test_a_human_authored_issue_cannot_suppress_the_refresh(self):
        env = Env(self, CONFLICTING)
        env.seed_issue(env.api.main, "conflict", user="User")
        plan = env.plan()
        self.assertEqual(plan["outcome"], "conflict")
        self.assertEqual(ops(plan), ["upsert_issue"])

    def test_a_human_branch_is_never_overwritten(self):
        env = Env(self)
        human = env.human_push(BRANCH, parent="origin/main")
        plan = env.plan()
        self.assertEqual(plan["outcome"], "refused-branch-collision")
        self.assertEqual(ops(plan), ["upsert_issue"])
        env.apply(plan)
        self.assertEqual(env.remote_sha(BRANCH), human)
        self.assertEqual(env.api.pulls, {})

    def test_a_human_pull_request_is_left_untouched(self):
        env = Env(self)
        human = env.human_push(BRANCH, parent="origin/main")
        env.api.pulls[1] = {
            "number": 1,
            "state": "open",
            "draft": False,
            "labels": [],
            "body": "my own change",
            "head": {"ref": BRANCH, "sha": human, "repo": {"full_name": REPOSITORY}},
        }
        plan = env.plan()
        self.assertEqual(plan["outcome"], "refused-foreign-pr")
        self.assertEqual(ops(plan), ["upsert_issue"])
        env.apply(plan)
        self.assertEqual(env.remote_sha(BRANCH), human)
        self.assertEqual([w[1] for w in env.api.writes], ["issues"])

    def test_an_independently_modified_head_is_refused(self):
        env = Env(self)
        env.publish()
        moved = env.human_push(BRANCH)
        env.api.pulls[1]["head"]["sha"] = moved
        env.pass_checks(moved)
        plan = env.plan()
        self.assertEqual(plan["outcome"], "refused-head-modified")
        self.assertEqual(ops(plan), ["upsert_issue"])
        self.assertNotIn("merge_pr", ops(plan))

    def test_a_forged_marker_cannot_claim_a_modified_head(self):
        env = Env(self)
        env.publish()
        moved = env.human_push(BRANCH)
        pull = env.api.pulls[1]
        marker = parse_marker(pull["body"])
        forged = {
            **marker,
            "head": moved,
            "tree": git(env.remote_dir, "rev-parse", f"{moved}^{{tree}}"),
        }
        pull["body"] = pull["body"].replace(
            re.search(r"<!-- benchbox-upstream .*? -->", pull["body"]).group(0),
            "<!-- benchbox-upstream " + json.dumps(forged) + " -->",
        )
        pull["head"]["sha"] = moved
        env.pass_checks(moved)
        plan = env.plan()
        self.assertEqual(plan["outcome"], "refused-head-unproven")
        self.assertNotIn("merge_pr", ops(plan))

    def test_draft_closed_and_held_pull_requests_are_untouched(self):
        for name, change, outcome, issue in (
            ("draft", {"draft": True}, "draft", False),
            ("held", {"labels": [{"name": "no-auto-merge"}]}, "held", False),
            ("closed", {"state": "closed"}, "refused-closed", True),
        ):
            with self.subTest(name):
                env = Env(self)
                env.publish()
                head = env.remote_sha(BRANCH)
                env.pass_checks(head)
                env.api.pulls[1].update(change)
                writes = len(env.api.writes)
                plan = env.plan()
                self.assertEqual(plan["outcome"], outcome)
                self.assertEqual(ops(plan), ["upsert_issue"] if issue else [])
                self.assertNotIn("merge_pr", ops(plan))
                env.apply(plan)
                self.assertEqual(env.remote_sha(BRANCH), head)
                self.assertEqual(len(env.api.pulls), 1)
                self.assertEqual(len(env.api.writes) - writes, 1 if issue else 0)

    def test_a_stale_base_is_refreshed_in_place(self):
        env = Env(self)
        env.publish()
        old = env.remote_sha(BRANCH)
        env.advance_main()
        plan = env.plan()
        self.assertEqual(plan["outcome"], "refreshed")
        self.assertEqual(ops(plan), ["update_pr", "push_branch", "update_pr"])
        result = env.apply(plan)
        self.assertNotIn("halted", result)
        new = env.remote_sha(BRANCH)
        self.assertNotEqual(new, old)
        self.assertEqual(git(env.source, "rev-parse", f"{new}^1"), env.api.main)
        self.assertEqual(len(env.api.pulls), 1)
        marker = parse_marker(env.api.pulls[1]["body"])
        self.assertEqual((marker["head"], marker["base"]), (new, env.api.main))
        self.assertNotIn("pending", marker)

    def test_partial_stale_refresh_recovers_without_duplicates(self):
        env = Env(self)
        env.publish()
        env.advance_main()
        plan = env.plan()
        env.apply(plan, ops=plan["actions"][:2])
        new = env.remote_sha(BRANCH)
        env.api.pulls[1]["head"]["sha"] = new
        pending = parse_marker(env.api.pulls[1]["body"])
        self.assertEqual(pending["pending"]["head"], new)
        recovery = env.plan()
        self.assertEqual(ops(recovery), ["update_pr"])
        env.apply(recovery)
        self.assertEqual(env.plan()["actions"], [])
        self.assertEqual(env.apply(env.plan())["performed"], [])
        self.assertEqual(len(env.api.pulls), 1)

    def test_a_branch_pushed_without_its_pull_request_is_adopted(self):
        env = Env(self)
        plan = env.plan()
        env.apply(plan, ops=plan["actions"][:1])
        self.assertEqual(env.api.pulls, {})
        recovery = env.plan()
        self.assertEqual(ops(recovery), ["open_pr"])
        env.apply(recovery)
        self.assertEqual(len(env.api.pulls), 1)
        self.assertEqual(env.api.pulls[1]["head"]["sha"], env.remote_sha(BRANCH))

    def test_reapplying_a_plan_is_idempotent(self):
        env = Env(self)
        plan = env.plan()
        env.apply(plan)
        pushed = env.remote_sha(BRANCH)
        writes = len(env.api.writes)
        again = env.apply(plan)
        self.assertEqual([r["status"] for r in again["performed"]], ["unchanged", "unchanged"])
        self.assertEqual(env.remote_sha(BRANCH), pushed)
        self.assertEqual(len(env.api.writes), writes)
        self.assertEqual(env.plan()["actions"], [])

    def test_publish_halts_when_the_branch_moved_after_the_plan(self):
        env = Env(self)
        plan = env.plan()
        human = env.human_push(BRANCH, parent="origin/main")
        result = env.apply(plan)
        self.assertIn("leaving it alone", result["halted"])
        self.assertEqual(result["performed"], [])
        self.assertEqual(env.remote_sha(BRANCH), human)
        self.assertEqual(env.api.writes, [])

    def test_publish_halts_when_main_moved_after_the_plan(self):
        env = Env(self)
        plan = env.plan()
        env.api.main = "f" * 40
        result = env.apply(plan)
        self.assertIn("plan was made on", result["halted"])
        self.assertIsNone(env.remote_sha(BRANCH))

    def test_publish_refuses_a_bundle_that_differs_from_the_plan(self):
        env = Env(self)
        plan = env.plan()
        plan["actions"][0]["tree"] = "e" * 40
        result = env.apply(plan)
        self.assertIn("planned tree", result["halted"])
        self.assertIsNone(env.remote_sha(BRANCH))

    def test_merge_requires_every_required_check_from_the_required_publisher(self):
        cases = (
            ("missing", None, "waiting", []),
            ("pending", {"status": "in_progress", "conclusion": None}, "waiting", []),
            ("failed", {"conclusion": "failure"}, "checks-failed", ["upsert_issue"]),
            ("wrong publisher", {"app": 999}, "checks-failed", ["upsert_issue"]),
        )
        for name, checks, outcome, expected in cases:
            with self.subTest(name):
                env = Env(self)
                env.publish()
                head = env.remote_sha(BRANCH)
                if checks is not None:
                    env.pass_checks(head, **checks)
                plan = env.plan()
                self.assertEqual(plan["outcome"], outcome)
                self.assertEqual(ops(plan), expected)
                env.apply(plan)
                self.assertFalse(env.api.pulls[1]["merged"])

    def test_merge_is_bound_to_the_exact_head_with_a_merge_commit(self):
        env = Env(self)
        env.publish()
        head = env.remote_sha(BRANCH)
        env.pass_checks(head)
        env.api.pulls[1]["mergeable_state"] = "blocked"
        self.assertEqual(env.plan()["outcome"], "waiting")
        env.api.pulls[1]["mergeable_state"] = "clean"
        plan = env.plan()
        self.assertEqual((plan["outcome"], ops(plan)), ("merge", ["merge_pr"]))
        result = env.apply(plan)
        self.assertEqual(result["performed"][0]["status"], "merged")
        method, path, body = env.api.writes[-1]
        self.assertEqual((method, path), ("PUT", "pulls/1/merge"))
        self.assertEqual((body["merge_method"], body["sha"]), ("merge", head))
        writes = len(env.api.writes)
        self.assertEqual(env.apply(plan)["performed"][0]["status"], "unchanged")
        self.assertEqual(len(env.api.writes), writes)

    def test_publish_rechecks_hold_and_checks_before_merging(self):
        env = Env(self)
        env.publish()
        head = env.remote_sha(BRANCH)
        env.pass_checks(head)
        plan = env.plan()
        self.assertEqual(ops(plan), ["merge_pr"])
        env.api.pulls[1]["labels"] = [{"name": "no-auto-merge"}]
        result = env.apply(plan)
        self.assertEqual(result["performed"][0]["status"], "blocked")
        env.api.pulls[1]["labels"] = []
        env.pass_checks(head, conclusion="failure")
        self.assertEqual(env.apply(plan)["performed"][0]["status"], "failed")
        self.assertFalse(env.api.pulls[1]["merged"])
        self.assertNotIn("PUT", [w[0] for w in env.api.writes])

    def test_dry_run_and_a_disabled_kill_switch_mutate_nothing(self):
        self.assertEqual(run_mode(False, False), "disabled")
        self.assertEqual(run_mode(False, True), "disabled")
        self.assertEqual(run_mode(True, True), "dry-run")
        self.assertEqual(run_mode(True, False), "enabled")
        env = Env(self)
        plan = env.plan()
        self.assertEqual(ops(plan), ["push_branch", "open_pr"])
        for mode in ("dry-run", "disabled"):
            result = env.apply(plan, mode=mode)
            self.assertEqual(result["performed"], [])
            self.assertEqual(result["unapplied"], plan["actions"])
            self.assertEqual(env.api.writes, [])
            self.assertIsNone(env.remote_sha(BRANCH))

    def test_up_to_date_closes_a_lingering_issue(self):
        env = Env(self)
        number = env.seed_issue(env.api.main, "conflict")
        controller = env.controller()
        controller.discover = lambda root: ("v1.0.0", "x")
        plan = controller.plan()
        self.assertEqual(plan["outcome"], "up-to-date")
        self.assertEqual(ops(plan), ["close_issues"])
        apply(controller, plan, "enabled", env.out / "refresh.bundle")
        self.assertEqual(env.api.issues[number]["state"], "closed")

    def test_a_moved_main_defers_to_the_next_run(self):
        env = Env(self)
        env.api.main = "a" * 40
        plan = env.plan()
        self.assertEqual((plan["outcome"], plan["actions"]), ("main-moved", []))

    def test_discovery_reads_the_newest_final_tag(self):
        env = Env(self)
        run(env.source, "tag", "v1.2.0-rc1", TAG)
        self.assertEqual(refresh.newest_tag(env.source)[0], TAG)


if __name__ == "__main__":
    unittest.main()
