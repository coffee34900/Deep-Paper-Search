"""Source credibility scoring.

Scored in code, not by the model. A model asked "how credible is this source"
tends to rate anything well-written highly, and its answer drifts between
calls. A deterministic policy means two runs weigh the same source the same
way, and you can argue with the policy by reading it.

Score is a multiplier in [0, 1] used when weighing conflicting claims. It is
not a truth probability.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from urllib.parse import urlparse

from .state import Source, SourceKind

# Base weight by source type. The ordering here is the tool's epistemics made
# explicit -- edit it if you disagree.
KIND_WEIGHT: dict[SourceKind, float] = {
    SourceKind.PEER_REVIEWED: 0.90,
    SourceKind.PRIMARY_DATA: 0.88,
    SourceKind.OFFICIAL: 0.82,
    SourceKind.PREPRINT: 0.65,
    SourceKind.QUALITY_PRESS: 0.60,
    SourceKind.TRADE_PRESS: 0.45,
    SourceKind.CORPORATE: 0.35,
    SourceKind.BLOG: 0.25,
    SourceKind.FORUM: 0.15,
    SourceKind.UNKNOWN: 0.30,
}

# Domain patterns that override a model's self-reported `kind`. Workers
# misclassify constantly -- a company blog gets called `official`, a press
# release gets called `primary_data`. These are the cheap structural checks.
DOMAIN_OVERRIDES: list[tuple[re.Pattern[str], SourceKind]] = [
    (re.compile(r"\.gov(\.|$)|\.gov\.[a-z]{2}$|europa\.eu$|\.int$"), SourceKind.OFFICIAL),
    (re.compile(r"\.edu(\.|$)|\.ac\.[a-z]{2}$"), SourceKind.OFFICIAL),
    (re.compile(r"arxiv\.org|biorxiv\.org|medrxiv\.org|ssrn\.com"), SourceKind.PREPRINT),
    (re.compile(r"doi\.org|pubmed|nature\.com|science\.org|sciencedirect|springer|wiley|jstor"),
     SourceKind.PEER_REVIEWED),
    (re.compile(r"reddit\.com|news\.ycombinator|stackexchange|stackoverflow|quora\.com|x\.com|twitter\.com"),
     SourceKind.FORUM),
    (re.compile(r"medium\.com|substack\.com|wordpress\.com|blogspot\."), SourceKind.BLOG),
    (re.compile(r"prnewswire|businesswire|globenewswire"), SourceKind.CORPORATE),
]

# Half-life in years for time decay, by kind. A 2011 physics paper is still
# good; a 2011 market-size estimate is worthless. Same evidence class, very
# different shelf life -- so decay has to be per-kind, not global.
HALF_LIFE_YEARS: dict[SourceKind, float] = {
    SourceKind.PEER_REVIEWED: 12.0,
    SourceKind.PRIMARY_DATA: 4.0,
    SourceKind.OFFICIAL: 6.0,
    SourceKind.PREPRINT: 6.0,
    SourceKind.QUALITY_PRESS: 3.0,
    SourceKind.TRADE_PRESS: 2.0,
    SourceKind.CORPORATE: 2.0,
    SourceKind.BLOG: 2.0,
    SourceKind.FORUM: 1.5,
    SourceKind.UNKNOWN: 3.0,
}

FLOOR = 0.05


# Path markers that cap how authoritative a source may claim to be. A worker
# will happily label a vendor's /blog/ post `official`; the URL says otherwise.
PATH_DEMOTIONS: list[tuple[re.Pattern[str], SourceKind]] = [
    (re.compile(r"/(blog|blogs|news/blog|opinion|editorial|column)(/|$)"), SourceKind.BLOG),
    (re.compile(r"/(press|press-release|press-releases|newsroom|media-release)(/|$)"),
     SourceKind.CORPORATE),
    (re.compile(r"/(forum|forums|thread|comments|discussion)(/|$)"), SourceKind.FORUM),
]

# Kinds a path marker is allowed to override. A peer-reviewed journal article
# sitting under /news/ is still a demotion candidate; a forum post is already
# at the bottom and nothing demotes it further.
_DEMOTABLE = {
    SourceKind.PEER_REVIEWED,
    SourceKind.PRIMARY_DATA,
    SourceKind.OFFICIAL,
    SourceKind.QUALITY_PRESS,
    SourceKind.UNKNOWN,
}


def normalise_kind(source: Source) -> SourceKind:
    """Reconcile the model's self-report against the URL's own evidence.

    Domain patterns promote (arxiv.org really is a preprint server); path
    markers demote (a /blog/ post is not an official statement). Demotion runs
    second so it wins -- overclaiming is the failure mode worth defending
    against, since it inflates a weak source's weight in every later conflict.
    """
    parsed = urlparse(source.url)
    host = (parsed.hostname or "").lower()
    path = (parsed.path or "").lower()

    kind = source.kind
    for pattern, promoted in DOMAIN_OVERRIDES:
        if pattern.search(host):
            kind = promoted
            break

    if kind in _DEMOTABLE:
        for pattern, demoted in PATH_DEMOTIONS:
            if pattern.search(path):
                # Take the lower of the two weights rather than the demotion
                # blindly -- keeps the policy monotone.
                if KIND_WEIGHT[demoted] < KIND_WEIGHT[kind]:
                    return demoted
                break
    return kind


def _age_years(published: str | None) -> float | None:
    if not published:
        return None
    for fmt in ("%Y-%m-%d", "%Y-%m", "%Y"):
        try:
            dt = datetime.strptime(published[: len(fmt.replace("%Y", "2000"))], fmt)
            dt = dt.replace(tzinfo=timezone.utc)
            return max(0.0, (datetime.now(timezone.utc) - dt).days / 365.25)
        except ValueError:
            continue
    return None


def score(source: Source) -> float:
    """Assign a credibility multiplier and normalise the source's kind in place."""
    source.kind = normalise_kind(source)
    base = KIND_WEIGHT.get(source.kind, 0.30)

    age = _age_years(source.published)
    if age is not None:
        half_life = HALF_LIFE_YEARS.get(source.kind, 3.0)
        base *= 0.5 ** (age / half_life)
    else:
        # Undated sources are penalised: an undated claim cannot be checked for
        # staleness, which is itself a defect.
        base *= 0.85

    source.credibility = round(max(FLOOR, min(1.0, base)), 3)
    return source.credibility


def corroboration(claim_text_urls: list[str], sources: dict[str, Source]) -> float:
    """Combined weight of independent sources supporting one claim.

    Independence is approximated by distinct registrable domain. Two articles
    on the same domain count once -- this is the cheap defence against the
    circular-sourcing failure the verifier hunts for.
    """
    best_per_domain: dict[str, float] = {}
    for url in claim_text_urls:
        host = (urlparse(url).hostname or url).lower().removeprefix("www.")
        cred = sources[url].credibility if url in sources else 0.3
        best_per_domain[host] = max(best_per_domain.get(host, 0.0), cred)

    # Diminishing returns: the 5th independent source adds much less than the
    # 2nd. 1 - prod(1 - w) saturates toward 1 without ever reaching it.
    product = 1.0
    for weight in sorted(best_per_domain.values(), reverse=True):
        product *= 1.0 - weight
    return round(1.0 - product, 3)
