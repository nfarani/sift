"""Prompt-injection fixtures. Phase 1 has no LLM, so these prove the deterministic pipeline
cannot be steered by email content; Phase 3 reuses the same fixtures against ClaudeClassifier."""
from sift.plans import PlanItem, create_plan
from sift.providers.synthetic import INJECTIONS

INJECTION_SUBJECTS = [s for _, _, s in INJECTIONS]


def test_injection_fixtures_present(db, scanned):
    with db.connect() as c:
        subjects = {r[0] for r in c.execute("SELECT subject FROM messages")}
    assert set(INJECTION_SUBJECTS) <= subjects


def test_bank_and_security_mail_stay_protected(db, scanned):
    with db.connect() as c:
        bad = c.execute("SELECT COUNT(*) FROM messages WHERE from_addr IN "
                        "('statements@monzi-bank.com','alerts@harbourbank.co.uk','security@idvault.com') "
                        "AND protected=0").fetchone()[0]
        cats = {r[0] for r in c.execute("SELECT category FROM sender_groups WHERE sender IN "
                                        "('statements@monzi-bank.com','security@idvault.com')")}
    assert bad == 0
    assert cats <= {"financial", "security"}


def test_injection_senders_cannot_claim_trusted_categories(db, scanned):
    with db.connect() as c:
        cats = {r[0]: r[1] for r in c.execute(
            "SELECT sender, category FROM sender_groups WHERE sender IN ('admin@inbox-tools.top','bot@cleanup-helper.xyz')")}
    assert set(cats.values()) <= {"spam", "uncertain"}


def test_no_plan_contains_protected_or_sent_mail_despite_injections(db, scanned):
    with db.connect() as c:
        recs = [r[0] for r in c.execute("SELECT id FROM recommendations WHERE action!='review'")]
    p = create_plan(db, [PlanItem(rec_id=r) for r in recs])
    with db.connect() as c:
        leaked = c.execute("SELECT COUNT(*) FROM plan_messages pm JOIN messages m ON m.id=pm.message_id "
                           "WHERE pm.plan_id=? AND (m.protected=1 OR m.is_sent=1 OR m.labels LIKE '%STARRED%')",
                           (p["id"],)).fetchone()[0]
        actions = {r[0] for r in c.execute("SELECT DISTINCT action FROM plan_messages WHERE plan_id=?", (p["id"],))}
    assert leaked == 0
    assert actions == {"archive"}  # nothing in email content can upgrade the default action
