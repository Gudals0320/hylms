"""Session-local display labels, separate from semantic state and QA commits."""
from __future__ import annotations

import copy
import re

from .core import HylmsError

LABEL = re.compile(r"[AB][1-9][0-9]*\Z")


def invalid(code="pending_agenda_invalid"):
    raise HylmsError(code, code)


def validate_agenda(agenda):
    if not isinstance(agenda, dict) or set(agenda) != {"schema_version", "baseline_ids", "labels"}:
        invalid()
    if type(agenda["schema_version"]) is not int or agenda["schema_version"] != 1:
        invalid()
    baseline, labels = agenda["baseline_ids"], agenda["labels"]
    if not isinstance(baseline, list) or any(not isinstance(x, str) or not x for x in baseline):
        invalid()
    if len(set(baseline)) != len(baseline) or not isinstance(labels, dict):
        invalid()
    if any(not isinstance(key, str) or not key or not isinstance(label, str) or not LABEL.fullmatch(label)
           for key, label in labels.items()):
        invalid()
    if len(set(labels.values())) != len(labels):
        invalid()
    if any(label.startswith("A") and key in baseline for key, label in labels.items()):
        invalid()


def create_agenda(baseline_ids, pending_ids):
    baseline = set(baseline_ids)
    current = set(pending_ids)
    labels = {}
    for group, ids in (("A", current - baseline), ("B", current & baseline)):
        labels.update({identity: f"{group}{index}" for index, identity in enumerate(sorted(ids), 1)})
    agenda = {"schema_version": 1, "baseline_ids": sorted(baseline), "labels": labels}
    validate_agenda(agenda)
    return agenda


def extend_agenda(agenda, pending_ids):
    """Keep retired labels reserved; unexpected/legacy entries are existing B."""
    validate_agenda(agenda)
    updated = copy.deepcopy(agenda)
    labels = updated["labels"]
    next_b = max((int(label[1:]) for label in labels.values() if label.startswith("B")), default=0)
    for identity in sorted(set(pending_ids) - set(labels)):
        next_b += 1
        labels[identity] = f"B{next_b}"
    validate_agenda(updated)
    return updated


def resolve_labels(agenda, pending_ids, labels):
    validate_agenda(agenda)
    if not isinstance(labels, list) or not labels or any(not isinstance(x, str) or not LABEL.fullmatch(x) for x in labels):
        invalid("pending_label_invalid")
    if len(set(labels)) != len(labels):
        invalid("pending_label_invalid")
    by_label = {label: identity for identity, label in agenda["labels"].items()}
    current = set(pending_ids)
    if any(label not in by_label or by_label[label] not in current for label in labels):
        invalid("pending_label_stale")
    return [by_label[label] for label in labels]
