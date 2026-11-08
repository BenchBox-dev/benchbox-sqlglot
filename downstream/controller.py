from __future__ import annotations

import argparse
import base64
import http.client
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from downstream import refresh
from downstream.build import ROOT, git
from downstream.delivery import (
    MAX_PAGES,
    REPOSITORY,
    SHA,
    DeliveryError,
    GitHub,
    live_required_contexts,
)

SCHEMA = 1
BRANCH_PREFIX = "refresh/upstream-"
HOLD_LABELS = frozenset({"no-auto-merge", "hold"})
REPORT_ONLY = frozenset({"draft", "held"})
PREPARE_FAILED = "prepare-failed"
PUBLISHING = frozenset({"push_branch", "open_pr", "update_pr", "merge_pr"})
PUBLICATION_FAILURES = frozenset({"publish-refused", "publish-failed"})
FAILED_INPUTS = frozenset({"conflict", "failed"}) | PUBLICATION_FAILURES
STATE_FILE = "state.json"
RESULT_FILE = "result.json"
BUNDLE_FILE = "refresh.bundle"
PR_MARKER = re.compile(r"<!-- benchbox-upstream (\{[^\n]*?\}) -->")
ISSUE_MARKER = re.compile(r"<!-- benchbox-upstream-issue (\{[^\n]*?\}) -->")
TAG = re.compile(r"^v\d+\.\d+\.\d+$")
CHECK_PASS = {"success"}
MERGEABLE_POLLS = (2, 4, 8)
UNSETTLED = frozenset({"unknown", "blocked"})
EVENT_WORKFLOWS = frozenset(
    {"BenchBox distribution", "BenchBox real engines", "Run tests and linter checks"}
)
MERGE_ONLY = frozenset({"merge_pr"})


class ControllerError(Exception):
    pass


class Refused(Exception):
    pass


TRANSPORT = (OSError, http.client.HTTPException, ValueError)


class Client(GitHub):
    def get(self, path: str, params: dict | None = None) -> Any:
        try:
            return super().get(path, params)
        except DeliveryError:
            raise
        except TRANSPORT as error:
            raise ControllerError(f"GET {path} failed: {error!r}") from error

    def lists(self, path: str, params: dict | None = None) -> list:
        items: list = []
        for page in range(1, MAX_PAGES + 1):
            batch = self.get(path, {**(params or {}), "per_page": 100, "page": page})
            items.extend(batch)
            if len(batch) < 100:
                return items
        raise DeliveryError(f"{path} exceeds {MAX_PAGES} pages; the listing is incomplete")

    def send(self, method: str, path: str, body: dict | None = None) -> Any:
        url = f"https://api.github.com/repos/{self.repository}/{path}"
        headers = dict(self._request(url).header_items())
        headers["Content-Type"] = "application/json"
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                text = response.read().decode()
        except urllib.error.HTTPError as error:
            try:
                detail = error.read()[:500].decode(errors="replace")
            except TRANSPORT:
                detail = "the error body could not be read"
            raise ControllerError(f"{method} {path} failed with {error.code}: {detail}") from error
        except TRANSPORT as error:
            raise ControllerError(f"{method} {path} failed: {error!r}") from error
        try:
            return json.loads(text) if text else None
        except ValueError as error:
            raise ControllerError(f"{method} {path} returned unreadable JSON: {error}") from error


class Remote:
    def __init__(self, url: str, token: str | None = None):
        self.url = url
        self.token = token

    def run(self, root: Path, *args: str) -> subprocess.CompletedProcess:
        env = dict(os.environ)
        if self.token:
            encoded = base64.b64encode(f"x-access-token:{self.token}".encode()).decode()
            env.update(
                GIT_CONFIG_COUNT="1",
                GIT_CONFIG_KEY_0="http.extraheader",
                GIT_CONFIG_VALUE_0=f"AUTHORIZATION: basic {encoded}",
            )
        return subprocess.run(
            ["git", "-C", str(root), *args], env=env, capture_output=True, text=True, check=False
        )

    def head(self, root: Path, branch: str) -> str | None:
        result = self.run(root, "ls-remote", self.url, f"refs/heads/{branch}")
        if result.returncode:
            raise ControllerError(f"Cannot list {branch}: {result.stderr.strip()[-300:]}")
        lines = result.stdout.split()
        return lines[0] if lines else None

    def fetch(self, root: Path, branch: str) -> str | None:
        if self.head(root, branch) is None:
            return None
        ref = f"refs/controller/remote/{branch}"
        result = self.run(root, "fetch", "--no-tags", self.url, f"+refs/heads/{branch}:{ref}")
        if result.returncode:
            raise ControllerError(f"Cannot fetch {branch}: {result.stderr.strip()[-300:]}")
        return git(root, "rev-parse", ref)

    def push(self, root: Path, branch: str, head: str, expect: str | None) -> None:
        lease = f"--force-with-lease=refs/heads/{branch}:{expect or ''}"
        result = self.run(root, "push", lease, self.url, f"{head}:refs/heads/{branch}")
        if result.returncode:
            raise Refused(f"Push of {branch} was rejected: {result.stderr.strip()[-300:]}")


@dataclass
class Result:
    report: dict
    head: str | None = None
    tree: str | None = None
    parents: list[str] = field(default_factory=list)

    @property
    def status(self) -> str:
        return self.report["status"]


def refresh_at(
    root: Path, tag: str, base: str, scratch: Path, bundle: Path | None = None
) -> Result:
    scratch.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(dir=scratch))
    checkout = work / "base"
    added = subprocess.run(
        ["git", "-C", str(root), "worktree", "add", "--detach", str(checkout), base],
        capture_output=True,
        text=True,
        check=False,
    )
    if added.returncode:
        shutil.rmtree(work, ignore_errors=True)
        return Result({"status": "failed", "step": "base", "detail": added.stderr.strip()[-500:]})
    report: dict = {"status": "failed", "step": "refresh", "detail": "refresh did not run"}
    try:
        report = refresh.auto(checkout, tag, work / "auto")
        if report["status"] != "refreshed":
            return Result(report)
        integration = Path(report["worktree"])
        head = git(integration, "rev-parse", "HEAD")
        result = Result(
            report,
            head,
            git(integration, "rev-parse", "HEAD^{tree}"),
            git(integration, "rev-list", "--parents", "-n", "1", "HEAD").split()[1:],
        )
        if bundle is not None:
            ref = f"refs/heads/{report['branch']}"
            subprocess.run(
                ["git", "-C", str(checkout), "bundle", "create", str(bundle), ref, f"^{base}"],
                check=True,
                capture_output=True,
            )
        return result
    finally:
        if report.get("status") == "refreshed":
            refresh.discard(checkout, Path(report["worktree"]), report["branch"])
        subprocess.run(
            ["git", "-C", str(root), "worktree", "remove", "--force", str(checkout)],
            capture_output=True,
            check=False,
        )
        shutil.rmtree(work, ignore_errors=True)


