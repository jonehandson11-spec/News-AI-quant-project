"""Run FT collection locally and publish only its inbox batch using a deploy key."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from scripts import ft_sync
from scripts.dataset import read_json, write_json

REPOSITORY = "jonehandson11-spec/News-AI-quant-project"
ORIGIN = "https://github.com/" + REPOSITORY + ".git"
API = "https://api.github.com/repos/" + REPOSITORY


class AutorunError(ValueError):
    """Only fixed, non-sensitive reason codes are raised by this module."""


@dataclass
class Settings:
    root: Path
    state_dir: Path
    cookie_file: Path
    ssh_key: Path
    known_hosts: Path
    ssh_executable: Path | None = None
    ssh_host: str = "github.com"
    ssh_port: int = 22
    lookback_hours: int = 48
    max_new: int = 100
    wait_seconds: int = 300
    poll_seconds: int = 30
    check: bool = False
    resume_only: bool = False


def utcnow():
    return datetime.now(timezone.utc)


@contextmanager
def process_lock(state: Path):
    """The file may remain; the operating system releases its lock on process exit."""
    state.mkdir(parents=True, exist_ok=True)
    handle = (state / "autorun.lock").open("a+b")
    locked = False
    try:
        if handle.seek(0, os.SEEK_END) == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            locked = True
        except OSError:
            raise AutorunError("already_running") from None
        yield
    finally:
        if locked:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


def _ssh_executable(settings):
    if settings.ssh_executable is not None:
        path = settings.ssh_executable.resolve()
    else:
        windows = Path(os.environ.get("WINDIR", "C:/Windows")) / "System32/OpenSSH/ssh.exe"
        detected = shutil.which("ssh")
        path = windows if windows.is_file() else Path(detected) if detected else None
    if path is None or not path.is_file():
        raise AutorunError("ssh_unavailable")
    return path.resolve()


def ssh_command(settings):
    if (settings.ssh_host, settings.ssh_port) not in (("github.com", 22), ("ssh.github.com", 443)):
        raise AutorunError("invalid_ssh_destination")
    if any(character.isspace() for character in str(settings.known_hosts.resolve())):
        raise AutorunError("known_hosts_path_has_whitespace")
    arguments = [_ssh_executable(settings).as_posix(), "-F", "NUL" if os.name == "nt" else "/dev/null",
                 "-i", settings.ssh_key.resolve().as_posix(), "-p", str(settings.ssh_port),
                 "-o", "BatchMode=yes", "-o", "IdentitiesOnly=yes", "-o", "IdentityAgent=none",
                 "-o", "PasswordAuthentication=no", "-o", "PreferredAuthentications=publickey",
                 "-o", "StrictHostKeyChecking=yes", "-o", "ConnectTimeout=15",
                 "-o", "UserKnownHostsFile=" + settings.known_hosts.resolve().as_posix()]
    # Git parses GIT_SSH_COMMAND with its shell, including Git for Windows.
    return shlex.join(arguments)


def validate_settings(settings):
    settings.root = ft_sync._repository(settings.root)
    for field in ("state_dir", "cookie_file", "ssh_key", "known_hosts"):
        setattr(settings, field, ft_sync._outside(settings.root, getattr(settings, field), field + "_must_be_external"))
    for field in ("ssh_key", "known_hosts"):
        if not getattr(settings, field).is_file():
            raise AutorunError(field + "_missing")
    if not settings.resume_only and not settings.cookie_file.is_file():
        raise AutorunError("cookie_file_missing")
    origin = ft_sync._git(settings.root, "remote", "get-url", "origin").stdout.decode().strip()
    if origin not in (ORIGIN, ORIGIN.removesuffix(".git")):
        raise AutorunError("unexpected_origin")
    if settings.lookback_hours not in (48, 120) or not 1 <= settings.max_new <= 100:
        raise AutorunError("invalid_collection_limits")
    if not 0 <= settings.wait_seconds <= 3600 or not 5 <= settings.poll_seconds <= 300:
        raise AutorunError("invalid_wait_settings")
    ssh_command(settings)


def _git(settings, *arguments, input_bytes=None, index=None, ssh=False, reason="git_operation_failed", allowed=(0,)):
    environment = dict(os.environ)
    for key in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE"):
        environment.pop(key, None)
    environment.update(GIT_TERMINAL_PROMPT="0", GCM_INTERACTIVE="Never", GIT_ASKPASS="", SSH_ASKPASS="",
                       GIT_AUTHOR_NAME="FT local synchronizer", GIT_AUTHOR_EMAIL="ft-local@users.noreply.github.com",
                       GIT_COMMITTER_NAME="FT local synchronizer", GIT_COMMITTER_EMAIL="ft-local@users.noreply.github.com")
    if index is not None:
        environment["GIT_INDEX_FILE"] = str(index)
    if ssh:
        environment.update(GIT_SSH_COMMAND=ssh_command(settings), GIT_SSH_VARIANT="ssh")
    try:
        result = subprocess.run([ft_sync._git_executable(), "-c", "credential.helper=", "-c", "core.askPass=",
                                 "-C", str(settings.root), *arguments], input=input_bytes, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, env=environment, shell=False, timeout=120, check=False)
    except (OSError, subprocess.SubprocessError):
        raise AutorunError(reason) from None
    if result.returncode not in allowed:
        raise AutorunError(reason)
    return result


def _object(result):
    value = result.stdout.decode("ascii").strip()
    if not ft_sync.SHA.fullmatch(value):
        raise AutorunError("invalid_git_object")
    return value


def build_commit(settings, plan):
    """Use a private index; neither the user's checkout nor its index is changed."""
    ft_sync._load_plan(settings.root, Path(plan["plan_file"]))
    file = plan["publish_files"][0]
    if file["path"] != ft_sync.PUBLISH_PATH or plan["branch_name"] != ft_sync.INBOX_BRANCH:
        raise AutorunError("invalid_publish_destination")
    content = Path(file["local_path"]).read_bytes()
    blob = _object(_git(settings, "hash-object", "-w", "--stdin", input_bytes=content))
    if blob != file["blob_sha"]:
        raise AutorunError("batch_changed")
    with tempfile.TemporaryDirectory(prefix="git-index-", dir=settings.state_dir) as temporary:
        index = Path(temporary) / "index"
        _git(settings, "read-tree", plan["base_tree_sha"], index=index)
        _git(settings, "update-index", "--add", "--cacheinfo", "100644," + blob + "," + ft_sync.PUBLISH_PATH, index=index)
        tree = _object(_git(settings, "write-tree", index=index))
    changed = _git(settings, "diff-tree", "--no-commit-id", "--name-only", "-r", plan["base_tree_sha"], tree).stdout.decode().splitlines()
    if changed != [ft_sync.PUBLISH_PATH]:
        raise AutorunError("unexpected_tree_changes")
    return _object(_git(settings, "commit-tree", tree, "-p", plan["parent_sha"],
                        input_bytes=("data: submit local FT batch " + plan["batch_id"] + "\n").encode()))


