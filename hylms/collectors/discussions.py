"""Discussion collection, privacy filtering, and graded canonical links."""

from __future__ import annotations

from typing import Any, Mapping

from ..content import clean_html, normalize_attachment, sanitize_url
from ..core import CanvasHTTPError, HylmsError, kst_iso
from ..schedule import evaluate_summary, normalize_discussion_schedule


class DiscussionCollectorMixin:
    def _collect_discussions(
        self,
        course_id: str,
        base_url: str,
        assignment_raw_by_id: Mapping[str, Mapping[str, Any]],
        assignment_records_by_id: Mapping[str, Mapping[str, Any]],
    ) -> tuple[list[dict[str, Any]], dict[str, Any], list[str]]:
        raw_topics = self.client.get_paginated(
            f"/api/v1/courses/{course_id}/discussion_topics",
            {
                "include[]": ["all_dates", "sections", "overrides"],
                "order_by": "position",
                "per_page": 100,
            },
        )
        topics = [topic for topic in raw_topics if isinstance(topic, dict) and not topic.get("is_announcement")]
        if len(topics) != len([topic for topic in raw_topics if not isinstance(topic, dict) or not topic.get("is_announcement")]):
            raise HylmsError("discussions_invalid", "Canvas 토론 목록 형식이 올바르지 않습니다.")

        discussions: list[dict[str, Any]] = []
        warnings: list[str] = []
        restricted_count = 0
        detail_failed_count = 0
        graded_count = 0
        linked_count = 0
        for listed in topics:
            if listed.get("id") is None:
                raise HylmsError("discussions_invalid", "Canvas 토론 ID가 누락됐습니다.")
            topic_id = str(listed["id"])
            detail = dict(listed)
            detail_state = "collected"
            detail_reason = None
            topic_detail_failed = False
            try:
                payload = self.client.get_json(
                    f"/api/v1/courses/{course_id}/discussion_topics/{topic_id}",
                    {"include[]": ["all_dates", "sections", "overrides"]},
                )
                if not isinstance(payload, dict):
                    raise HylmsError("discussion_detail_invalid", "Canvas 토론 상세 형식이 올바르지 않습니다.")
                detail.update(payload)
            except CanvasHTTPError as exc:
                if exc.status in {401, 403}:
                    detail_state = "restricted"
                    detail_reason = exc.reason or "provider_restricted"
                    restricted_count += 1
                elif exc.status == 404:
                    detail_state = "not_available"
                    detail_reason = "endpoint_404"
                    restricted_count += 1
                else:
                    detail_state = "unavailable"
                    detail_reason = "detail_request_failed"
                    topic_detail_failed = True
                    warnings.append("discussion_detail_failed")
            except HylmsError:
                detail_state = "unavailable"
                detail_reason = "detail_response_invalid"
                topic_detail_failed = True
                warnings.append("discussion_detail_failed")

            assignment_id_value = detail.get("assignment_id")
            if assignment_id_value is None and isinstance(detail.get("assignment"), dict):
                assignment_id_value = detail["assignment"].get("id")
            assignment_id = str(assignment_id_value) if assignment_id_value is not None else None
            assignment_raw = assignment_raw_by_id.get(assignment_id or "")
            assignment_record = assignment_records_by_id.get(assignment_id or "")
            if assignment_id:
                graded_count += 1
                if assignment_raw is not None:
                    linked_count += 1

            prompt, _ = clean_html(detail.get("message"), detail.get("html_url") or base_url)
            attachment_raw: list[Mapping[str, Any]] = []
            if isinstance(detail.get("attachment"), dict):
                attachment_raw.append(detail["attachment"])
            attachment_raw.extend(
                item for item in detail.get("attachments") or [] if isinstance(item, dict)
            )
            attachments = [normalize_attachment(item, detail.get("html_url") or base_url) for item in attachment_raw]
            entries: dict[str, Any]
            if detail_state in {"restricted", "not_available"}:
                entries = {"state": detail_state, "reason": detail_reason, "items": []}
            elif detail.get("require_initial_post") is True and detail.get("user_can_see_posts") is False:
                entries = {"state": "restricted", "reason": "initial_post_required", "items": []}
                restricted_count += 1
            elif detail.get("discussion_subentry_count") == 0:
                entries = {"state": "collected", "reason": None, "items": []}
            else:
                try:
                    view = self.client.get_json(
                        f"/api/v1/courses/{course_id}/discussion_topics/{topic_id}/view",
                        {"include_new_entries": 1},
                    )
                    entries = self._privacy_filter_discussion_view(
                        view, detail.get("html_url") or base_url
                    )
                except CanvasHTTPError as exc:
                    if exc.status in {401, 403, 404}:
                        reason = exc.reason or "provider_restricted"
                        entries = {"state": "restricted", "reason": reason, "items": []}
                        restricted_count += 1
                    else:
                        entries = {"state": "unavailable", "reason": "detail_request_failed", "items": []}
                        topic_detail_failed = True
                        warnings.append("discussion_detail_failed")
                except HylmsError:
                    entries = {"state": "unavailable", "reason": "detail_response_invalid", "items": []}
                    topic_detail_failed = True
                    warnings.append("discussion_detail_failed")

            if entries["state"] == "unavailable":
                detail_state = "unavailable"
                detail_reason = entries.get("reason")
            elif entries["state"] == "restricted" and detail_state == "collected":
                detail_state = "restricted"
                detail_reason = entries.get("reason")
            if topic_detail_failed:
                detail_failed_count += 1

            schedule = normalize_discussion_schedule(detail, assignment_raw, self.user_id)
            locked = bool(detail.get("locked_for_user"))
            if locked:
                access_state = "locked"
                access_reason = detail.get("lock_explanation") or "locked_for_user"
            elif detail_state in {"restricted", "not_available"} or entries["state"] == "restricted":
                access_state = "restricted"
                access_reason = entries.get("reason") or detail_reason
            else:
                access_state = "available"
                access_reason = None
            sections = [
                {
                    "id": str(section["id"]) if section.get("id") is not None else None,
                    "name": section.get("name"),
                }
                for section in detail.get("sections") or []
                if isinstance(section, dict)
            ]
            submission_for_summary = (
                assignment_record.get("submission")
                if isinstance(assignment_record, dict)
                else None
            )
            discussions.append(
                {
                    "id": topic_id,
                    "assignment_id": assignment_id,
                    "assignment_group_id": str(assignment_raw["assignment_group_id"])
                    if assignment_raw and assignment_raw.get("assignment_group_id") is not None
                    else None,
                    "title": detail.get("title"),
                    "prompt": prompt,
                    "links": {"body": list(prompt["links"]), "attachments": attachments},
                    "source_url": sanitize_url(
                        detail.get("html_url") or f"{base_url}/discussion_topics/{topic_id}", base_url
                    ),
                    "position": detail.get("position"),
                    "discussion_type": detail.get("discussion_type"),
                    "posted_at": kst_iso(detail.get("posted_at")),
                    "delayed_post_at": kst_iso(detail.get("delayed_post_at")),
                    "last_reply_at": kst_iso(detail.get("last_reply_at")),
                    "published": detail.get("published"),
                    "require_initial_post": detail.get("require_initial_post"),
                    "user_can_see_posts": detail.get("user_can_see_posts"),
                    "sections": sections,
                    "read_state": detail.get("read_state"),
                    "unread_count": detail.get("unread_count"),
                    "reply_count": detail.get("discussion_subentry_count"),
                    "subscribed": detail.get("subscribed"),
                    "points_possible": assignment_raw.get("points_possible") if assignment_raw else None,
                    "schedule": schedule,
                    "access": {"state": access_state, "reason": access_reason},
                    "summary_state": evaluate_summary(schedule, submission_for_summary, self.now),
                    "submission": assignment_record.get("submission")
                    if isinstance(assignment_record, dict)
                    else None,
                    "detail_state": detail_state,
                    "detail_reason": detail_reason,
                    "entries": entries,
                }
            )

        normalized_count = len(discussions)
        completeness = "with_warnings" if detail_failed_count else "restricted" if restricted_count else "complete"
        source = {
            "status": "collected" if topics else "empty",
            "discovered_count": len(topics),
            "normalized_count": normalized_count,
            "graded_count": graded_count,
            "linked_count": linked_count,
            "restricted_count": restricted_count,
            "detail_failed_count": detail_failed_count,
            "completeness": completeness,
        }
        if normalized_count != len(topics):
            raise HylmsError("discussions_incomplete", "Canvas 토론 정규화 수가 발견 수와 일치하지 않습니다.")
        return discussions, source, list(dict.fromkeys(warnings))

    def _privacy_filter_discussion_view(self, payload: Any, base_url: str) -> dict[str, Any]:
        if not isinstance(payload, dict) or not isinstance(payload.get("view"), list):
            raise HylmsError("discussion_view_invalid", "Canvas 토론 view 형식이 올바르지 않습니다.")
        participants = {
            str(item["id"]): item.get("display_name")
            for item in payload.get("participants") or []
            if isinstance(item, dict) and item.get("id") is not None
        }
        unread_ids = {str(value) for value in payload.get("unread_entries") or []}
        nodes: dict[str, Mapping[str, Any]] = {}
        parents: dict[str, str | None] = {}
        children: dict[str, list[str]] = {}

        def visit(
            node: Any, inherited_parent: str | None = None, *, prefer_new: bool = False
        ) -> None:
            if not isinstance(node, dict) or node.get("id") is None:
                return
            node_id = str(node["id"])
            parent_value = node.get("parent_id")
            parent_id = str(parent_value) if parent_value is not None else inherited_parent
            if prefer_new and node_id in nodes:
                merged = dict(nodes[node_id])
                merged.update(node)
                if "replies" not in node and "replies" in nodes[node_id]:
                    merged["replies"] = nodes[node_id]["replies"]
                nodes[node_id] = merged
            else:
                nodes.setdefault(node_id, node)
            parents.setdefault(node_id, parent_id)
            if parent_id is not None:
                children.setdefault(parent_id, [])
                if node_id not in children[parent_id]:
                    children[parent_id].append(node_id)
            for reply in node.get("replies") or []:
                visit(reply, node_id, prefer_new=prefer_new)

        for node in payload["view"]:
            visit(node)
        new_entries = payload.get("new_entries") or []
        if not isinstance(new_entries, list):
            raise HylmsError("discussion_view_invalid", "Canvas 토론 new_entries 형식이 올바르지 않습니다.")
        for node in new_entries:
            visit(node, prefer_new=True)

        own_items: list[dict[str, Any]] = []
        for node_id, node in nodes.items():
            if str(node.get("user_id")) != self.user_id:
                continue
            body, _ = clean_html(node.get("message"), base_url)
            feedback_entries: list[dict[str, Any]] = []
            for child_id in children.get(node_id, []):
                child = nodes[child_id]
                child_body, _ = clean_html(child.get("message"), base_url)
                child_user_id = str(child.get("user_id")) if child.get("user_id") is not None else None
                feedback_entries.append(
                    {
                        "id": child_id,
                        "parent_id": node_id,
                        "body": child_body,
                        "created_at": kst_iso(child.get("created_at")),
                        "updated_at": kst_iso(child.get("updated_at")),
                        "author_display_name": participants.get(child_user_id)
                        if child_user_id and child_user_id != self.user_id
                        else None,
                    }
                )
            own_items.append(
                {
                    "id": node_id,
                    "parent_id": parents.get(node_id),
                    "body": body,
                    "created_at": kst_iso(node.get("created_at")),
                    "updated_at": kst_iso(node.get("updated_at")),
                    "read_state": "unread" if node_id in unread_ids else "read",
                    "feedback_entries": feedback_entries,
                }
            )
        own_items.sort(key=lambda item: (item.get("created_at") or "", item["id"]))
        return {"state": "collected", "reason": None, "items": own_items}

    @staticmethod
    def _reconcile_discussions(
        groups: list[dict[str, Any]],
        assignments: list[dict[str, Any]],
        discussions: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        by_assignment = {
            item["assignment_id"]: item
            for item in discussions
            if item.get("assignment_id") is not None
        }
        linked_assignments: set[str] = set()
        for group in groups:
            for item in group["items"]:
                assignment_id = str(item.get("assignment_id") or item.get("id"))
                discussion = by_assignment.get(assignment_id)
                if discussion is None:
                    continue
                item.clear()
                item.update(
                    {
                        "kind": "discussion",
                        "id": discussion["id"],
                        "assignment_id": assignment_id,
                    }
                )
                linked_assignments.add(assignment_id)
        missing = set(by_assignment) - linked_assignments
        if missing:
            raise HylmsError(
                "discussion_assignment_link_missing",
                "graded discussion의 assignment group canonical 연결이 누락됐습니다.",
            )
        return [item for item in assignments if item["id"] not in by_assignment]