def marker_text(data: dict) -> str:
    return (
        "<!-- benchbox-upstream " + json.dumps(data, sort_keys=True, separators=(",", ":")) + " -->"
    )


def issue_marker_text(data: dict) -> str:
    return (
        "<!-- benchbox-upstream-issue "
        + json.dumps(data, sort_keys=True, separators=(",", ":"))
        + " -->"
    )


def _sha(value: Any) -> bool:
    return isinstance(value, str) and bool(SHA.fullmatch(value))


def parse_marker(body: str | None) -> dict | None:
    found = PR_MARKER.findall(body or "")
    if len(found) != 1:
        return None
    try:
        data = json.loads(found[0])
    except ValueError:
        return None
    if not isinstance(data, dict) or not isinstance(data.get("tag"), str):
        return None
    if not all(_sha(data.get(key)) for key in ("base", "head", "tree")):
        return None
    pending = data.get("pending")
    if pending is not None and not (
        isinstance(pending, dict)
        and all(_sha(pending.get(key)) for key in ("base", "head", "tree"))
    ):
        return None
    return data


def parse_issue_marker(body: str | None) -> dict | None:
    found = ISSUE_MARKER.findall(body or "")
    if len(found) != 1:
        return None
    try:
        data = json.loads(found[0])
    except ValueError:
        return None
    if not isinstance(data, dict) or not all(
        isinstance(data.get(key), str) for key in ("tag", "base", "outcome")
    ):
        return None
    return data


def claim(marker: dict, head: str) -> dict | None:
    if head == marker["head"]:
        return {"base": marker["base"], "tree": marker["tree"]}
    pending = marker.get("pending")
    if pending and head == pending["head"]:
        return {"base": pending["base"], "tree": pending["tree"]}
    return None


def replace_marker(body: str | None, data: dict) -> str:
    text = marker_text(data)
    if PR_MARKER.search(body or ""):
        return PR_MARKER.sub(lambda _: text, body or "", count=1)
    return f"{body or ''}\n\n{text}\n"


def pull_title(tag: str) -> str:
    return f"chore: refresh SQLGlot from upstream {tag}"


def pull_body(tag: str, base: str, head: str, tree: str, run_url: str) -> str:
    marker = marker_text({"base": base, "head": head, "tag": tag, "tree": tree})
    return (
        f"Automated refresh of the BenchBox SQLGlot fork onto upstream {tag}.\n\n"
        f"- main base: `{base}`\n- head: `{head}`\n\n"
        "The upstream lifecycle controller merges this pull request with a merge commit, bound to "
        "this exact head, once every required check on main has succeeded on it. Mark it draft or "
        "add the `no-auto-merge` label to hold it. Any push to the branch or edit that changes "
        "the head makes the controller leave it alone.\n\n"
        f"Run: {run_url}\n\n{marker}\n"
    )


def issue_title(tag: str) -> str:
    return f"Upstream refresh to {tag} needs follow-up"


GUIDANCE = {
    "conflict": "Resolve the refresh by hand as described under Upstream refresh in "
    "downstream/MAINTAINING.md.",
    "failed": "Resolve the refresh by hand as described under Upstream refresh in "
    "downstream/MAINTAINING.md.",
    "checks-failed": "A required check on the refresh pull request failed or came from the wrong "
    "publisher. Fix the cause or close the pull request.",
}


def issue_body(tag: str, base: str, outcome: str, detail: str, run_url: str) -> str:
    marker = issue_marker_text({"base": base, "outcome": outcome, "tag": tag})
    guidance = GUIDANCE.get(
        outcome,
        "The controller left the branch or pull request untouched. Resolve it by hand: remove "
        "the cause, then close this issue or dispatch the controller with `retry`.",
    )
    return (
        f"{marker}\n\nThe upstream refresh to `{tag}` on main `{base}` stopped: **{outcome}**.\n\n"
        f"```\n{detail[-1500:]}\n```\n\n{guidance}\n\nThe controller skips this unchanged input "
        "on later runs. A new upstream tag, a new main commit, a `retry` dispatch, or closing "
        "this issue makes it try again; a successful refresh closes it.\n\n"
        f"Latest run: {run_url}\n"
    )


def required_state(api: Client, head: str) -> tuple[list[str], list[str]]:
    required = live_required_contexts(api)
    runs = api.pages(f"commits/{head}/check-runs", "check_runs", {"filter": "latest"})
    statuses = api.get(f"commits/{head}/status").get("statuses", [])
    failed: list[str] = []
    waiting: list[str] = []

    def state(item: dict, is_run: bool) -> str:
        if is_run:
            if item["status"] != "completed":
                return "pending"
            return "success" if item["conclusion"] in CHECK_PASS else "failure"
        return {"success": "success", "pending": "pending"}.get(item["state"], "failure")

    for context in sorted(required):
        named = [run for run in runs if run["name"] == context]
        reported = [status for status in statuses if status["context"] == context]
        if not named and not reported:
            waiting.append(f"{context}: not reported yet")
            continue
        specific = {app for app in required[context] if app is not None and app >= 1}
        for app in sorted(required[context], key=lambda a: -1 if a is None else a):
            if app is None or app < 1:
                states = [state(r, True) for r in named] + [state(r, False) for r in reported]
                if "success" in states:
                    continue
                label = f"{context} (any publisher)"
                states = states or ["missing"]
            else:
                matching = [r for r in named if r["app"]["id"] == app]
                label = f"{context} (app {app})"
                if not matching:
                    if any(r["app"]["id"] in specific for r in named):
                        waiting.append(f"{label}: not reported yet")
                    else:
                        failed.append(f"{label}: reported only by other apps")
                    continue
                states = [state(r, True) for r in matching]
                if all(item == "success" for item in states):
                    continue
            if "failure" in states:
                failed.append(f"{label}: did not succeed")
            else:
                waiting.append(f"{label}: still running")
    return failed, waiting


def labels(pull: dict) -> set[str]:
    return {label["name"] for label in pull.get("labels", [])}


def targets_main(api: Client, pull: dict) -> bool:
    base = pull.get("base") or {}
    return base.get("ref") == "main" and (base.get("repo") or {}).get("full_name") == api.repository


