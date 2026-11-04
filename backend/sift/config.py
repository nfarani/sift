"""Runtime configuration, read from the environment (and the repo-root .env if present).

Secrets are never logged: `public_status()` reports only whether a value is set.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parents[2]
load_dotenv(REPO_ROOT / ".env", override=False)


def _int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "") or default)
    except ValueError:
        return default


def _bool(name: str, default: bool) -> bool:
    v = os.environ.get(name, "").strip().lower()
    if not v:
        return default
    return v in ("1", "true", "yes", "on")


@dataclass
class Settings:
    data_dir: Path = field(default_factory=lambda: Path(os.environ.get("SIFT_DATA_DIR", REPO_ROOT / "data")))
    provider: str = field(default_factory=lambda: os.environ.get("SIFT_PROVIDER", "mock"))
    mock_size: int = field(default_factory=lambda: _int("SIFT_MOCK_SIZE", 45_000))
    mock_seed: int = field(default_factory=lambda: _int("SIFT_MOCK_SEED", 1234))
    # Simulated per-request latency so scan/execution progress is observable in the UI.
    mock_latency_ms: int = field(default_factory=lambda: _int("SIFT_MOCK_LATENCY_MS", 15))
    first_run_cap: int = field(default_factory=lambda: _int("SIFT_FIRST_RUN_CAP", 50))
    demo_mode: bool = field(default_factory=lambda: _bool("SIFT_DEMO_MODE", False))
    api_port: int = field(default_factory=lambda: _int("SIFT_API_PORT", 8765))
    # Hosts allowed to talk to the API (DNS-rebinding guard). Ports are added from api_port/ui_port.
    extra_allowed_hosts: tuple[str, ...] = ()
    ui_port: int = field(default_factory=lambda: _int("SIFT_UI_PORT", 5173))
    spend_cap_usd: float = field(default_factory=lambda: float(os.environ.get("SIFT_AI_SPEND_CAP_USD", "") or 2.0))
    model_bulk: str = field(default_factory=lambda: os.environ.get("SIFT_MODEL_BULK", "").strip() or "claude-haiku-4-5")
    model_review: str = field(default_factory=lambda: os.environ.get("SIFT_MODEL_REVIEW", "").strip() or "claude-opus-5-5")
    anthropic_key_set: bool = field(default_factory=lambda: bool(os.environ.get("ANTHROPIC_API_KEY", "").strip()))
    # Backoff: base delay in seconds, doubled each attempt, capped.
    backoff_base: float = 0.5
    backoff_max: float = 30.0
    max_attempts: int = 6

    @property
    def allowed_hosts(self) -> set[str]:
        hosts = {f"{h}:{p}" for h in ("127.0.0.1", "localhost") for p in (self.api_port, self.ui_port)}
        return hosts | set(self.extra_allowed_hosts)

    @property
    def allowed_origins(self) -> set[str]:
        return {f"http://{h}" for h in self.allowed_hosts}

    client_secrets: Path = field(default_factory=lambda: Path(
        os.environ.get("GOOGLE_CLIENT_SECRETS", "~/.sift/client_secret.json")).expanduser())

    @property
    def app_db(self) -> Path:
        # Synthetic and real data never share a database.
        return self.data_dir / ("sift.sqlite" if self.provider == "mock" else f"sift-{self.provider}.sqlite")

    @property
    def mock_db(self) -> Path:
        return self.data_dir / "mock_mailbox.sqlite"

    def public_status(self) -> dict:
        return {
            "provider": self.provider,
            "synthetic": self.provider == "mock",
            "client_secrets_present": self.client_secrets.exists() if self.provider == "gmail" else None,
            "anthropic_api_key": "set" if self.anthropic_key_set else "not set",
            "spend_cap_usd": self.spend_cap_usd,
            "models": {"bulk": self.model_bulk, "review": self.model_review},
            "first_run_cap": self.first_run_cap,
            "demo_mode": self.demo_mode,
        }