def push_commit(settings, commit):
    if not ft_sync.SHA.fullmatch(commit):
        raise AutorunError("invalid_commit_sha")
    destination = "git@" + settings.ssh_host + ":" + REPOSITORY + ".git"
    # No force flag, '+' refspec, configurable branch, token, or credential helper.
    _git(settings, "push", "--porcelain", destination, commit + ":refs/heads/" + ft_sync.INBOX_BRANCH,
         ssh=True, reason="push_not_confirmed")


def workflow_status(commit):
    request = Request(API + "/actions/workflows/import-ft.yml/runs?head_sha=" + commit + "&per_page=10",
                      headers={"Accept": "application/vnd.github+json", "User-Agent": "FTLocalSynchronizer/1.0"})
    try:
        with urlopen(request, timeout=20) as response:
            payload = json.load(response)
        runs = [run for run in payload.get("workflow_runs", []) if run.get("head_sha") == commit]
        if not runs:
            return {"state": "unknown"}
        run = max(runs, key=lambda value: (value.get("id", 0), value.get("run_attempt", 0)))
        if run.get("status") != "completed":
            return {"state": "running", "run_id": run.get("id")}
        conclusion = run.get("conclusion")
        return {"state": "success" if conclusion == "success" else "failed", "run_id": run.get("id")}
    except (HTTPError, URLError, OSError, ValueError, TypeError):
        # A 404, outage, or unauthenticated API rate limit is not evidence of no push.
        return {"state": "unknown"}