def event_from_env(env: Any = os.environ) -> dict | None:
    """The workflow_run facts passed by the workflow, or None outside that trigger.

    Every value is untrusted: it only selects which live pull request to look at.
    """
    if env.get("EVENT", "") != "workflow_run":
        return None
    return {
        "workflow": env.get("EVENT_WORKFLOW", ""),
        "type": env.get("EVENT_TYPE", ""),
        "conclusion": env.get("EVENT_CONCLUSION", ""),
        "repository": env.get("EVENT_REPOSITORY", ""),
        "head_repository": env.get("EVENT_HEAD_REPOSITORY", ""),
        "branch": env.get("EVENT_BRANCH", ""),
        "head": env.get("EVENT_HEAD_SHA", ""),
    }


def event_refusal(event: dict, repository: str) -> str | None:
    """Why a workflow_run event must not drive the controller, or None when it may."""
    if event.get("workflow") not in EVENT_WORKFLOWS:
        return f"workflow {event.get('workflow')!r} is not one of the required workflows"
    if event.get("type") != "pull_request":
        return f"the run was triggered by {event.get('type')!r}, not a pull request"
    if event.get("conclusion") != "success":
        return (
            f"the run concluded {event.get('conclusion')!r}; only a passing run can unblock a merge"
        )
    if event.get("repository") != repository or event.get("head_repository") != repository:
        return "the run is not from this repository's own branch"
    branch = event.get("branch") or ""
    if not branch.startswith(BRANCH_PREFIX) or not TAG.fullmatch(
        "v" + branch[len(BRANCH_PREFIX) :]
    ):
        return f"branch {branch!r} is not a canonical refresh branch"
    if not _sha(event.get("head")):
        return "the run has no valid head commit"
    return None


def merge_gate(api: Client, number: int, head: str, base: str) -> tuple[str, list[str]]:
    for attempt in range(len(MERGEABLE_POLLS) + 1):
        if attempt:
            time.sleep(MERGEABLE_POLLS[attempt - 1])
        pull = api.get(f"pulls/{number}")
        live = api.get("branches/main")["commit"]["sha"]
        if not targets_main(api, pull):
            return "blocked", ["the pull request does not target main of this repository"]
        if pull["state"] != "open" or pull.get("draft") or labels(pull) & HOLD_LABELS:
            return "blocked", ["the pull request is not open, is draft or is held"]
        if pull["head"]["sha"] != head:
            return "blocked", ["the pull request head is not the proven head"]
        marker = parse_marker(pull.get("body"))
        if marker is None or claim(marker, head) is None:
            return "blocked", ["the pull request no longer carries the proven controller marker"]
        if live != base:
            return "blocked", ["main moved since the plan"]
        try:
            failed, waiting = required_state(api, head)
        except DeliveryError as error:
            return "wait", [str(error)]
        if failed:
            return "failed", failed
        if waiting:
            return "wait", waiting
        if pull.get("mergeable") is not None and pull.get("mergeable_state") not in UNSETTLED:
            break
    if pull.get("mergeable") is not True or pull.get("mergeable_state") != "clean":
        return "wait", [f"merge state is {pull.get('mergeable_state')}"]
    return "ready", []


