# Same-task manual corrections

Clarify user intent and select current target IDs first. The worker provides `packet`, current `entities`, approved `rules`, and optional previous decision/feedback. Manual work requires a completed pipeline in this task.

The helper accepts target_labels such as A1/B2 as an alternative to target_ids and resolves them against the session's stable mapping and live pending set. The model still emits real entity IDs, never display labels. Use the supplied `target_labels` mapping to interpret labelled answers. QA receives the identical label binding alongside its existing packet. Do not reinterpret a retired/unknown label as another item.

Return exactly:

```json
{"schema_version":1,"transaction_id":"packet.transaction.id","reason":"사용자 지시에 근거한 수정","operations":[{"op":"set_user_action_state","id":"user-authority event ID","state":"done","evidence":["user_confirmed"]}]}
```

Manual operations differ from automatic operation wrappers:

- `upsert_event`: exactly `op,value,confirmed_fields`. `value` is the full event from [decision shapes](decisions.md). `confirmed_fields` is a nonempty array containing all changed user-confirmable fields, such as `timing.start`, `timing.end`, `location` or `title`. Preserve the existing `user_confirmed` map; the engine writes timestamps. Do not include `action_state` here.
- `cancel_event`: exactly `op,id,evidence` (no source_record_ids field).
- `resolve_pending`: exactly `op,id`; ID must be a selected current pending.
- `upsert_announcement_application`: exactly `op,value`, using the complete application shape.
- `set_user_action_state`: exactly `op,id,state,evidence` as above.

`upsert_pending` uses exactly `op,value` and can only reclassify an existing selected pending of the same course while preserving its source IDs. Use resolution_owner=source for facts awaiting future LMS notices. Stay within selected targets/their supported resolution. A new event/application is allowed only while also resolving targeted pending in the same transaction. New event `user_confirmed` starts `{}` and action completion starts `unknown`. Keep the pending until all information required for its resolution has been established.

Only user-authority actions accept completion; reversal uses `state:"unknown"`. Never override LMS completion. The engine records confirmation timestamps; do not manufacture or rewrite those markers.

Every mutation receives candidate-bound QA, with one revision. Pending/failed QA does not save. Technical evidence failures remain internal; ask the user only for actual user-owned missing information. Cursor and failure history stay unchanged. No snapshot/ntfy/ICS/Google calls follow manual commit; the next new-task `$hylms` reconciles outputs.

Successful saves return a refreshed A/B agenda with unchanged labels. "I don't know" or deferral alone does not resolve an item or require a semantic commit. Keep it pending, stop repeating it immediately in this conversation, and ask it again with all B items on the next invocation. Personal state remains outside Git.

The pipeline may issue a manual-shaped interpretation request during internal context recovery before collection. Its instruction is the persisted user policy, its targets are existing technical review items, and its structured_evidence/course_context are supplied by the engine. This does not authorize unrelated state edits or external calls. Return a decision only; the engine requires independent QA before committing. New events mark only supplied confirmed_fields as user-confirmed.