def _receipt_at(root, batch_id, main):
    manifest = json.loads(ft_sync._git(root, "show", main + ":data/manifest.json").stdout)
    report = json.loads(ft_sync._git(root, "show", main + ":data/source_reports/ft.json").stdout)
    latest = manifest.get("latest_run") or {}
    local = latest.get("local_batch") or {}
    source = report.get("latest_run") or {}
    if batch_id == local.get("batch_id"):
        inserted = local.get("inserted", latest.get("inserted", 0))
    elif batch_id == source.get("batch_id"):
        inserted = source.get("counts", {}).get("inserted", 0)
    else:
        return None
    return {"main_commit_sha": main, "inserted": inserted if type(inserted) is int else 0,
            "total_articles": manifest["total_articles"],
            "ft_articles": manifest["sources"]["Financial Times"]["article_count"]}


def _main_receipt(root, batch_id, main, since=None):
    receipt = _receipt_at(root, batch_id, main)
    if receipt or since == main:
        return receipt
    if since is None:
        return None
    ancestry = ft_sync._git(root, "merge-base", "--is-ancestor", since, main, allowed=(0, 1, 128))
    if ancestry.returncode != 0:
        raise AutorunError("main_history_changed")
    commits = ft_sync._git(root, "rev-list", "--max-count=101", main, "^" + since, "--",
                           "data/source_reports/ft.json", "data/manifest.json").stdout.decode().splitlines()
    for commit in commits[:100]:
        if not ft_sync.SHA.fullmatch(commit):
            raise AutorunError("invalid_git_object")
        if commit != main:
            receipt = _receipt_at(root, batch_id, commit)
            if receipt:
                return receipt
    if len(commits) > 100:
        raise AutorunError("receipt_history_limit_reached")
    return None


def _uploaded_commit(root, plan, record, base):
    inbox = base.get("expected_inbox_sha")
    if not inbox:
        return None
    saved = record.get("inbox_commit_sha") if record.get("batch_id") == plan["batch_id"] else None
    if saved and ft_sync.SHA.fullmatch(saved):
        result = ft_sync._git(root, "merge-base", "--is-ancestor", saved, inbox, allowed=(0, 1, 128))
        if result.returncode == 0:
            return saved
    # This also recognises a batch uploaded by a connector before installation.
    commits = ft_sync._git(root, "rev-list", "--max-count=30", inbox, "--", ft_sync.PUBLISH_PATH).stdout.decode().splitlines()
    for commit in commits:
        if not ft_sync.SHA.fullmatch(commit):
            raise AutorunError("invalid_git_object")
        result = ft_sync._git(root, "rev-parse", commit + ":" + ft_sync.PUBLISH_PATH, allowed=(0, 128))
        if result.returncode == 0 and result.stdout.decode().strip() == plan["publish_files"][0]["blob_sha"]:
            return commit
    return None


def _load_state(settings):
    path = settings.state_dir / "autorun.json"
    value = read_json(path) if path.exists() else {"format_version": 1, "repository": REPOSITORY}
    if value.get("format_version") != 1 or value.get("repository") != REPOSITORY:
        raise AutorunError("invalid_autorun_state")
    return value


def _save_state(settings, state):
    write_json(settings.state_dir / "autorun.json", state)