@dataclass
class Controller:
    api: Client
    root: Path
    remote: Remote
    scratch: Path
    bundle: Path
    run_url: str = ""
    retry: bool = False
    discover: Callable[[Path], tuple[str, str]] = refresh.newest_tag
    refresher: Callable[..., Result] = refresh_at
    cache: dict = field(default_factory=dict)
    app_slug: str = ""
    event: dict | None = None

    def controller_issues(self) -> list[dict]:
        found = []
        for issue in self.api.lists("issues", {"state": "open"}):
            author = issue.get("user") or {}
            if (
                not self.app_slug
                or "pull_request" in issue
                or author.get("type") != "Bot"
                or author.get("login") != f"{self.app_slug}[bot]"
            ):
                continue
            key = parse_issue_marker(issue.get("body"))
            if key:
                found.append({"number": issue["number"], "key": key, "body": issue.get("body")})
        return sorted(found, key=lambda item: item["number"])

    def recomputed(self, tag: str, base: str) -> Result:
        if (tag, base) not in self.cache:
            self.cache[(tag, base)] = self.refresher(self.root, tag, base, self.scratch)
        return self.cache[(tag, base)]

    def plan(self) -> dict:
        """Plan one pass. A workflow_run event narrows it to merging the pull request."""
        if self.event is None:
            return self.full_plan()
        refusal = event_refusal(self.event, self.api.repository)
        if refusal:
            return {
                "schema": SCHEMA,
                "repository": self.api.repository,
                "base": git(self.root, "rev-parse", "HEAD"),
                "run_url": self.run_url,
                "outcome": "ignored",
                "actions": [],
                "notes": [f"Ignored the triggering event: {refusal}"],
            }
        out = self.full_plan()
        out["event"] = {k: self.event[k] for k in ("workflow", "branch", "head")}
        out["actions"] = [a for a in out["actions"] if a["op"] in MERGE_ONLY]
        return out

    def full_plan(self) -> dict:
        base = git(self.root, "rev-parse", "HEAD")
        out: dict = {
            "schema": SCHEMA,
            "repository": self.api.repository,
            "base": base,
            "run_url": self.run_url,
            "actions": [],
            "notes": [],
        }
        live = self.api.get("branches/main")["commit"]["sha"]
        if live != base:
            out.update(outcome="main-moved")
            out["notes"].append(f"main is {live}, the checkout is {base}; the next run decides")
            return out
        current = refresh.manifest(self.root)["upstream"]["tag"]
        tag, _ = self.discover(self.root)
        if not TAG.fullmatch(tag):
            raise ControllerError(f"Unexpected upstream tag {tag!r}")
        out.update(current=current, latest=tag)
        if not refresh.is_newer(tag, current):
            issues = self.controller_issues()
            out["outcome"] = "up-to-date"
            if issues:
                out["actions"].append({"op": "close_issues", "tag": None})
            return out
        out["tag"] = tag
        try:
            return self.decide(out, base, tag)
        except Exception as error:  # noqa: BLE001
            return self.prepare_failed(out, base, tag, error)

    def prepare_failed(self, out: dict, base: str, tag: str, error: Exception) -> dict:
        try:
            issues = self.controller_issues()
        except Exception:  # noqa: BLE001
            issues = []
        out["actions"] = []
        out.pop("pull_request", None)
        out["fail"] = True
        self.problem(out, issues, tag, base, PREPARE_FAILED, f"{type(error).__name__}: {error}")
        return out

    def decide(self, out: dict, base: str, tag: str) -> dict:
        issues = self.controller_issues()
        known = next((i["key"] for i in issues if i["key"]["tag"] == tag), None)
        if (
            known
            and known["base"] == base
            and known["outcome"] == PREPARE_FAILED
            and not self.retry
        ):
            out["outcome"] = "skipped"
            out["notes"].append(
                "Preparing this input already failed; set retry or close the issue to re-attempt"
            )
            return out
        self.reconcile(out, tag, base, issues)
        key = known
        if (
            key
            and key["base"] == base
            and key["outcome"] in PUBLICATION_FAILURES
            and not self.retry
            and any(a["op"] in PUBLISHING for a in out["actions"])
        ):
            out["actions"] = []
            out["outcome"] = "skipped"
            out["notes"].append(
                f"Publication already failed ({key['outcome']}) on this input; set retry or "
                "close the issue to re-attempt"
            )
        return out

    def finish(self, out: dict, outcome: str, issues: list[dict], tag: str) -> None:
        out["outcome"] = outcome
        waiting = outcome == "waiting"
        closable = [
            issue
            for issue in issues
            if issue["key"]["tag"] == tag
            and not (
                waiting
                and issue["key"]["outcome"] in PUBLICATION_FAILURES
                and issue["key"]["base"] == out["base"]
            )
        ]
        if closable:
            op = {"op": "close_issues", "tag": tag}
            if waiting:
                op["keep_publication_base"] = out["base"]
            out["actions"].append(op)

    def problem(
        self, out: dict, issues: list[dict], tag: str, base: str, outcome: str, detail: str
    ) -> None:
        out["outcome"] = outcome
        out["detail"] = detail
        if outcome in REPORT_ONLY:
            return
        key = {"base": base, "outcome": outcome, "tag": tag}
        existing = [issue for issue in issues if issue["key"]["tag"] == tag]
        if existing and existing[0]["key"] == key and not self.retry:
            out["notes"].append("An unchanged failed input is skipped; set retry to re-attempt")
            return
        out["actions"].append(
            {
                "op": "upsert_issue",
                "tag": tag,
                "key": key,
                "title": issue_title(tag),
                "body": issue_body(tag, base, outcome, detail, self.run_url),
            }
        )

    def known_failure(self, issues: list[dict], tag: str, base: str) -> bool:
        key = next((i["key"] for i in issues if i["key"]["tag"] == tag), None)
        return bool(
            key and key["base"] == base and key["outcome"] in FAILED_INPUTS and not self.retry
        )

    def reconcile(self, out: dict, tag: str, base: str, issues: list[dict]) -> None:
        branch = BRANCH_PREFIX + tag[1:]
        out["branch"] = branch
        repository = self.api.repository
        pulls = [
            pull
            for pull in self.api.lists(
                "pulls", {"state": "all", "head": f"{repository.split('/')[0]}:{branch}"}
            )
            if pull["head"]["ref"] == branch
        ]
        if self.event and (self.event["branch"] != branch or not pulls):
            out["outcome"] = "ignored"
            out["notes"].append(
                f"The event is for {self.event['branch']}; this pass only merges the open "
                f"refresh pull request on {branch}, and the scheduled run handles the rest"
            )
            return
        remote = self.remote.fetch(self.root, branch)
        if len(pulls) > 1:
            return self.problem(
                out,
                issues,
                tag,
                base,
                "refused-multiple-prs",
                f"{len(pulls)} pull requests use {branch}",
            )
        if pulls:
            return self.pull_request(out, issues, tag, base, branch, pulls[0], remote)
        if remote is None:
            return self.fresh(out, issues, tag, base, branch, None)
        self.orphan(out, issues, tag, base, branch, remote)

    def refuse(
        self, out: dict, issues: list[dict], tag: str, base: str, reason: str, detail: str
    ) -> None:
        self.problem(
            out, issues, tag, base, reason if reason in REPORT_ONLY else f"refused-{reason}", detail
        )

    def verify(
        self, tag: str, head: str, remote: str | None, base: str, tree: str | None
    ) -> str | None:
        if remote != head:
            return "the branch tip is not the pull request head"
        ancestor = subprocess.run(
            ["git", "-C", str(self.root), "merge-base", "--is-ancestor", base, "HEAD"],
            capture_output=True,
            check=False,
        )
        if ancestor.returncode:
            return f"recorded base {base} is not an ancestor of main"
        parents = git(self.root, "rev-list", "--parents", "-n", "1", head).split()
        result = self.recomputed(tag, base)
        if result.status != "refreshed":
            return f"the refresh cannot be recomputed at {base}: {result.report.get('detail', '')}"
        actual = git(self.root, "rev-parse", f"{head}^{{tree}}")
        if actual != result.tree or (tree is not None and actual != tree):
            return "the head tree differs from the deterministic refresh"
        if parents[1:] != result.parents:
            return "the head parents differ from the deterministic refresh"
        return None

    def pull_request(
        self,
        out: dict,
        issues: list[dict],
        tag: str,
        base: str,
        branch: str,
        pull: dict,
        remote: str | None,
    ) -> None:
        number, head = pull["number"], pull["head"]["sha"]
        out["pull_request"] = number
        if self.event and self.event["head"] != head:
            out["outcome"] = "ignored"
            out["notes"].append(
                f"#{number} head is {head}, not the event's {self.event['head']}; the checks of "
                "its own head trigger their own pass"
            )
            return
        repository = self.api.repository
        if pull["state"] != "open":
            return self.refuse(out, issues, tag, base, "closed", f"#{number} is {pull['state']}")
        if (pull["head"].get("repo") or {}).get("full_name") != repository:
            return self.refuse(
                out, issues, tag, base, "foreign-head", f"#{number} head is not on the fork"
            )
        if not targets_main(self.api, pull):
            return self.refuse(
                out, issues, tag, base, "foreign-base", f"#{number} does not target main here"
            )
        if pull.get("draft"):
            return self.refuse(out, issues, tag, base, "draft", f"#{number} is a draft")
        if labels(pull) & HOLD_LABELS:
            return self.refuse(out, issues, tag, base, "held", f"#{number} carries a hold label")
        marker = parse_marker(pull.get("body"))
        if marker is None or marker["tag"] != tag:
            return self.refuse(
                out, issues, tag, base, "foreign-pr", f"#{number} has no valid controller marker"
            )
        claimed = claim(marker, head)
        if claimed is None:
            return self.refuse(
                out,
                issues,
                tag,
                base,
                "head-modified",
                f"#{number} head {head} was not pushed by the controller",
            )
        failure = self.verify(tag, head, remote, claimed["base"], claimed["tree"])
        if failure:
            return self.refuse(out, issues, tag, base, "head-unproven", f"#{number}: {failure}")
        canonical = {"base": claimed["base"], "head": head, "tag": tag, "tree": claimed["tree"]}
        if marker != canonical:
            out["actions"].append(
                {
                    "op": "update_pr",
                    "number": number,
                    "head": head,
                    "expect": marker,
                    "marker": canonical,
                }
            )
        if claimed["base"] != base:
            return self.stale(out, issues, tag, base, branch, number, canonical)
        state, reasons = merge_gate(self.api, number, head, base)
        if state == "failed":
            return self.problem(out, issues, tag, base, "checks-failed", "; ".join(reasons))
        if state == "ready":
            out["actions"].append(
                {"op": "merge_pr", "number": number, "head": head, "base": base, "tag": tag}
            )
            return self.finish(out, "merge", issues, tag)
        out["notes"].extend(reasons)
        self.finish(out, "waiting", issues, tag)

    def stale(
        self,
        out: dict,
        issues: list[dict],
        tag: str,
        base: str,
        branch: str,
        number: int,
        canonical: dict,
    ) -> None:
        out["notes"].append(f"#{number} was prepared on {canonical['base']}; refreshing again")
        if self.event:
            out["outcome"] = "waiting"
            out["notes"].append("An event never refreshes; the scheduled run does")
            return
        if self.known_failure(issues, tag, base):
            out["outcome"] = "skipped"
            out["notes"].append("An unchanged failed input is skipped; set retry to re-attempt")
            return
        result = self.refresher(self.root, tag, base, self.scratch, self.bundle)
        if result.status != "refreshed":
            return self.problem(
                out, issues, tag, base, result.status, result.report.get("detail", "")
            )
        pending = {**canonical, "pending": {"base": base, "head": result.head, "tree": result.tree}}
        final = {"base": base, "head": result.head, "tag": tag, "tree": result.tree}
        out["actions"] += [
            {
                "op": "update_pr",
                "number": number,
                "head": canonical["head"],
                "expect": canonical,
                "marker": pending,
            },
            self.push_op(branch, base, result, canonical["head"]),
            {
                "op": "update_pr",
                "number": number,
                "head": result.head,
                "expect": pending,
                "marker": final,
            },
        ]
        self.finish(out, "refreshed", issues, tag)

    def push_op(self, branch: str, base: str, result: Result, expect: str | None) -> dict:
        return {
            "op": "push_branch",
            "branch": branch,
            "base": base,
            "head": result.head,
            "tree": result.tree,
            "parents": result.parents,
            "expect": expect,
        }

    def fresh(
        self,
        out: dict,
        issues: list[dict],
        tag: str,
        base: str,
        branch: str,
        expect: str | None,
    ) -> None:
        if self.known_failure(issues, tag, base):
            out["outcome"] = "skipped"
            out["notes"].append("An unchanged failed input is skipped; set retry to re-attempt")
            return
        result = self.refresher(self.root, tag, base, self.scratch, self.bundle)
        if result.status != "refreshed":
            return self.problem(
                out, issues, tag, base, result.status, result.report.get("detail", "")
            )
        out["actions"].append(self.push_op(branch, base, result, expect))
        out["actions"].append(self.open_op(tag, branch, base, result.head, result.tree))
        self.finish(out, "refreshed", issues, tag)

    def open_op(self, tag: str, branch: str, base: str, head: str | None, tree: str | None) -> dict:
        if not head or not tree:
            raise ControllerError("A refreshed result lacks its head or tree")
        return {
            "op": "open_pr",
            "tag": tag,
            "branch": branch,
            "base": base,
            "head": head,
            "tree": tree,
            "title": pull_title(tag),
            "body": pull_body(tag, base, head, tree, self.run_url),
        }

    def orphan(
        self, out: dict, issues: list[dict], tag: str, base: str, branch: str, remote: str
    ) -> None:
        parents = git(self.root, "rev-list", "--parents", "-n", "1", remote).split()[1:]
        failure = "the branch is not a two-parent refresh commit"
        if len(parents) == 2:
            failure = self.verify(tag, remote, remote, parents[0], None) or ""
        if failure:
            return self.refuse(
                out, issues, tag, base, "branch-collision", f"{branch} at {remote}: {failure}"
            )
        if parents[0] == base:
            tree = git(self.root, "rev-parse", f"{remote}^{{tree}}")
            out["actions"].append(self.open_op(tag, branch, base, remote, tree))
            return self.finish(out, "refreshed", issues, tag)
        self.fresh(out, issues, tag, base, branch, remote)


