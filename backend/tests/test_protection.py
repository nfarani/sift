import pytest

from sift.protection import ProtectionContext, protection_reason

ME = "you@sift-demo.test"


def reason(labels=(), thread="t1", sender="promo@shop.example", subject="Big sale", **ctx):
    c = ProtectionContext(user_email=ME, **ctx)
    return protection_reason(labels=list(labels), thread_id=thread, from_addr=sender, subject=subject, ctx=c)


def test_starred_is_protected():
    assert reason(labels=["INBOX", "STARRED"]) == "starred"


def test_thread_with_sent_reply_is_protected():
    assert reason(thread="t9", sent_threads={"t9"}) == "thread contains your reply"


def test_sender_you_emailed_is_protected():
    assert reason(sender="friend@mail.test", sent_recipients={"friend@mail.test"}) == "sender you have emailed"


@pytest.mark.parametrize("subject", [
    "Your receipt from Crumb & Co", "Invoice #4411", "Order confirmation #12345", "Your order has shipped",
    "Your booking confirmation: LHR → FCO", "Your boarding pass", "Your itinerary", "Reservation confirmed",
    "Password reset request", "Your verification code is 123456", "Security alert: new sign-in on Mac",
    "Two-factor authentication enabled", "Your statement is ready", "Payment received: £20", "Direct debit notice",
])
def test_transactional_subject_patterns(subject):
    assert reason(subject=subject) is not None


@pytest.mark.parametrize("subject", ["Up to 50% off running shoes", "12 new jobs for 'Data Analyst'",
                                     "Your next order: 20% off", "Dev Digest #42", "Trending in London"])
def test_marketing_subjects_not_protected(subject):
    assert reason(subject=subject) is None


def test_user_rules():
    assert reason(sender="a@keep.example", protected_senders={"a@keep.example"}) == "protected sender (your rule)"
    assert reason(sender="x@keep.example", protected_domains={"keep.example"}) == "protected domain (your rule)"
    assert reason(labels=["Label_7"], protected_labels={"Label_7"}).startswith("protected label")


def test_important_label_alone_does_not_protect():
    assert reason(labels=["INBOX", "IMPORTANT"]) is None


def test_list_unsubscribe_is_not_evidence_of_disposable():
    # Protection ignores headers; a receipt from a list sender is still protected.
    assert reason(sender="deals@voltmart.com", subject="Your VoltMart order #55555 has shipped") is not None


def test_scan_protects_receipts_from_promo_sender_and_replied_threads(db, scanned):
    with db.connect() as c:
        rows = c.execute("SELECT subject, protected, category FROM messages WHERE from_addr='deals@voltmart.com'").fetchall()
        replied = c.execute("SELECT COUNT(*) FROM messages m WHERE is_sent=0 AND protected=0 AND thread_id IN "
                            "(SELECT thread_id FROM messages WHERE is_sent=1)").fetchone()[0]
        starred_unprot = c.execute("SELECT COUNT(*) FROM messages WHERE labels LIKE '%STARRED%' AND protected=0 "
                                   "AND is_sent=0").fetchone()[0]
    receipts = [r for r in rows if "order" in r["subject"].lower() and "#" in r["subject"]]
    assert receipts and all(r["protected"] for r in receipts)
    assert any(not r["protected"] for r in rows)  # promos from the same sender are not
    assert replied == 0
    assert starred_unprot == 0


def test_protected_messages_never_enter_a_plan(db, scanned):
    from sift.plans import PlanItem, create_plan
    for rec in ("promotions", "notifications", "job_alerts", "newsletters", "social"):
        p = create_plan(db, [PlanItem(rec_id=rec)])
        with db.connect() as c:
            n = c.execute("SELECT COUNT(*) FROM plan_messages pm JOIN messages m ON m.id=pm.message_id "
                          "WHERE pm.plan_id=? AND m.protected=1", (p["id"],)).fetchone()[0]
        assert n == 0