def _cooldown(settings, state, plan, now):
    if state.get("cooldown_batch_id") == plan["batch_id"]:
        return
    batch = read_json(Path(plan["batch_file"]))
    report = batch.get("report", {})
    if report.get("reason") != "rate_limited":
        return
    seconds = report.get("retry_after_seconds", 3600)
    seconds = max(3600, seconds) if type(seconds) is int and seconds >= 0 else 3600
    observed_at = batch.get("finished_at") or batch.get("started_at") or plan.get("last_attempt_at")
    if observed_at:
        try:
            observed = datetime.fromisoformat(observed_at.replace("Z", "+00:00"))
            if observed.tzinfo is None:
                raise ValueError
        except (TypeError, ValueError):
            raise AutorunError("invalid_cooldown_time") from None
    else:
        observed = now
    try:
        until = observed + timedelta(seconds=seconds)
    except OverflowError:
        until = datetime.max.replace(tzinfo=timezone.utc)
    previous = state.get("cooldown_until")
    if previous:
        until = max(until, datetime.fromisoformat(previous))
    state.update(cooldown_until=until.isoformat(), cooldown_batch_id=plan["batch_id"])
    _save_state(settings, state)


def _inherit_cooldown(settings, state):
    attempt_file = settings.state_dir / "last_attempt.json"
    if attempt_file.is_file():
        attempt = read_json(attempt_file)
        if Path(attempt.get("repository_root", "")).resolve() != settings.root:
            raise AutorunError("state_belongs_to_another_repository")
        plan_file = Path(attempt.get("plan_file", ""))
        if plan_file.is_file():
            _, plan = ft_sync._load_plan(settings.root, plan_file)
            _cooldown(settings, state, plan, utcnow())


def _pending(settings):
    path = settings.state_dir / "last_attempt.json"
    if not path.is_file():
        return None
    attempt = read_json(path)
    if Path(attempt.get("repository_root", "")).resolve() != settings.root:
        raise AutorunError("state_belongs_to_another_repository")
    plan_file = Path(attempt.get("plan_file", "")).resolve()
    if not plan_file.is_relative_to(settings.state_dir) or plan_file.parent == settings.state_dir:
        raise AutorunError("invalid_plan_location")
    if not plan_file.is_file():
        if (plan_file.parent / "ft_batch.json").is_file():
            return _recover_plan(settings, attempt, plan_file)
        return None
    _, plan = ft_sync._load_plan(settings.root, plan_file)
    return plan if not plan.get("receipt_file") else None


def _recover_plan(settings, attempt, plan_file):
    """Finish an interrupted prepare using its existing batch, with no FT request."""
    batch_file = plan_file.parent / "ft_batch.json"
    snapshot = ft_sync._snapshot(settings.root, plan_file.parent)
    merged = ft_sync._merge_batch(Path(snapshot["staging_root"]), batch_file, now=utcnow())
    batch = read_json(batch_file)
    file = ft_sync._batch_file(batch_file)
    collected = {**batch.get("report", {}), "collected": len(batch["rows"]),
                 "snapshot_total": batch.get("snapshot_total", 0), "execution_location": "local"}
    lookback = batch.get("lookback_hours", 48)
    plan = {"format_version": 1, "repository_root": str(settings.root), "state_dir": str(settings.state_dir),
            "kind": "ft_inbox_batch", "prepared_at": batch["started_at"], "due_slot": attempt["due_slot"],
            "lookback_hours": lookback, "collection_mode": batch.get("collection_mode", "daily"),
            **snapshot, **ft_sync._upload_base(settings.root), "status": "ready", "batch_file": str(batch_file),
            "batch_sha256": file["sha256"], "batch_id": batch["batch_id"], "last_attempt_at": batch["started_at"],
            "result_summary": ft_sync._summary(collected), "preview_result": ft_sync._summary(merged),
            "publish_files": [file]}
    return ft_sync._write_plan(plan_file, plan)