APP_PERMISSIONS = {
    "contents": "write",
    "pull_requests": "write",
    "issues": "write",
    "checks": "read",
    "statuses": "read",
    "actions": "read",
}
LEVELS = {"read": 1, "write": 2, "admin": 3}
API_ROOT = "https://api.github.com"


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def openssl_sign(key_pem: str, message: bytes) -> bytes:
    with tempfile.TemporaryDirectory() as scratch:
        key = Path(scratch) / "key.pem"
        key.touch(mode=0o600)
        key.write_text(key_pem if key_pem.endswith("\n") else key_pem + "\n")
        try:
            done = subprocess.run(
                ["openssl", "dgst", "-sha256", "-sign", str(key)],
                input=message,
                capture_output=True,
                check=False,
            )
        except OSError as error:
            raise ControllerError(f"openssl could not run: {type(error).__name__}") from error
    if done.returncode or not done.stdout:
        raise ControllerError("the private key could not sign a request (is it a PEM RSA key?)")
    return done.stdout


def app_jwt(
    app_id: str,
    key_pem: str,
    sign: Callable[[str, bytes], bytes] = openssl_sign,
    now: float | None = None,
) -> str:
    issued = int(time.time() if now is None else now)
    head = b64url(json.dumps({"alg": "RS256", "typ": "JWT"}).encode())
    claims = b64url(json.dumps({"iat": issued - 60, "exp": issued + 540, "iss": app_id}).encode())
    signing = f"{head}.{claims}"
    return f"{signing}.{b64url(sign(key_pem, signing.encode()))}"


