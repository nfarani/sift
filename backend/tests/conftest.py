import pytest

from sift.classifier.rules import RuleOnlyClassifier
from sift.db import Database
from sift.executor import Executor
from sift.plans import PlanItem, approve_plan, create_plan
from sift.providers.mock import MockProvider
from sift.retry import RetryPolicy
from sift.scanner import Scanner


class Sleeps(list):
    def __call__(self, s):
        self.append(s)


@pytest.fixture(autouse=True)
def no_real_anthropic_key(monkeypatch):
    """sift.config loads the repo .env; tests must never see (or spend on) the real key."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)


@pytest.fixture
def sleeps():
    return Sleeps()


@pytest.fixture
def retry(sleeps):
    return RetryPolicy(max_attempts=4, base=0.5, cap=30, sleep=sleeps, jitter=False)


@pytest.fixture
def provider(tmp_path):
    return MockProvider(tmp_path / "mock.sqlite", size=3000, seed=7, latency_ms=0)


@pytest.fixture
def db(tmp_path):
    return Database(tmp_path / "app.sqlite")


@pytest.fixture
def scanner(db, provider, retry):
    return Scanner(db, provider, RuleOnlyClassifier(), retry, page_size=200, meta_batch=50)


@pytest.fixture
def scanned(scanner):
    sid = scanner.start()
    assert scanner.run(sid) == "completed"
    return sid


@pytest.fixture
def executor(db, provider, retry):
    return Executor(db, provider, provider, first_run_cap=50, retry=retry, chunk=100)


@pytest.fixture
def unlocked(db):
    db.set_setting("cleanup.full_runs_unlocked", True)


def approved_plan(db, rec="promotions", action="archive", older=0, excluded=()):
    p = create_plan(db, [PlanItem(rec_id=rec, action=action, older_than_days=older, excluded_senders=list(excluded))])
    return approve_plan(db, p["id"], p["ids_hash"])


def plan_ids(db, plan_id):
    with db.connect() as c:
        return [r[0] for r in c.execute("SELECT message_id FROM plan_messages WHERE plan_id=?", (plan_id,))]


def labels_of(provider, ids):
    return {m.id: m.labels for m in provider.get_metadata(list(ids))}
