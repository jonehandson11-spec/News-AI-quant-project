"""Private access state for FT collection; never publish this file or its hashes."""
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path

from crawler.ft_impl.session import effective_cookies
from scripts.dataset import read_json, write_json

AUTH_REASONS = {"auth_required", "auth_expired", "login_or_subscription_required"}
STATE_FILE = "access-state.json"


def credential_fingerprint(path):
    # Ignore formatting, tracking cookies and a Windows UTF-8 BOM. A mere touch
    # of the file must not schedule another collection with the same credential.
    cookies = effective_cookies(Path(path).read_text(encoding="utf-8-sig"))
    payload = json.dumps(cookies, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def load(state_dir, root):
    path = state_dir / STATE_FILE
    state = read_json(path) if path.is_file() else {
        "format_version": 1, "repository_root": str(root.resolve())}
    if state.get("format_version") != 1 or Path(state.get("repository_root", "")).resolve() != root.resolve():
        raise ValueError("invalid_access_state")
    return state


def _time(value):
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError("invalid_access_state_time")
    return result


def decision(state, fingerprint, now, *, inherited_cooldown=None):
    deadlines = [_time(value) for value in (state.get("retry_after"), inherited_cooldown) if value]
    if deadlines and max(deadlines) > now:
        return {"reason": "rate_limited", "retry_after": max(deadlines).isoformat()}
    if state.get("blocked_fingerprint") == fingerprint and state.get("auth_reason") in AUTH_REASONS:
        return {"reason": "awaiting_credential_update", "auth_reason": state["auth_reason"],
                "action": "update_local_credentials"}
    return None


def changed_credentials(state, fingerprint, attempt):
    return (state.get("auth_reason") in AUTH_REASONS
            and state.get("blocked_fingerprint") not in (None, fingerprint)
            and attempt.get("credential_fingerprint") != fingerprint)


def observe(state_dir, root, fingerprint, batch):
    """Idempotent, observation-time-based state; no paths/hashes enter a batch."""
    state = load(state_dir, root)
    batch_id = batch["batch_id"]
    if state.get("observed_batch_id") == batch_id:
        return state
    at = _time(batch["finished_at"])
    if state.get("observed_at") and at < _time(state["observed_at"]):
        return state
    report = batch.get("report", {})
    reason = report.get("reason")
    if reason in AUTH_REASONS and report.get("stopped") is True:
        state.update(blocked_fingerprint=fingerprint, auth_reason=reason)
    elif report.get("status") in ("complete", "partial") and report.get("counts", {}).get("success", 0) > 0:
        state.pop("blocked_fingerprint", None)
        state.pop("auth_reason", None)
    if reason == "rate_limited":
        seconds = report.get("retry_after_seconds", 3600)
        seconds = max(3600, seconds) if type(seconds) is int and seconds >= 0 else 3600
        try:
            deadline = at + timedelta(seconds=seconds)
        except OverflowError:
            deadline = datetime.max.replace(tzinfo=timezone.utc)
        if state.get("retry_after"):
            deadline = max(deadline, _time(state["retry_after"]))
        state["retry_after"] = deadline.isoformat()
    state.update(observed_batch_id=batch_id, observed_at=at.isoformat())
    temporary = state_dir / (STATE_FILE + ".tmp")
    write_json(temporary, state)
    temporary.replace(state_dir / STATE_FILE)
    return state
