"""Prepare an FT inbox batch; cloud Actions alone merges it into the shared database."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.dataset import BEIJING, iso, read_json, write_json
from scripts.validate_database import validate
from scripts import ft_run_guard

SNAPSHOT_FILES = (
    "data/manifest.json", "data/news.sqlite3", "data/news.csv", "data/progress.json",
    "data/source_reports/bbc.json", "data/source_reports/sina.json", "data/source_reports/ft.json",
)
EXPORT_FILES = SNAPSHOT_FILES + ("schema.sql", "crawl_config.json")
INBOX_BRANCH = "codex/ft-inbox"
PUBLISH_PATH = "incoming/ft-batch.json"
SHA = re.compile(r"[0-9a-f]{40}")


class SyncError(ValueError):
    """A fixed reason code safe to show without command output or credentials."""


def _now(value: datetime | None = None) -> datetime:
    value = value or datetime.now(timezone.utc)
    if value.tzinfo is None:
        raise SyncError("timezone_required")
    return value


def due_slot(now: datetime) -> datetime:
    """The latest Beijing 20:00 occurrence, including yesterday before 20:00."""
    local = _now(now).astimezone(BEIJING)
    slot = local.replace(hour=20, minute=0, second=0, microsecond=0)
    return slot if local >= slot else slot - timedelta(days=1)


def blob_sha(content: bytes) -> str:
    return hashlib.sha1(b"blob " + str(len(content)).encode("ascii") + b"\0" + content).hexdigest()


def _git_executable() -> str:
    executable = shutil.which("git")
    if executable:
        return executable
    bundled = Path.home() / ".cache/codex-runtimes/codex-primary-runtime/dependencies/native/git/cmd/git.exe"
    if bundled.is_file():
        return str(bundled)
    raise SyncError("git_unavailable")


def _git(root: Path, *arguments: str, reason: str = "git_read_failed", allowed=(0,)):
    environment = dict(os.environ)
    environment.update(GIT_TERMINAL_PROMPT="0", GCM_INTERACTIVE="Never", GIT_ASKPASS="",
                       SSH_ASKPASS="", GIT_SSH_COMMAND="ssh -o BatchMode=yes")
    try:
        result = subprocess.run(
            [_git_executable(), "-c", "credential.interactive=false", "-c", "credential.helper=",
             "-c", "core.askPass=", "-C", str(root), *arguments],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=environment,
            timeout=120, check=False, shell=False,
        )
    except (OSError, subprocess.SubprocessError):
        raise SyncError(reason) from None
    if result.returncode not in allowed:
        raise SyncError(reason)
    return result


def _repository(root: Path) -> Path:
    root = root.resolve()
    if not root.is_dir():
        raise SyncError("repository_missing")
    try:
        top = Path(os.fsdecode(_git(root, "rev-parse", "--show-toplevel").stdout.strip())).resolve()
    except (ValueError, OSError):
        raise SyncError("invalid_repository") from None
    if top != root:
        raise SyncError("repository_root_required")
    return root


def _outside(root: Path, path: Path, reason: str) -> Path:
    path = path.resolve()
    if path == root or path.is_relative_to(root):
        raise SyncError(reason)
    return path


@contextmanager
def _lock(state: Path):
    # The OS releases this lock even if the process crashes or is terminated.
    # Keep the file permanently: unlinking it could let a new process lock a
    # different inode while another waiter still has the original file open.
    try:
        state.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(state / "prepare.lock", os.O_CREAT | os.O_RDWR, 0o600)
    except OSError:
        raise SyncError("state_unavailable") from None
    with os.fdopen(descriptor, "r+b", buffering=0) as handle:
        locked = False
        try:
            try:
                handle.seek(0)
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                locked = True
            except OSError as error:
                import errno
                reason = "state_locked" if error.errno in (errno.EACCES, errno.EAGAIN, errno.EDEADLK) else "state_unavailable"
                raise SyncError(reason) from None
            handle.seek(0)
            handle.write((str(os.getpid()) + "\n").encode("ascii"))
            handle.truncate()
            yield
        finally:
            if locked:
                handle.seek(0)
                if os.name == "nt":
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _fetch(root: Path) -> tuple[str, str]:
    _git(root, "fetch", "--no-tags", "--no-recurse-submodules", "--no-write-fetch-head", "origin",
         "+refs/heads/main:refs/remotes/origin/main", reason="git_fetch_failed")
    parent = _git(root, "rev-parse", "refs/remotes/origin/main^{commit}").stdout.decode("ascii").strip()
    tree = _git(root, "rev-parse", parent + "^{tree}").stdout.decode("ascii").strip()
    if not SHA.fullmatch(parent) or not SHA.fullmatch(tree):
        raise SyncError("invalid_git_object")
    return parent, tree


def _snapshot(root: Path, directory: Path) -> dict:
    parent, tree = _fetch(root)
    staging = Path(tempfile.mkdtemp(prefix="snapshot-", dir=directory)).resolve()
    for relative in EXPORT_FILES:
        # The ref and every exported pathname are pinned; neither comes from a manifest.
        content = _git(root, "show", parent + ":" + relative).stdout
        output = staging / relative
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(content)
    validate(staging)
    return {"main_sha": parent, "base_tree_sha": tree, "staging_root": str(staging)}


def _metadata(root: Path):
    """Skipped hourly checks need small status files, not another DB/CSV copy."""
    parent, tree = _fetch(root)
    try:
        documents = [json.loads(_git(root, "show", parent + ":" + path).stdout)
                     for path in ("data/manifest.json", "crawl_config.json", "data/source_reports/ft.json")]
    except (ValueError, TypeError):
        raise SyncError("invalid_source_metadata") from None
    return {"main_sha": parent, "base_tree_sha": tree}, *documents


def _upload_base(root: Path) -> dict:
    main, tree = _fetch(root)
    remote_ref = "refs/heads/" + INBOX_BRANCH
    listed = _git(root, "ls-remote", "--heads", "origin", remote_ref,
                  reason="git_fetch_failed").stdout.decode("ascii").splitlines()
    inbox = None
    if listed:
        fields = listed[0].split()
        if len(listed) != 1 or len(fields) != 2 or fields[1] != remote_ref or not SHA.fullmatch(fields[0]):
            raise SyncError("invalid_inbox_ref")
        _git(root, "fetch", "--no-tags", "--no-recurse-submodules", "--no-write-fetch-head", "origin",
             "+" + remote_ref + ":refs/remotes/origin/" + INBOX_BRANCH, reason="git_fetch_failed")
        inbox = _git(root, "rev-parse", "refs/remotes/origin/" + INBOX_BRANCH + "^{commit}").stdout.decode("ascii").strip()
        if not SHA.fullmatch(inbox):
            raise SyncError("invalid_inbox_ref")
    return {"main_sha": main, "base_tree_sha": tree, "parent_sha": inbox or main,
            "expected_inbox_sha": inbox, "branch_name": INBOX_BRANCH}


def _collect_batch(*args, **kwargs):
    from scripts.ft_local import collect_batch
    return collect_batch(*args, **kwargs)


def _merge_batch(*args, **kwargs):
    from scripts.ft_local import merge_batch
    return merge_batch(*args, **kwargs)


def _summary(result: dict) -> dict:
    """Only fixed statuses and scalar counts belong in the upload plan summary."""
    summary = {}
    for key in ("status", "reason", "execution_location"):
        value = result.get(key)
        if isinstance(value, str) and re.fullmatch(r"[a-z_]{1,64}", value):
            summary[key] = value
    for key in ("collected", "snapshot_total", "before", "after", "inserted", "duplicates", "deferred_cap"):
        value = result.get(key)
        if type(value) is int and value >= 0:
            summary[key] = value
    if type(result.get("health_preserved")) is bool:
        summary["health_preserved"] = result["health_preserved"]
    return summary


def _batch_file(batch: Path) -> dict:
    content = batch.read_bytes()
    return {"path": PUBLISH_PATH, "local_path": str(batch), "bytes": len(content),
            "blob_sha": blob_sha(content), "sha256": hashlib.sha256(content).hexdigest()}


def _attempt_covers(value: dict, slot: datetime) -> bool:
    attempted = value.get("last_attempt_at")
    if not attempted:
        return False
    try:
        parsed = datetime.fromisoformat(attempted.replace("Z", "+00:00"))
        return parsed.tzinfo is not None and parsed >= slot
    except (ValueError, TypeError, AttributeError):
        raise SyncError("invalid_local_attempt_time") from None


def _write_plan(path: Path, plan: dict) -> dict:
    plan["plan_file"] = str(path)
    write_json(path, plan)
    return plan


def prepare(root: Path, state_dir: Path, cookie_file: Path, *, max_new: int = 100,
            force: bool = False, lookback_hours: int = 48, now: datetime | None = None) -> dict:
    root = _repository(root)
    state = _outside(root, state_dir, "state_must_be_outside_repository")
    cookie = _outside(root, cookie_file, "cookie_must_be_outside_repository")
    now = _now(now)
    if type(max_new) is not int or not 1 <= max_new <= 100:
        raise SyncError("invalid_max_new")
    if type(lookback_hours) is not int or lookback_hours not in (48, 120):
        raise SyncError("invalid_lookback_hours")
    backfill = lookback_hours == 120
    with _lock(state):
        directory = Path(tempfile.mkdtemp(prefix="run-", dir=state)).resolve()
        initial, manifest, config, report = _metadata(root)
        health = report.get("health", {})
        receipt_path, attempt_path = state / "latest_receipt.json", state / "last_attempt.json"
        receipt = read_json(receipt_path) if receipt_path.exists() else {}
        attempted = read_json(attempt_path) if attempt_path.exists() else {}
        if any(value and Path(value.get("repository_root", "")).resolve() != root for value in (receipt, attempted)):
            raise SyncError("state_belongs_to_another_repository")
        slot = due_slot(now)
        if attempted.get("plan_file") and Path(attempted["plan_file"]).is_file():
            pending_path, pending = _load_plan(root, Path(attempted["plan_file"]))
            if pending.get("status") == "ready" and not pending.get("receipt_file"):
                imported_ids = ((manifest.get("latest_run") or {}).get("local_batch", {}).get("batch_id"),
                                (report.get("latest_run") or {}).get("batch_id"))
                if pending["batch_id"] in imported_ids:
                    # The saved plan stays ready until acknowledge verifies main ancestry.
                    return {**pending, "status": "awaiting_acknowledgement", "import_commit_sha": initial["main_sha"]}
                pending.update(_upload_base(root))
                return _write_plan(pending_path, pending)
        if backfill and attempted.get("plan_file"):
            unresolved = Path(attempted["plan_file"])
            if not unresolved.is_file() and (unresolved.parent / "ft_batch.json").is_file():
                raise SyncError("pending_batch_without_plan")
        reason = None
        blocked = None
        fingerprint = None
        credential_retry = False
        if manifest["total_articles"] >= config["target_articles"]:
            reason = "target_reached"
        else:
            if not cookie.is_file():
                raise SyncError("cookie_file_missing")
            try:
                fingerprint = ft_run_guard.credential_fingerprint(cookie)
                if attempted.get("credential_fingerprint") and attempted.get("plan_file"):
                    previous_path = Path(attempted["plan_file"])
                    if previous_path.is_file():
                        _, previous = _load_plan(root, previous_path)
                        if previous.get("batch_file"):
                            ft_run_guard.observe(state, root, attempted["credential_fingerprint"],
                                                 read_json(Path(previous["batch_file"])))
                guard = ft_run_guard.load(state, root)
                autorun = read_json(state / "autorun.json") if (state / "autorun.json").is_file() else {}
                blocked = ft_run_guard.decision(guard, fingerprint, now,
                                               inherited_cooldown=autorun.get("cooldown_until"))
                credential_retry = ft_run_guard.changed_credentials(guard, fingerprint, attempted)
            except (OSError, ValueError, TypeError, KeyError):
                raise SyncError("access_state_unavailable") from None
            if blocked:
                reason = blocked["reason"]
        if reason is None and not (force or backfill or credential_retry) and ((health.get("execution_location") == "local" and _attempt_covers(health, slot))
                            or _attempt_covers(receipt, slot)):
            reason = "already_attempted_due_slot"
        elif reason is None and not (force or backfill or credential_retry) and _attempt_covers(attempted, slot):
            reason = "local_attempt_already_started"
        common = {"format_version": 1, "repository_root": str(root), "state_dir": str(state),
                  "kind": "ft_inbox_batch", "prepared_at": iso(now), "due_slot": iso(slot),
                  "lookback_hours": lookback_hours, "collection_mode": "backfill" if backfill else "daily"}
        plan_path = directory / "plan.json"
        if reason:
            return _write_plan(plan_path, {**common, **initial, "status": "skipped", "batch_file": None,
                               "batch_sha256": None, "result_summary": {"status": "skipped", "reason": reason,
                               "before": manifest["total_articles"], "after": manifest["total_articles"], "inserted": 0,
                               **(blocked or {})},
                               "publish_files": []})
        if not cookie.is_file():
            raise SyncError("cookie_file_missing")
        initial = _snapshot(root, directory)
        staging = Path(initial["staging_root"])
        batch = directory / "ft_batch.json"
        write_json(attempt_path, {"repository_root": str(root), "last_attempt_at": iso(now),
                                 "due_slot": iso(slot), "plan_file": str(plan_path),
                                 "credential_fingerprint": fingerprint})
        collected = _collect_batch(staging, cookie, batch, max_new=max_new, now=now,
                                   lookback_hours=lookback_hours)
        ft_run_guard.observe(state, root, fingerprint, read_json(batch))
        # Validate an optional preview against fresh data. Its database is never published.
        latest = _snapshot(root, directory)
        merged = _merge_batch(Path(latest["staging_root"]), batch, now=max(now, datetime.now(timezone.utc)))
        validate(Path(latest["staging_root"]))
        batch_data = read_json(batch)
        file = _batch_file(batch)
        plan = {**common, **latest, **_upload_base(root), "status": "ready", "batch_file": str(batch),
                "batch_sha256": file["sha256"], "batch_id": batch_data["batch_id"],
                "last_attempt_at": batch_data["started_at"], "result_summary": _summary(collected),
                "preview_result": _summary(merged), "publish_files": [file]}
        return _write_plan(plan_path, plan)


def _load_plan(root: Path, plan_file: Path) -> tuple[Path, dict]:
    plan_file = _outside(root, plan_file, "plan_must_be_outside_repository")
    plan = read_json(plan_file)
    if plan.get("format_version") != 1 or Path(plan.get("repository_root", "")).resolve() != root:
        raise SyncError("invalid_plan_repository")
    state = _outside(root, Path(plan["state_dir"]), "state_must_be_outside_repository")
    if not plan_file.is_relative_to(state) or plan_file.parent == state or Path(plan["plan_file"]).resolve() != plan_file:
        raise SyncError("invalid_plan_location")
    directory = plan_file.parent
    staging = Path(plan["staging_root"]).resolve()
    _outside(root, staging, "staging_must_be_outside_repository")
    if staging == directory or not staging.is_relative_to(directory):
        raise SyncError("invalid_staging_location")
    for key in ("main_sha", "base_tree_sha", "parent_sha"):
        if not isinstance(plan.get(key), str) or not SHA.fullmatch(plan[key]):
            raise SyncError("invalid_plan_git_object")
    if plan.get("branch_name") != INBOX_BRANCH or len(plan["publish_files"]) != 1:
        raise SyncError("invalid_publish_destination")
    item = plan["publish_files"][0]
    if item["path"] != PUBLISH_PATH:
        raise SyncError("invalid_publish_path")
    batch = Path(plan["batch_file"]).resolve()
    if batch == directory or not batch.is_relative_to(directory) or batch.is_relative_to(staging):
        raise SyncError("invalid_batch_location")
    if Path(item["local_path"]).resolve() != batch:
        raise SyncError("invalid_publish_location")
    expected = _batch_file(batch)
    if any(item.get(key) != expected[key] for key in ("bytes", "blob_sha", "sha256")) or plan["batch_sha256"] != expected["sha256"]:
        raise SyncError("batch_changed")
    batch_data = read_json(batch)
    if batch_data.get("batch_id") != plan.get("batch_id"):
        raise SyncError("batch_changed")
    return plan_file, plan


def acknowledge(root: Path, plan_file: Path, commit: str, *, now: datetime | None = None) -> dict:
    root = _repository(root)
    plan_file, plan = _load_plan(root, plan_file)
    if not isinstance(commit, str) or not SHA.fullmatch(commit):
        raise SyncError("invalid_commit_sha")
    if not plan["publish_files"] or plan["status"] != "ready":
        raise SyncError("plan_has_no_upload")
    with _lock(Path(plan["state_dir"])):
        remote, _ = _fetch(root)
        resolved = _git(root, "rev-parse", commit + "^{commit}", reason="ack_commit_missing").stdout.decode("ascii").strip()
        if resolved != commit:
            raise SyncError("invalid_commit_sha")
        ancestor = _git(root, "merge-base", "--is-ancestor", commit, remote,
                        reason="ack_ancestry_check_failed", allowed=(0, 1))
        if ancestor.returncode != 0:
            raise SyncError("ack_commit_not_in_main")
        try:
            manifest = json.loads(_git(root, "show", commit + ":data/manifest.json", reason="ack_receipt_missing").stdout)
            report = json.loads(_git(root, "show", commit + ":data/source_reports/ft.json", reason="ack_receipt_missing").stdout)
        except (ValueError, KeyError, TypeError):
            raise SyncError("ack_receipt_invalid") from None
        local_batch = (manifest.get("latest_run") or {}).get("local_batch", {})
        report_batch = (report.get("latest_run") or {}).get("batch_id")
        if plan["batch_id"] not in (local_batch.get("batch_id"), report_batch):
            raise SyncError("ack_batch_not_imported")
        receipt = {"format_version": 1, "status": "imported", "commit_sha": commit,
                   "verified_origin_main": remote, "acknowledged_at": iso(_now(now)),
                   "repository_root": str(root), "plan_file": str(plan_file), "batch_id": plan["batch_id"],
                   "due_slot": plan["due_slot"], "last_attempt_at": plan["last_attempt_at"],
                   "total_articles": manifest["total_articles"],
                   "ft_articles": manifest["sources"]["Financial Times"]["article_count"],
                   "collection_summary": plan["result_summary"]}
        receipt_file = plan_file.parent / "receipt.json"
        write_json(receipt_file, receipt)
        write_json(Path(plan["state_dir"]) / "latest_receipt.json", receipt)
        plan["receipt_file"] = str(receipt_file)
        _write_plan(plan_file, plan)
        return {**receipt, "receipt_file": str(receipt_file)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    preparing = commands.add_parser("prepare")
    preparing.add_argument("--root", type=Path, required=True)
    preparing.add_argument("--state-dir", type=Path, required=True)
    preparing.add_argument("--cookie-file", type=Path, required=True)
    preparing.add_argument("--max-new", type=int, default=100)
    preparing.add_argument("--force", action="store_true")
    preparing.add_argument("--lookback-hours", type=int, choices=(48, 120), default=48,
                           help="120 explicitly requests manual backfill; pending batches remain protected")
    acknowledging = commands.add_parser("acknowledge")
    acknowledging.add_argument("--root", type=Path, required=True)
    acknowledging.add_argument("--plan", type=Path, required=True)
    acknowledging.add_argument("--commit", required=True)
    args = parser.parse_args()
    try:
        if args.command == "prepare":
            result = prepare(args.root, args.state_dir, args.cookie_file, max_new=args.max_new,
                             force=args.force, lookback_hours=args.lookback_hours)
        else:
            result = acknowledge(args.root, args.plan, args.commit)
    except SyncError as error:
        print(json.dumps({"status": "error", "reason": str(error)}))
        return 1
    except Exception:
        # External library errors may contain response bodies, local paths, or cookies.
        print(json.dumps({"status": "error", "reason": "ft_sync_failed"}))
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
