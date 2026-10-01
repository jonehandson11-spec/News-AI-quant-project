"""No real FT requests, GitHub pushes, deploy keys, or API calls are used here."""
from datetime import datetime, timedelta, timezone
import hashlib
import io
import json
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError

import test_ft_sync as fixtures
from scripts import ft_autorun as runner
from scripts import ft_sync
from scripts.dataset import read_json, write_json
from scripts.ft_local import merge_batch


class AutorunTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.FTSyncTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        f = self.fixture
        self.key = f.directory / "test key"
        self.hosts = f.directory / "test-hosts"
        self.key.write_text("FAKE-PRIVATE-KEY", encoding="utf-8")
        self.hosts.write_text("FAKE-KNOWN-HOST", encoding="utf-8")
        self.settings = runner.Settings(f.repo, f.state, f.cookie, self.key, self.hosts,
                                        ssh_executable=Path(sys.executable), wait_seconds=0)
        self.api = patch.object(runner, "workflow_status", return_value={"state": "unknown"}).start()
        self.addCleanup(patch.stopall)

    def plan(self):
        plan, calls = self.fixture.prepare()
        self.assertEqual(calls, 1)
        return plan

    def initial_state(self):
        return {"format_version": 1, "repository": runner.REPOSITORY}

    def test_private_index_commit_only_changes_inbox_and_preserves_checkout(self):
        plan = self.plan()
        index = self.fixture.repo / ".git/index"
        before = index.read_bytes()
        original_status = self.fixture.command(self.fixture.repo, "status", "--porcelain")
        commit = runner.build_commit(self.settings, plan)
        self.assertEqual(self.fixture.command(self.fixture.repo, "rev-parse", commit + "^"), plan["parent_sha"])
        changed = self.fixture.command(self.fixture.repo, "diff-tree", "--no-commit-id", "--name-only", "-r", plan["main_sha"], commit)
        self.assertEqual(changed, ft_sync.PUBLISH_PATH)
        blob = self.fixture.command(self.fixture.repo, "rev-parse", commit + ":" + ft_sync.PUBLISH_PATH)
        self.assertEqual(blob, plan["publish_files"][0]["blob_sha"])
        self.assertEqual(index.read_bytes(), before)
        self.assertEqual(self.fixture.command(self.fixture.repo, "rev-parse", "HEAD"), self.fixture.initial)
        self.assertEqual(self.fixture.command(self.fixture.repo, "status", "--porcelain"), original_status)

    def test_push_uses_only_fixed_inbox_and_strict_configured_ssh(self):
        result = subprocess.CompletedProcess([], 0, b"", b"")
        with patch.object(runner.subprocess, "run", return_value=result) as invoke:
            runner.push_commit(self.settings, "a" * 40)
        args = invoke.call_args.args[0]
        self.assertIn("git@github.com:" + runner.REPOSITORY + ".git", args)
        self.assertEqual(args[-1], "a" * 40 + ":refs/heads/codex/ft-inbox")
        self.assertNotIn("--force", args)
        command = invoke.call_args.kwargs["env"]["GIT_SSH_COMMAND"]
        for flag in ("BatchMode=yes", "IdentitiesOnly=yes", "StrictHostKeyChecking=yes", "IdentityAgent=none"):
            self.assertIn(flag, command)
        self.assertIn(self.key.as_posix(), command)
        self.assertIn(self.hosts.as_posix(), command)
        self.assertNotIn("FAKE-PRIVATE-KEY", command)
        self.settings.ssh_host, self.settings.ssh_port = "ssh.github.com", 443
        self.assertIn("-p 443", runner.ssh_command(self.settings))
        self.settings.ssh_host = "untrusted.invalid"
        with self.assertRaisesRegex(runner.AutorunError, "invalid_ssh_destination"):
            runner.ssh_command(self.settings)

    def test_crash_after_push_is_detected_without_second_push(self):
        plan = self.plan()
        commit = runner.build_commit(self.settings, plan)
        self.fixture.command(self.fixture.repo, "push", "origin", commit + ":refs/heads/" + ft_sync.INBOX_BRANCH)
        state = {**self.initial_state(), "batch_id": plan["batch_id"], "inbox_commit_sha": commit,
                 "push_state": "push_started", "parent_sha": plan["parent_sha"]}
        with patch.object(runner, "push_commit", side_effect=AssertionError("duplicate push")), \
             patch.object(ft_sync, "prepare", side_effect=AssertionError("recrawl")):
            result = runner.service_plan(self.settings, state, plan)
        self.assertEqual(result["status"], "pending_import")
        self.assertEqual(state["push_state"], "uploaded")

    def test_existing_connector_upload_is_recognized_by_blob(self):
        plan = self.plan()
        commit = runner.build_commit(self.settings, plan)
        self.fixture.command(self.fixture.repo, "push", "origin", commit + ":refs/heads/" + ft_sync.INBOX_BRANCH)
        with patch.object(runner, "push_commit", side_effect=AssertionError("duplicate push")):
            result = runner.service_plan(self.settings, self.initial_state(), plan)
        self.assertEqual(result["status"], "pending_import")

    def test_network_push_failure_preserves_same_commit_for_resume(self):
        plan = self.plan()
        state = self.initial_state()
        with patch.object(runner, "push_commit", side_effect=runner.AutorunError("push_not_confirmed")):
            first = runner.service_plan(self.settings, state, plan)
        self.assertEqual(first["status"], "pending_upload")
        saved_commit = state["inbox_commit_sha"]
        with patch.object(runner, "build_commit", side_effect=AssertionError("must reuse commit")), \
             patch.object(runner, "push_commit") as push:
            second = runner.service_plan(self.settings, state, plan)
        push.assert_called_once_with(self.settings, saved_commit)
        self.assertEqual(second["status"], "pending_import")

    def test_public_api_unavailable_does_not_trigger_repeated_push(self):
        plan = self.plan()
        state = self.initial_state()
        with patch.object(runner, "push_commit") as push:
            self.assertEqual(runner.service_plan(self.settings, state, plan)["status"], "pending_import")
            self.assertEqual(runner.service_plan(self.settings, state, plan)["status"], "pending_import")
        self.assertEqual(push.call_count, 1)

    def test_workflow_failure_retains_batch_without_repush(self):
        plan = self.plan()
        state = self.initial_state()
        self.api.return_value = {"state": "failed", "run_id": 123}
        with patch.object(runner, "push_commit") as push:
            result = runner.service_plan(self.settings, state, plan)
            again = runner.service_plan(self.settings, state, plan)
        self.assertEqual(result["reason"], "import_workflow_failed")
        self.assertEqual(again["status"], "needs_attention")
        self.assertEqual(push.call_count, 1)
        self.assertTrue(Path(plan["batch_file"]).exists())
        self.assertFalse((Path(plan["plan_file"]).parent / "receipt.json").exists())

    def test_partial_collection_is_acknowledged_before_reporting_attention(self):
        plan = self.plan()
        plan["result_summary"] = {"status": "partial", "reason": "login_or_subscription_required", "collected": 1}
        write_json(Path(plan["plan_file"]), plan)
        merge_batch(self.fixture.writer, Path(plan["batch_file"]), now=fixtures.NOW + timedelta(minutes=1))
        imported = self.fixture.push_data("Import local batch")
        with patch.object(runner, "push_commit", side_effect=AssertionError("already imported")):
            result = runner.service_plan(self.settings, self.initial_state(), plan)
        self.assertEqual(result["status"], "imported_needs_attention")
        self.assertEqual(result["commit_sha"], imported)
        self.assertEqual(result["inserted"], 1)
        self.assertTrue((self.fixture.state / "latest_receipt.json").exists())

    def test_imported_receipt_is_found_after_another_ft_batch_overwrites_latest(self):
        plan = self.plan()
        merge_batch(self.fixture.writer, Path(plan["batch_file"]), now=fixtures.NOW + timedelta(minutes=1))
        imported = self.fixture.push_data("Import local batch")
        report_path = self.fixture.writer / "data/source_reports/ft.json"
        report = read_json(report_path)
        report['latest_run']['batch_id'] = 'b' * 64
        write_json(report_path, report)
        manifest_path = self.fixture.writer / "data/manifest.json"
        manifest = read_json(manifest_path)
        manifest['latest_run']['local_batch']['batch_id'] = 'b' * 64
        write_json(manifest_path, manifest)
        self.fixture.push_data("Later FT batch supersedes latest receipt")
        with patch.object(runner, "push_commit", side_effect=AssertionError("already imported")):
            result = runner.service_plan(self.settings, self.initial_state(), plan)
        self.assertEqual(result['status'], 'imported')
        self.assertEqual(result['commit_sha'], imported)

    def test_receipt_history_bound_reports_attention_instead_of_republishing(self):
        commits = '\n'.join(f'{number:040x}' for number in range(1, 102)).encode()
        with patch.object(runner, '_receipt_at', return_value=None), \
             patch.object(ft_sync, '_git', side_effect=[subprocess.CompletedProcess([], 0, b'', b''),
                                                       subprocess.CompletedProcess([], 0, commits, b'')]):
            with self.assertRaisesRegex(runner.AutorunError, 'receipt_history_limit_reached'):
                runner._main_receipt(self.fixture.repo, 'a' * 64, 'f' * 40, '0' * 40)

    def test_rate_limit_cooldown_uses_observation_time_and_does_not_extend(self):
        plan = self.plan()
        batch = read_json(Path(plan["batch_file"]))
        observed = fixtures.NOW
        batch.update(finished_at=observed.isoformat())
        batch["report"].update(reason="rate_limited", retry_after_seconds=7200)
        write_json(Path(plan["batch_file"]), batch)
        state = self.initial_state()
        runner._cooldown(self.settings, state, plan, observed + timedelta(hours=1))
        expected = (observed + timedelta(hours=2)).isoformat()
        self.assertEqual(state["cooldown_until"], expected)
        runner._cooldown(self.settings, state, plan, observed + timedelta(hours=2))
        self.assertEqual(state["cooldown_until"], expected)
        batch["report"].pop("retry_after_seconds")
        write_json(Path(plan["batch_file"]), batch)
        default = self.initial_state()
        runner._cooldown(self.settings, default, plan, observed + timedelta(minutes=50))
        self.assertEqual(default["cooldown_until"], (observed + timedelta(hours=1)).isoformat())

    def test_cooldown_blocks_new_collection_but_still_services_pending(self):
        self.fixture.state.mkdir()
        state = {**self.initial_state(), "cooldown_until": (fixtures.NOW + timedelta(hours=1)).isoformat()}
        runner._save_state(self.settings, state)
        with patch.object(runner, "validate_settings"), patch.object(runner, "utcnow", return_value=fixtures.NOW), \
             patch.object(ft_sync, "prepare", side_effect=AssertionError("cooldown recrawl")):
            self.assertEqual(runner.run(self.settings)["status"], "cooldown")
            with patch.object(runner, "_pending", return_value={"batch_id": "x"}), \
                 patch.object(runner, "service_plan", return_value={"status": "pending_import"}) as service:
                self.assertEqual(runner.run(self.settings)["status"], "pending_import")
                service.assert_called_once()

    def test_recover_interrupted_prepare_uses_existing_batch_without_ft(self):
        real_snapshot = ft_sync._snapshot
        calls = 0
        def snapshot(*args):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise ft_sync.SyncError("git_fetch_failed")
            return real_snapshot(*args)
        with patch.object(ft_sync, "_snapshot", side_effect=snapshot):
            with self.assertRaisesRegex(ft_sync.SyncError, "git_fetch_failed"):
                self.fixture.prepare()
        with patch.object(ft_sync, "_collect_batch", side_effect=AssertionError("recrawl")), \
             patch.object(runner, "utcnow", return_value=fixtures.NOW + timedelta(minutes=1)):
            plan = runner._pending(self.settings)
        self.assertEqual(plan["status"], "ready")
        self.assertEqual(plan["result_summary"]["collected"], 1)
        ft_sync._load_plan(self.fixture.repo, Path(plan["plan_file"]))

    def test_check_and_empty_resume_never_collect_or_push(self):
        with patch.object(runner, "validate_settings"), \
             patch.object(ft_sync, "prepare", side_effect=AssertionError("must not collect")), \
             patch.object(runner, "push_commit", side_effect=AssertionError("must not push")):
            self.settings.check = True
            self.assertEqual(runner.run(self.settings)["status"], "configuration_ok")
            self.settings.check = False
            self.settings.resume_only = True
            self.assertEqual(runner.run(self.settings)["status"], "idle")

    def test_daily_does_not_force_and_backfill_stops_on_no_insert(self):
        self.settings.lookback_hours = 120
        plans = [{"status": "ready", "batch_id": "one"}, {"status": "ready", "batch_id": "two"}]
        results = [{"status": "imported", "inserted": 100, "total_articles": 1787},
                   {"status": "imported", "inserted": 0, "total_articles": 1787}]
        with patch.object(runner, "validate_settings"), patch.object(runner, "_pending", return_value=None), \
             patch.object(ft_sync, "prepare", side_effect=plans) as prepare, \
             patch.object(runner, "service_plan", side_effect=results):
            self.assertEqual(runner.run(self.settings)["inserted"], 0)
        self.assertEqual(prepare.call_count, 2)
        for call in prepare.call_args_list:
            self.assertNotIn("force", call.kwargs)
            self.assertEqual(call.kwargs["lookback_hours"], 120)

    def test_process_lock_releases_after_child_is_killed(self):
        code = ("import sys,time;from pathlib import Path;sys.path.insert(0,sys.argv[1]);"
                "from scripts.ft_autorun import process_lock;"
                "\nwith process_lock(Path(sys.argv[2])):\n print('ready',flush=True)\n time.sleep(120)\n")
        child = subprocess.Popen([sys.executable, "-c", code, str(fixtures.ROOT), str(self.fixture.state)],
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                 creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        try:
            self.assertEqual(child.stdout.readline().strip(), "ready")
            with self.assertRaisesRegex(runner.AutorunError, "already_running"):
                with runner.process_lock(self.fixture.state):
                    pass
        finally:
            child.kill()
            child.communicate(timeout=10)
        with runner.process_lock(self.fixture.state):
            pass

    def test_cli_error_never_prints_raw_exception_or_key_contents(self):
        args = ["--root", str(self.fixture.repo), "--state-dir", str(self.fixture.state),
                "--cookie-file", str(self.fixture.cookie), "--ssh-key", str(self.key), "--known-hosts", str(self.hosts)]
        from contextlib import redirect_stdout
        output = io.StringIO()
        with patch.object(runner, "run", side_effect=RuntimeError("FAKE-PRIVATE-KEY")), redirect_stdout(output):
            self.assertEqual(runner.main(args), 1)
        self.assertNotIn("FAKE-PRIVATE-KEY", output.getvalue())
        self.assertEqual(json.loads(output.getvalue())["reason"], "autorun_failed")


class PublicAPITests(unittest.TestCase):
    def test_404_or_invalid_response_remains_unknown(self):
        for error in (HTTPError("https://api.github.com", 404, "not found", {}, None), ValueError("private response")):
            with self.subTest(error=type(error).__name__), patch.object(runner, "urlopen", side_effect=error):
                self.assertEqual(runner.workflow_status("a" * 40), {"state": "unknown"})


if __name__ == "__main__":
    unittest.main()
