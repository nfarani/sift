"""Mail provider interface. Gmail (Phase 2) and Outlook (later) slot in behind this.

Read methods are used by the scanner and by executor revalidation. Write methods
(`ensure_label`, `batch_modify`, `trash`, `untrash`) must only ever be called from
`sift.executor`. There is deliberately no delete, send or draft method.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field


class ProviderError(Exception):
    """Base for provider failures."""


class TransientError(ProviderError):
    """Retryable failure (HTTP 429 or 5xx)."""

    def __init__(self, status: int, message: str = ""):
        super().__init__(f"HTTP {status} {message}".strip())
        self.status = status


class PermanentError(ProviderError):
    """Non-retryable failure (4xx other than 429)."""


@dataclass
class MessageMeta:
    id: str
    thread_id: str
    from_addr: str
    from_name: str
    to_addrs: list[str]
    subject: str
    snippet: str
    internal_date: int  # epoch ms
    labels: list[str] = field(default_factory=list)
    list_id: str | None = None
    list_unsubscribe: str | None = None
    list_unsubscribe_post: str | None = None

    @property
    def from_domain(self) -> str:
        return self.from_addr.rsplit("@", 1)[-1].lower()


@dataclass
class Page:
    ids: list[str]
    next_page_token: str | None
    result_size_estimate: int | None = None


class MailProvider(ABC):
    name: str = "base"
    synthetic: bool = False
    max_modify_batch: int = 1000  # Gmail batchModify limit

    @abstractmethod
    def account_email(self) -> str: ...

    @abstractmethod
    def profile(self) -> dict:
        """{'email', 'messages_total', 'threads_total'}"""

    @abstractmethod
    def count_label(self, label: str) -> int:
        """Total messages carrying a label (Gmail: labels.get -> messagesTotal)."""

    @abstractmethod
    def list_message_ids(self, label: str, page_token: str | None = None, max_results: int = 500) -> Page: ...

    @abstractmethod
    def get_metadata(self, ids: list[str]) -> list[MessageMeta]:
        """Metadata only, never bodies. Unknown ids are omitted from the result. Must not mark read."""

    # --- write methods: executor only ------------------------------------
    @abstractmethod
    def ensure_label(self, name: str) -> str:
        """Create the label if missing; return its id."""

    @abstractmethod
    def batch_modify(self, ids: list[str], add: list[str], remove: list[str]) -> None: ...

    @abstractmethod
    def trash(self, message_id: str) -> None: ...

    @abstractmethod
    def untrash(self, message_id: str) -> None: ...