def can_continue_backfill(report, inserted):
    """Keep a productive backfill going after isolated article failures.

    Preserve the partial health report. Access restrictions, feed failures and
    stopped runs still require attention instead of another collection batch.
    """
    isolated = {"invalid_or_incomplete_article", "article_request_failed"}
    errors = report.get("errors", [])
    return (inserted > 0 and report.get("status") == "partial"
            and report.get("stopped") is False
            and report.get("reason") in isolated and bool(errors)
            and all(isinstance(error, dict) and error.get("reason") in isolated for error in errors))


def _finish_import(settings, state, plan, observed):
    receipt = ft_sync.acknowledge(settings.root, Path(plan["plan_file"]), observed["main_commit_sha"])
    state.update(batch_id=plan["batch_id"], plan_file=plan["plan_file"], push_state="imported",
                 main_commit_sha=receipt["commit_sha"], last_imported_at=utcnow().isoformat())
    _save_state(settings, state)
    summary = plan.get("result_summary", {})
    failed = summary.get("status") in ("failed", "partial")
    report = read_json(Path(plan["batch_file"])).get("report", {}) if failed else {}
    return {"status": "imported_needs_attention" if failed else "imported", "batch_id": plan["batch_id"],
            "reason": summary.get("reason") if failed else None,
            "commit_sha": receipt["commit_sha"], "inserted": observed["inserted"],
            "total_articles": receipt["total_articles"], "ft_articles": receipt["ft_articles"],
            "continue_backfill": can_continue_backfill(report, observed["inserted"]),
            "cooldown_until": state.get("cooldown_until")}


def service_plan(settings, state, plan):
    """Recover an existing push before attempting any new push of this batch."""
    _, plan = ft_sync._load_plan(settings.root, Path(plan["plan_file"]))
    _cooldown(settings, state, plan, utcnow())
    base = ft_sync._upload_base(settings.root)
    observed = _main_receipt(settings.root, plan["batch_id"], base["main_sha"], plan["main_sha"])
    if observed:
        return _finish_import(settings, state, plan, observed)
    pushed = _uploaded_commit(settings.root, plan, state, base)
    if not pushed and state.get("batch_id") == plan["batch_id"] and state.get("push_state") in ("uploaded", "workflow_failed"):
        # Confirmed uploads must never be recreated just because refs/API are stale.
        pushed = state.get("inbox_commit_sha")
    if not pushed:
        plan.update(base)
        ft_sync._write_plan(Path(plan["plan_file"]), plan)
        same_parent = state.get("batch_id") == plan["batch_id"] and state.get("parent_sha") == plan["parent_sha"]
        commit = state.get("inbox_commit_sha") if same_parent and state.get("push_state") in ("built", "push_started") else None
        if not commit:
            commit = build_commit(settings, plan)
        state.update(batch_id=plan["batch_id"], plan_file=plan["plan_file"], parent_sha=plan["parent_sha"],
                     inbox_commit_sha=commit, push_state="push_started")
        _save_state(settings, state)  # Persist before any packet can reach GitHub.
        try:
            push_commit(settings, commit)
        except AutorunError:
            return {"status": "pending_upload", "reason": "push_not_confirmed", "batch_id": plan["batch_id"]}
        pushed = commit
    state.update(batch_id=plan["batch_id"], plan_file=plan["plan_file"], inbox_commit_sha=pushed, push_state="uploaded")
    _save_state(settings, state)
    deadline = time.monotonic() + settings.wait_seconds
    while True:
        main, _ = ft_sync._fetch(settings.root)
        observed = _main_receipt(settings.root, plan["batch_id"], main, plan["main_sha"])
        if observed:
            return _finish_import(settings, state, plan, observed)
        workflow = workflow_status(pushed)
        if workflow["state"] in ("failed", "success"):
            # The workflow may report FT's partial failure after committing data.
            main, _ = ft_sync._fetch(settings.root)
            observed = _main_receipt(settings.root, plan["batch_id"], main, plan["main_sha"])
            if observed:
                return _finish_import(settings, state, plan, observed)
            state.update(push_state="workflow_failed", workflow_run_id=workflow.get("run_id"))
            _save_state(settings, state)
            reason = "import_workflow_failed" if workflow["state"] == "failed" else "workflow_succeeded_without_receipt"
            return {"status": "needs_attention", "reason": reason, "batch_id": plan["batch_id"]}
        if time.monotonic() >= deadline:
            return {"status": "pending_import", "reason": "awaiting_main_receipt", "batch_id": plan["batch_id"]}
        time.sleep(min(settings.poll_seconds, max(0, deadline - time.monotonic())))


