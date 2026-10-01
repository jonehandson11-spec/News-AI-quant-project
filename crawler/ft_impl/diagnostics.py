"""Closed-vocabulary diagnostics: no remote text, URLs, headers or cookie values."""

PARSE_REASONS = frozenset({
    "visible_article_body_missing", "incomplete_article_body", "article_title_missing",
    "missing_or_ambiguous_publication_time", "missing_publication_time",
    "naive_publication_time", "invalid_publication_time",
})
NEW_REASONS = PARSE_REASONS | {
    "subscription_required", "login_prompt_detected", "subscription_barrier_detected",
    "paywall_page_text_detected", "paywall_body_text_detected",
}
STAGES = frozenset({"input", "credential", "feed_fetch", "feed_parse",
                    "article_fetch", "article_parse", "complete"})
_ACTIONS = {
    "ok": "none",
    "auth_required": "refresh_session",
    "auth_expired": "refresh_session",
    "credential_unavailable": "check_private_cookie_file",
    "login_prompt_detected": "refresh_session",
    "subscription_required": "check_subscription_access",
    "subscription_barrier_detected": "check_subscription_access",
    "paywall_page_text_detected": "verify_browser_access_then_parser",
    "paywall_body_text_detected": "verify_browser_access_then_parser",
    "login_or_subscription_required": "verify_browser_access_then_parser",
    "access_denied": "check_access_restriction",
    "rate_limited": "wait_before_retry",
    "robots_disallowed": "stop_robots_restriction",
    "robots_unavailable": "check_robots_availability",
    "article_request_failed": "check_network",
    "feed_request_failed": "check_network",
    "consecutive_network_failures": "check_network",
    "invalid_feed": "check_feed_parser",
    "redirect_limit": "check_destination",
    "unsupported_method": "check_local_setup",
    "invalid_article_url": "check_article_url",
    "unsafe_destination": "check_destination",
    "private_file_must_be_external": "move_private_file_outside_repo",
    "invalid_arguments": "check_arguments",
    "invalid_or_incomplete_article": "check_parser",
    "diagnostic_failed": "check_local_setup",
    "collector_failed": "check_local_setup",
}
_ACTIONS.update({reason: "check_parser" for reason in PARSE_REASONS})


def safe_diagnostic(reason, stage):
    reason = reason if isinstance(reason, str) and reason in _ACTIONS else "diagnostic_failed"
    stage = stage if isinstance(stage, str) and stage in STAGES else "input"
    return {"reason": reason, "stage": stage, "action": _ACTIONS[reason]}


class ArticleParseError(ValueError):
    def __init__(self, reason):
        self.reason = reason if reason in PARSE_REASONS else "invalid_or_incomplete_article"
        super().__init__(self.reason)
