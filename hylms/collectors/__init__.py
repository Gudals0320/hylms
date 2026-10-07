"""Collector responsibility mixins."""

from .announcements import AnnouncementCollectorMixin
from .assignments import AssignmentCollectorMixin
from .discussions import DiscussionCollectorMixin
from .quizzes import QuizCollectorMixin

__all__ = [
    "AnnouncementCollectorMixin",
    "AssignmentCollectorMixin",
    "DiscussionCollectorMixin",
    "QuizCollectorMixin",
]
