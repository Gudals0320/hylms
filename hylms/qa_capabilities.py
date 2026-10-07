"""Validate session-bound observations of QA tool discovery, not model guesses.

The Python worker cannot inspect Codex's tool registry. These records are an
auditable observation contract, not cryptographic proof of host availability.
"""
import datetime as dt
import re

QA_AVAILABILITY_ERRORS = {
    "runtime_qa_unavailable", "runtime_qa_tools_missing",
    "runtime_qa_spawn_failed", "runtime_qa_execution_failed",
}


def availability_error(evidence, session, now=None):
    if not isinstance(evidence, dict) or evidence.get("schema_version") != 1 or evidence.get("session_id") != session:
        return None
    try:
        checked = dt.datetime.fromisoformat(evidence["checked_at"])
        current = now or dt.datetime.now(dt.timezone.utc)
        if checked.tzinfo is None or not 0 <= (current - checked).total_seconds() <= 86400:
            return None
    except (KeyError, TypeError, ValueError):
        return None
    if evidence.get("discovery_method") != "tool_registry":
        return None
    names = evidence.get("discovered_tools")
    if not isinstance(names, list) or len(names) > 100 or any(
        not isinstance(n, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,160}", n) for n in names
    ):
        return None
    role_names = [evidence.get(k) for k in ("spawn_tool", "send_tool", "wait_tool")]
    complete = all(isinstance(n, str) and n in names for n in role_names)
    status = evidence.get("status")
    recognizable_complete = all(any(n.endswith(suffix) for n in names)
                                for suffix in ("spawn_agent", "send_input", "wait_agent"))
    if status == "tools_missing" and not complete and not recognizable_complete:
        return "runtime_qa_tools_missing"
    if status in {"spawn_failed", "execution_failed"} and complete:
        if evidence.get("attempted_tool") not in role_names:
            return None
        if not isinstance(evidence.get("failure_code"), str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,79}", evidence["failure_code"]):
            return None
        if status == "spawn_failed" and evidence["attempted_tool"] == evidence["spawn_tool"]:
            return "runtime_qa_spawn_failed"
        if status == "execution_failed" and evidence.get("reviewer_agent_id") and evidence["attempted_tool"] in role_names[1:]:
            return "runtime_qa_execution_failed"
    return None