def run(settings):
    validate_settings(settings)
    if settings.check:
        return {"status": "configuration_ok", "reason": "no_collection_or_push_performed"}
    with process_lock(settings.state_dir):
        state = _load_state(settings)
        _inherit_cooldown(settings, state)
        while True:
            plan = _pending(settings)
            if plan is None:
                cooldown = state.get("cooldown_until")
                if cooldown and utcnow() < datetime.fromisoformat(cooldown):
                    return {"status": "cooldown", "reason": "rate_limited", "cooldown_until": cooldown}
                if settings.resume_only:
                    return {"status": "idle", "reason": "no_pending_batch"}
                plan = ft_sync.prepare(settings.root, settings.state_dir, settings.cookie_file,
                                       max_new=settings.max_new, lookback_hours=settings.lookback_hours)
                if plan["status"] == "skipped":
                    return {"status": "skipped", "reason": plan["result_summary"]["reason"],
                            "total_articles": plan["result_summary"].get("after")}
            result = service_plan(settings, state, plan)
            productive = result["status"] == "imported" or result.get("continue_backfill") is True
            if (settings.resume_only or settings.lookback_hours != 120 or not productive
                    or result.get("inserted", 0) == 0 or result.get("total_articles", 0) >= 3000):
                return result


def _record_result(settings, result):
    # No article text, key/cookie contents, SSH diagnostics, or raw response bodies.
    allowed = {"status", "reason", "batch_id", "commit_sha", "inserted", "total_articles", "ft_articles", "cooldown_until", "continue_backfill"}
    safe = {key: value for key, value in result.items() if key in allowed}
    settings.state_dir.mkdir(parents=True, exist_ok=True)
    write_json(settings.state_dir / "autorun-result.json", safe)
    with (settings.state_dir / "autorun-log.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({"at": utcnow().isoformat(), **safe}, ensure_ascii=False) + "\n")
    return safe


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--cookie-file", type=Path, required=True)
    parser.add_argument("--ssh-key", type=Path, required=True)
    parser.add_argument("--known-hosts", type=Path, required=True)
    parser.add_argument("--ssh-executable", type=Path)
    parser.add_argument("--ssh-host", choices=("github.com", "ssh.github.com"), default="github.com")
    parser.add_argument("--ssh-port", type=int, choices=(22, 443), default=22)
    parser.add_argument("--lookback-hours", type=int, choices=(48, 120), default=48)
    parser.add_argument("--max-new", type=int, default=100)
    parser.add_argument("--wait-seconds", type=int, default=300)
    parser.add_argument("--poll-seconds", type=int, default=30)
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--check", action="store_true")
    modes.add_argument("--resume-only", action="store_true")
    settings = Settings(**vars(parser.parse_args(argv)))
    try:
        result = run(settings)
    except (AutorunError, ft_sync.SyncError) as error:
        reason = str(error)
        status = ("idle" if reason == "already_running" else "needs_attention"
                  if reason in ("receipt_history_limit_reached", "main_history_changed") else "error")
        result = {"status": status,
                  "reason": reason if re.fullmatch(r"[a-z_]{1,80}", reason) else "autorun_failed"}
    except Exception:
        result = {"status": "error", "reason": "autorun_failed"}
    try:
        # Never turn an invalid state-dir argument into a write inside the repo.
        if not settings.state_dir.resolve().is_relative_to(settings.root.resolve()):
            result = _record_result(settings, result)
    except Exception:
        pass
    print(json.dumps(result, ensure_ascii=False))
    return 1 if result["status"] in ("error", "needs_attention", "imported_needs_attention") else 0


if __name__ == "__main__":
    raise SystemExit(main())
