"""RuleOnlyClassifier: deterministic sender-group heuristics. No AI, no network.

Shown in the UI as "Rules only, no AI". Anything it cannot settle confidently is
labelled `uncertain` and routed to manual review rather than guessed.
"""
from __future__ import annotations

import re

from .base import Classification, Classifier, SenderGroup

_JOB = re.compile(r"\b(jobs?|hiring|roles?|salary|recruiters?|career)\b", re.I)
_APPLY = re.compile(r"\b(appl(y|ying|ication)|interview|offer letter)\b", re.I)
_PROMO = re.compile(r"(\d+%\s*off|\bsale\b|\bdeals?\b|\boffers?\b|free delivery|last chance|members|restocked|"
                    r"new in\b|gift with purchase|from £\d+|getaway|come back|price drop|black friday|early access|3 for 2)", re.I)
_NEWS = re.compile(r"(digest|newsletter|edition|weekly|#\d+|roundup|wrap|essays|recipes|stories|this week)", re.I)
_SPAM = re.compile(r"(won|winner|claim|prize|reward|action required|unclaimed|reschedule now|fee)", re.I)
_NOTIFY_LOCAL = re.compile(r"^(no-?reply|notifications?|notify|alerts?|calendar|updates?)@", re.I)
_SUSPECT_TLD = re.compile(r"\.(top|biz|xyz|click|loan|work)$", re.I)
_SECURITY = re.compile(r"(security|sign-?in|verification code|password|two-factor)", re.I)
_FINANCE = re.compile(r"(statement|payment received|direct debit|bank)", re.I)


def _share(subjects: list[str], pat: re.Pattern) -> float:
    return sum(1 for s in subjects if pat.search(s)) / len(subjects) if subjects else 0.0


def _top_cat(g: SenderGroup) -> tuple[str, float]:
    total = sum(g.gmail_categories.values()) or 1
    cat, n = max(g.gmail_categories.items(), key=lambda kv: kv[1], default=("", 0))
    return cat, n / total


class RuleOnlyClassifier(Classifier):
    name = "rules"
    label = "Rules only, no AI"
    uses_ai = False

    def classify(self, groups: list[SenderGroup]) -> dict[str, Classification]:
        return {g.sender: self._one(g) for g in groups}

    def _one(self, g: SenderGroup) -> Classification:
        subj = g.sample_subjects
        cat, cat_share = _top_cat(g)
        prot_share = g.protected / g.total if g.total else 0

        def c(category: str, conf: float, reason: str) -> Classification:
            return Classification(category, round(conf, 2), reason, self.name)

        if not g.user_emailed and _SUSPECT_TLD.search(g.domain) and not g.list_id:
            conf = 0.75 if _share(subj, _SPAM) >= 0.3 else 0.6
            return c("spam", conf, f"Unlisted bulk sender on a high-risk domain (.{g.domain.rsplit('.', 1)[-1]}).")
        if g.user_emailed or (prot_share > 0.8 and cat == "CATEGORY_PERSONAL" and not g.list_id):
            return c("personal", 0.9, "You have corresponded with this sender.")
        if _share(subj, _SECURITY) >= 0.5:
            return c("security", 0.9, "Subjects are sign-in alerts, codes or password notices.")
        if _share(subj, _FINANCE) >= 0.5:
            return c("financial", 0.9, "Subjects are statements and payment notices.")
        if _share(subj, _APPLY) >= 0.4:
            return c("employment", 0.85, "Subjects refer to your job applications or interviews.")
        if _share(subj, _JOB) >= 0.3 and (g.list_id or _NOTIFY_LOCAL.search(g.sender) or "jobs" in g.sender):
            return c("job_alerts", 0.9, "Automated job-board alerts sent to a mailing list, not a recruiter writing to you.")
        if cat == "CATEGORY_SOCIAL" and cat_share >= 0.6:
            return c("social", 0.85, "Gmail files this sender under Social; subjects are activity notifications.")
        if g.list_id and _share(subj, _NEWS) >= 0.4:
            return c("newsletters", 0.8, "Recurring mailing-list issues (digest/edition style subjects).")
        if cat == "CATEGORY_PROMOTIONS" and g.list_id:
            if _share(subj, _PROMO) >= 0.3:
                return c("promotions", 0.85, "Marketing list in Gmail's Promotions tab with discount/sale subjects.")
            return c("promotions", 0.65, "Marketing list in Gmail's Promotions tab.")
        if _NOTIFY_LOCAL.search(g.sender) and cat == "CATEGORY_UPDATES" and g.total >= 5:
            return c("notifications", 0.7, "High-volume automated notifications from a no-reply address.")
        return c("uncertain", 0.3, "Rules could not classify this sender confidently.")
