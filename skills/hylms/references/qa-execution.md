# Discover and prepare the independent QA executor

Before an authorized pipeline/manual worker starts, inspect the CURRENT tool registry, including deferred tools. This preflight is not required for read-only status, pending, targets or code development.

```javascript
const qaTools = ALL_TOOLS.filter(t => /spawn_agent|send_input|wait_agent/.test(t.name));
text(qaTools);
```

Read the returned declarations. On this host the native names have been `multi_agent_v1__spawn_agent`, `multi_agent_v1__send_input`, `multi_agent_v1__wait_agent`. Use them only when present in the actual registry. Omission from the short initial tool description is not absence. Do not substitute user-owned task creation or a separate API.

If that name search yields no native match, broaden the registry search to tool names AND descriptions containing subagent, delegate or spawn and inspect their schemas. Do not declare absence solely because a namespace or version changed.

Create one read-only reviewer with fork_context:false, inheriting the task's model/effort. Ask only for QA_READY, with no tools, files, external calls or further delegation. Wait for that response before starting the worker. Retain and reuse this agent ID for all QA in this occurrence. A later scheduled occurrence needs fresh discovery and a new reviewer, even in the same chat. A readiness response is NOT a verdict. Keep each waiting tool call within 60 seconds.

Save UTF-8 `<this task staging>/qa-capabilities.json` from the actual results:

```json
{
  "schema_version": 1,
  "session_id": "actual current CODEX_THREAD_ID",
  "checked_at": "actual ISO timestamp with timezone",
  "discovery_method": "tool_registry",
  "discovered_tools": ["multi_agent_v1__spawn_agent", "multi_agent_v1__send_input", "multi_agent_v1__wait_agent"],
  "spawn_tool": "multi_agent_v1__spawn_agent",
  "send_tool": "multi_agent_v1__send_input",
  "wait_tool": "multi_agent_v1__wait_agent",
  "status": "ready",
  "reviewer_agent_id": "actual returned ID"
}
```

Never manufacture discoveries, timestamps, calls or agent IDs. This record is an audit contract, not a grant of authority or cryptographic proof. Exclude credentials, unrelated tool output and sensitive error bodies.

If the registry genuinely lacks a role, record status:tools_missing with the actual matching names; missing role fields are null/omitted. An actually attempted failed creation uses status:spawn_failed, attempted_tool and a sanitized lowercase failure_code. A send/wait failure after creation uses status:execution_failed, attempted_tool, failure_code and reviewer_agent_id. Preserve applicable role names. Stop before worker start on failed preflight; report observations, not guessed lack of support. Do not bypass an approval denial.

If a reviewer fails while a worker is already waiting, update the current record from the real failure. Submit the standard envelope with result:null and an error STRING: runtime_qa_tools_missing, runtime_qa_spawn_failed, or runtime_qa_execution_failed. The old runtime_qa_unavailable alias also requires evidence and is normalized. Unsupported claims return runtime_qa_discovery_required; the worker keeps waiting and the submission file is retained. Discover the tools rather than repeating that claim.

Do not infer availability from model names, unrelated MCP server warnings, memory or create_thread. Past success/failure does not replace current discovery. Never invent a pass or disable QA. For each QA request send the exact payload.prompt and payload.packet to the prepared reviewer, then submit its actual verdict.