class AppApi:
    """GitHub App reads. The only POST is the credential exchange that authenticates."""

    def request(
        self, method: str, path: str, credential: str, jwt: bool, body: dict | None = None
    ) -> Any:
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "Authorization": ("Bearer " if jwt else "token ") + credential,
        }
        data = json.dumps(body).encode() if body is not None else None
        if data:
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(API_ROOT + path, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                text = response.read().decode()
            return json.loads(text) if text else None
        except urllib.error.HTTPError as error:
            raise ControllerError(f"{method} {path} answered {error.code}") from error
        except TRANSPORT as error:
            raise ControllerError(f"{method} {path} failed: {type(error).__name__}") from error

    def get(self, path: str, credential: str, jwt: bool = False) -> Any:
        return self.request("GET", path, credential, jwt)

    def exchange(self, installation: int, jwt_credential: str) -> dict:
        """Authenticate as the installation with a read-only token on all its repositories."""
        return self.request(
            "POST",
            f"/app/installations/{installation}/access_tokens",
            jwt_credential,
            True,
            {"permissions": {"issues": "read"}},
        )

    def revoke(self, token: str) -> None:
        self.request("DELETE", "/installation/token", token, False)


def permission_gaps(granted: dict, wanted: dict = APP_PERMISSIONS) -> tuple[list[str], list[str]]:
    missing = [
        f"{name}:{level}"
        for name, level in wanted.items()
        if LEVELS.get(granted.get(name, ""), 0) < LEVELS[level]
    ]
    extra = [
        f"{name}:{level}"
        for name, level in sorted(granted.items())
        if name != "metadata" and name not in wanted
    ] + [
        f"{name}:{granted[name]}"
        for name, level in wanted.items()
        if LEVELS.get(granted.get(name, ""), 0) > LEVELS[level]
    ]
    return missing, extra


def readiness(
    api: AppApi,
    repository: str,
    app_id: str,
    key: str,
    slug: str,
    sign: Callable[[str, bytes], bytes] = openssl_sign,
    mask: Callable[[str], None] = lambda _: None,
) -> dict:
    """Read-only proof that the configured App can publish for this repository.

    The App JWT reads the exact installation permissions; the installation token is
    used only for GETs of its own scope and is revoked afterwards. Exact permissions come
    from the App JWT, never from a downscoped token.
    """
    owner, _, name = repository.partition("/")
    checks: list[dict] = []
    warnings: list[str] = []

    def record(item: str, ok: bool, detail: str) -> bool:
        checks.append({"check": item, "ok": ok, "detail": detail})
        return ok

    configured = [
        ("secret BENCHBOX_UPSTREAM_APP_ID", bool(app_id)),
        ("secret BENCHBOX_UPSTREAM_APP_PRIVATE_KEY", bool(key)),
        ("variable BENCHBOX_UPSTREAM_APP_SLUG", bool(slug)),
    ]
    absent = [label for label, present in configured if not present]
    if absent:
        record("configuration", False, "not configured: " + ", ".join(absent))
        return {"ready": False, "state": "missing", "checks": checks, "warnings": warnings}
    record("configuration", True, "App id, private key and slug are configured")
    token = ""
    try:
        credential = app_jwt(app_id, key, sign)
        app = api.get("/app", credential, True)
        record("authentication", True, f"authenticated as {app.get('slug')!r} (id {app.get('id')})")
        if str(app.get("id")) != app_id.strip():
            record(
                "app identity", False, f"the key authenticates App id {app.get('id')}, not {app_id}"
            )
        elif app.get("slug") != slug:
            record(
                "app identity",
                False,
                f"the App slug is {app.get('slug')!r}, not the configured {slug!r}",
            )
        else:
            record("app identity", True, "the App id and slug match the configuration")
        installation = api.get(f"/repos/{repository}/installation", credential, True)
        owner_ok = (installation.get("account") or {}).get("login") == owner
        record(
            "installation",
            owner_ok
            and installation.get("app_id") == app.get("id")
            and not installation.get("suspended_at"),
            f"installation {installation.get('id')} on {(installation.get('account') or {}).get('login')!r}"
            + (", suspended" if installation.get("suspended_at") else ""),
        )
        missing, extra = permission_gaps(installation.get("permissions") or {})
        record(
            "installation permissions",
            not missing,
            "missing " + ", ".join(missing)
            if missing
            else "the installation grants the required set",
        )
        if extra:
            warnings.append(
                "the installation grants more than the controller uses: " + ", ".join(extra)
            )
        token = api.exchange(installation["id"], credential)["token"]
        mask(token)
        record("installation token", True, "authenticated as the installation (read-only token)")
        listing = api.get("/installation/repositories?per_page=100", token)
        repositories = [item["full_name"] for item in listing.get("repositories", [])]
        total = listing.get("total_count", len(repositories))
        scoped = repositories == [repository] and total == 1
        record(
            "repository scope",
            scoped,
            "the installation covers only this repository"
            if scoped
            else f"the installation covers {total} repositories: {', '.join(sorted(repositories)) or 'none'}",
        )
        details = api.get(f"/repos/{repository}", token)
        issues_on = bool(details.get("has_issues"))
        record(
            "fork issues",
            issues_on and not details.get("archived") and not details.get("disabled"),
            "Issues are enabled" if issues_on else "Issues are disabled on the repository",
        )
        if details.get("full_name") != repository or name != details.get("name"):
            record("repository", False, f"the API names {details.get('full_name')!r}")
    except (ControllerError, KeyError, TypeError, AttributeError) as error:
        record("probe", False, f"{type(error).__name__}: {error}")
    finally:
        if token:
            try:
                api.revoke(token)
            except ControllerError as error:
                warnings.append(
                    f"the temporary token was not revoked ({error}); it expires in an hour"
                )
    ready = all(item["ok"] for item in checks)
    return {
        "ready": ready,
        "state": "ready" if ready else "failed",
        "checks": checks,
        "warnings": warnings,
    }


def readiness_summary(result: dict, mode: str) -> str:
    verdict = {
        "ready": "ready: the App can publish",
        "missing": "NOT CONFIGURED: the controller cannot publish until configured",
        "failed": "NOT READY: the controller cannot publish",
    }[result["state"]]
    lines = [
        "## BenchBox upstream App readiness",
        "",
        f"- mode: `{mode}`",
        f"- result: **{verdict}**",
    ]
    if mode != "enabled" and result["state"] != "ready":
        lines.append("- this run mutates nothing, so the result is informational")
    for item in result["checks"]:
        lines.append(f"- {'ok' if item['ok'] else 'FAIL'} {item['check']}: {item['detail']}")
    for warning in result["warnings"]:
        lines.append(f"- warning: {warning}")
    return "\n".join(lines) + "\n"


def summary(plan: dict, mode: str) -> str:
    lines = [
        "## BenchBox upstream controller",
        "",
        f"- mode: `{mode}`",
        f"- outcome: `{plan.get('outcome')}`",
        f"- tag: `{plan.get('tag') or plan.get('latest')}` on main `{plan.get('base')}`",
    ]
    for note in plan.get("notes", []):
        lines.append(f"- note: {note}")
    if plan.get("detail"):
        lines.append(f"- detail: {plan['detail'][:500]}")
    for action in plan.get("actions", []):
        label = {k: v for k, v in action.items() if k in ("op", "number", "branch", "head", "tag")}
        lines.append(f"- {'planned' if mode != 'enabled' else 'to apply'}: `{json.dumps(label)}`")
    return "\n".join(lines) + "\n"


def run_mode(enabled: bool, dry_run: bool) -> str:
    return "disabled" if not enabled else "dry-run" if dry_run else "enabled"


def push_branch(controller: Controller, plan: dict, op: dict, bundle: Path) -> dict:
    root, remote = controller.root, controller.remote
    branch, head = op["branch"], op["head"]
    live = remote.head(root, branch)
    if live == head:
        return {"status": "unchanged"}
    if live != op["expect"]:
        raise Refused(f"{branch} is at {live}, not the recorded {op['expect']}; leaving it alone")
    if not bundle.is_file():
        raise Refused("The prepared bundle is missing")
    fetched = subprocess.run(
        [
            "git",
            "-C",
            str(root),
            "fetch",
            "--no-tags",
            str(bundle),
            f"+refs/heads/{branch}:refs/controller/bundle/{branch}",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if fetched.returncode:
        raise Refused(f"The prepared bundle cannot be read: {fetched.stderr.strip()[-300:]}")
    if git(root, "rev-parse", f"refs/controller/bundle/{branch}") != head:
        raise Refused("The bundle does not contain the planned head")
    parents = git(root, "rev-list", "--parents", "-n", "1", head).split()[1:]
    if parents != op["parents"] or parents[0] != plan["base"]:
        raise Refused("The bundle head is not a merge of upstream onto the planned main")
    if git(root, "rev-parse", f"{head}^{{tree}}") != op["tree"]:
        raise Refused("The bundle head tree is not the planned tree")
    if git(root, "diff", "--name-only", plan["base"], head, "--", ".github/workflows"):
        raise Refused("The bundle changes workflows, which the controller never publishes")
    remote.push(root, branch, head, op["expect"])
    return {"status": "pushed", "head": head}


def pulls_for(api: Client, branch: str) -> list[dict]:
    owner = api.repository.split("/")[0]
    return [
        pull
        for pull in api.lists("pulls", {"state": "all", "head": f"{owner}:{branch}"})
        if pull["head"]["ref"] == branch
    ]


def open_pr(controller: Controller, op: dict) -> dict:
    api = controller.api
    existing = pulls_for(api, op["branch"])
    for pull in existing:
        marker = parse_marker(pull.get("body"))
        if pull["state"] == "open" and marker and marker["head"] == op["head"]:
            return {"status": "unchanged", "number": pull["number"]}
    if existing:
        raise Refused(f"A pull request already uses {op['branch']}; leaving it alone")
    live = controller.remote.head(controller.root, op["branch"])
    if live != op["head"]:
        raise Refused(
            f"{op['branch']} moved to {live}, not the planned {op['head']}; leaving it alone"
        )
    created = api.send(
        "POST",
        "pulls",
        {
            "title": op["title"],
            "head": op["branch"],
            "base": "main",
            "body": op["body"],
            "draft": False,
        },
    )
    return {"status": "opened", "number": created["number"]}


def update_pr(api: Client, op: dict) -> dict:
    pull = api.get(f"pulls/{op['number']}")
    if pull["state"] != "open" or pull.get("draft") or labels(pull) & HOLD_LABELS:
        raise Refused(f"#{op['number']} is not open, is draft or is held")
    if not targets_main(api, pull):
        raise Refused(f"#{op['number']} does not target main of this repository")
    if pull["head"]["sha"] != op["head"]:
        raise Refused(f"#{op['number']} head moved to {pull['head']['sha']}; leaving it alone")
    current = parse_marker(pull.get("body"))
    if current == op["marker"]:
        return {"status": "unchanged"}
    if current != op["expect"]:
        raise Refused(f"#{op['number']} marker changed since the plan")
    api.send(
        "PATCH",
        f"pulls/{op['number']}",
        {"body": replace_marker(pull.get("body"), op["marker"])},
    )
    return {"status": "updated"}


def merge_pr(api: Client, op: dict) -> dict:
    pull = api.get(f"pulls/{op['number']}")
    if pull.get("merged"):
        return {"status": "unchanged"}
    marker = parse_marker(pull.get("body"))
    if marker is None or claim(marker, op["head"]) is None or pull["head"]["sha"] != op["head"]:
        raise Refused(f"#{op['number']} is no longer the proven controller head")
    state, reasons = merge_gate(api, op["number"], op["head"], op["base"])
    if state != "ready":
        return {"status": state, "reasons": reasons}
    api.send(
        "PUT",
        f"pulls/{op['number']}/merge",
        {
            "merge_method": "merge",
            "sha": op["head"],
            "commit_title": f"chore: merge upstream refresh {op['tag']}",
        },
    )
    return {"status": "merged"}


def upsert_issue(controller: Controller, op: dict) -> dict:
    api = controller.api
    existing = [i for i in controller.controller_issues() if i["key"]["tag"] == op["tag"]]
    if not existing:
        created = api.send("POST", "issues", {"title": op["title"], "body": op["body"]})
        return {"status": "opened", "number": created["number"]}
    issue = existing[0]
    if issue["body"] == op["body"]:
        return {"status": "unchanged", "number": issue["number"]}
    api.send("PATCH", f"issues/{issue['number']}", {"title": op["title"], "body": op["body"]})
    return {"status": "updated", "number": issue["number"]}


def close_issues(controller: Controller, op: dict) -> dict:
    closed = []
    for issue in controller.controller_issues():
        key = issue["key"]
        kept = key["outcome"] in PUBLICATION_FAILURES and key["base"] == op.get(
            "keep_publication_base"
        )
        if (op["tag"] is None or key["tag"] == op["tag"]) and not kept:
            controller.api.send(
                "PATCH",
                f"issues/{issue['number']}",
                {"state": "closed", "state_reason": "completed"},
            )
            closed.append(issue["number"])
    return {"status": "closed" if closed else "unchanged", "numbers": closed}


def report_publication(
    controller: Controller, plan: dict, outcome: str, detail: str, result: dict
) -> None:
    tag = plan.get("tag")
    if not tag or plan.get("event"):
        return
    key = {"base": plan["base"], "outcome": outcome, "tag": tag}
    op = {
        "op": "upsert_issue",
        "tag": tag,
        "key": key,
        "title": issue_title(tag),
        "body": issue_body(tag, plan["base"], outcome, detail, plan.get("run_url", "")),
    }
    try:
        existing = [i for i in controller.controller_issues() if i["key"]["tag"] == tag]
        if existing and existing[0]["key"] == key and not controller.retry:
            result["issue"] = "unchanged failed input; no issue update"
            return
        result["issue"] = upsert_issue(controller, op)
    except (ControllerError, DeliveryError) as error:
        result["issue_error"] = f"The failure issue could not be published: {error}"


def apply(controller: Controller, plan: dict, mode: str, bundle: Path) -> dict:
    result: dict = {"mode": mode, "performed": [], "unapplied": []}
    if mode != "enabled":
        result["unapplied"] = plan["actions"]
        return result
    if plan.get("schema") != SCHEMA or plan.get("repository") != controller.api.repository:
        raise ControllerError("The plan is not for this repository or schema")
    live = controller.api.get("branches/main")["commit"]["sha"]
    head = git(controller.root, "rev-parse", "HEAD")
    if not (live == head == plan["base"]):
        result["halted"] = f"main is {live}, the plan was made on {plan['base']}"
        result["unapplied"] = plan["actions"]
        return result
    api = controller.api
    for index, op in enumerate(plan["actions"]):
        try:
            if op["op"] == "push_branch":
                outcome = push_branch(controller, plan, op, bundle)
            elif op["op"] == "open_pr":
                outcome = open_pr(controller, op)
            elif op["op"] == "update_pr":
                outcome = update_pr(api, op)
            elif op["op"] == "merge_pr":
                outcome = merge_pr(api, op)
            elif op["op"] == "upsert_issue":
                outcome = upsert_issue(controller, op)
            elif op["op"] == "close_issues":
                outcome = close_issues(controller, op)
            else:
                raise Refused(f"Unknown action {op['op']}")
        except (Refused, ControllerError, DeliveryError) as error:
            result["halted"] = str(error)
            result["unapplied"] = plan["actions"][index:]
            outcome_name = "publish-refused" if isinstance(error, Refused) else "publish-failed"
            report_publication(controller, plan, outcome_name, str(error), result)
            return result
        result["performed"].append({"op": op["op"], **outcome})
        if op["op"] == "merge_pr" and outcome["status"] not in ("merged", "unchanged"):
            result["deferred"] = f"#{op['number']} was not merged ({outcome['status']})"
            result["unapplied"] = plan["actions"][index + 1 :]
            return result
    return result


def write_output(name: str, value: str) -> None:
    target = os.environ.get("GITHUB_OUTPUT")
    if target:
        with open(target, "a", encoding="utf-8") as handle:
            handle.write(f"{name}={value}\n")


def write_summary(text: str) -> None:
    target = os.environ.get("GITHUB_STEP_SUMMARY")
    if target:
        with open(target, "a", encoding="utf-8") as handle:
            handle.write(text)


def truthy(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() == "true"


def build(args: argparse.Namespace, token: str) -> Controller:
    out_dir = args.out_dir.resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    return Controller(
        Client(token, args.repository),
        ROOT,
        Remote(f"https://github.com/{args.repository}.git", token),
        out_dir / "scratch",
        out_dir / BUNDLE_FILE,
        run_url=args.run_url,
        retry=truthy("RETRY"),
        app_slug=os.environ.get("BENCHBOX_UPSTREAM_APP_SLUG", "").strip(),
        event=event_from_env(),
    )


def check_readiness(args: argparse.Namespace) -> None:
    mode = run_mode(truthy("ENABLED"), truthy("DRY_RUN"))
    mask = (
        (lambda value: print(f"::add-mask::{value}"))
        if os.environ.get("GITHUB_ACTIONS")
        else (lambda _: None)
    )
    result = readiness(
        AppApi(),
        args.repository,
        os.environ.get("APP_ID", "").strip(),
        os.environ.get("APP_PRIVATE_KEY", ""),
        os.environ.get("BENCHBOX_UPSTREAM_APP_SLUG", "").strip(),
        mask=mask,
    )
    text = readiness_summary(result, mode)
    write_summary(text)
    write_output("ready", "true" if result["ready"] else "false")
    print(text)
    if mode == "enabled" and not result["ready"]:
        sys.exit("The GitHub App is not ready; refusing to publish before any mutation")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["prepare", "publish", "readiness"])
    parser.add_argument("--out-dir", type=Path)
    parser.add_argument("--repository", default=REPOSITORY)
    parser.add_argument("--run-url", default="")
    args = parser.parse_args()
    if args.action == "readiness":
        return check_readiness(args)
    if args.out_dir is None:
        parser.error("--out-dir is required for prepare and publish")
    token = os.environ.get("GH_TOKEN", "")
    if not token:
        sys.exit("GH_TOKEN is required")
    mode = run_mode(truthy("ENABLED"), truthy("DRY_RUN"))
    controller = build(args, token)
    if mode == "enabled" and not controller.app_slug:
        sys.exit("BENCHBOX_UPSTREAM_APP_SLUG is required to identify the controller's issues")
    state = args.out_dir.resolve() / STATE_FILE
    if args.action == "prepare":
        plan = controller.plan()
        plan["mode"] = mode
        state.write_text(json.dumps(plan, indent=2) + "\n")
        write_summary(summary(plan, mode))
        write_output("mutate", "true" if mode == "enabled" and plan["actions"] else "false")
        print(json.dumps(plan, indent=2))
        if plan.get("fail"):
            sys.exit(f"Preparing the plan failed: {plan.get('detail')}")
        return
    plan = json.loads(state.read_text())
    result = apply(controller, plan, mode, controller.bundle)
    (args.out_dir / RESULT_FILE).write_text(json.dumps(result, indent=2) + "\n")
    write_summary("## Applied\n\n```json\n" + json.dumps(result, indent=2) + "\n```\n")
    print(json.dumps(result, indent=2))
    if result.get("issue_error"):
        sys.exit(f"{result['halted']}\n{result['issue_error']}")
    if result.get("halted"):
        sys.exit(1)


if __name__ == "__main__":
    main()
