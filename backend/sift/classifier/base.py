"""Classifier interface. Phase 1 ships RuleOnlyClassifier; ClaudeClassifier arrives in Phase 3.

A classifier labels a sender group that protection rules did not fully settle.
Its output is data for the recommendation builder, never an instruction.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field

CATEGORIES = {
    "personal": "Personal correspondence",
    "important": "Important or urgent",
    "financial": "Financial and banking",
    "receipts": "Receipts and purchases",
    "travel": "Travel and bookings",
    "security": "Security and account access",
    "employment": "Employment and job applications",
    "job_alerts": "Job board alerts",
    "newsletters": "Newsletters",
    "promotions": "Shopping promotions",
    "advertising": "Advertising",
    "social": "Social media notifications",
    "notifications": "Automated notifications",
    "spam": "Spam",
    "uncertain": "Needs your review",
}
KEEP = {"personal", "important", "financial", "receipts", "travel", "security", "employment"}
DISPOSABLE = {"job_alerts", "newsletters", "promotions", "advertising", "social", "notifications", "spam"}


@dataclass
class SenderGroup:
    sender: str
    domain: str
    display_name: str
    total: int
    protected: int
    list_id: str | None
    gmail_categories: dict[str, int]
    sample_subjects: list[str] = field(default_factory=list)
    user_emailed: bool = False


@dataclass
class Classification:
    category: str
    confidence: float
    reason: str
    classifier: str


class Classifier(ABC):
    name: str
    label: str  # shown in the UI
    uses_ai: bool

    @abstractmethod
    def classify(self, groups: list[SenderGroup]) -> dict[str, Classification]: ...
