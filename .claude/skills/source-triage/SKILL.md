---
name: source-triage
description: Classify a web source by evidence type and judge its independence and recency before citing it. Use when deciding whether a fetched page is worth citing, when classifying a source as peer_reviewed/official/corporate/blog, or when several sources appear to agree and you need to check whether they are actually independent.
---

# Source triage

Classify every source before extracting claims from it. The classification
drives how much weight the claim carries later, so an inflated
classification quietly corrupts the final answer.

## Classify by what the source IS, not how it reads

A well-written corporate blog post is still a corporate blog post. Use the
narrowest honest category:

| Category | What qualifies | What does NOT |
| --- | --- | --- |
| `peer_reviewed` | Published in a journal after review; has a DOI, volume, issue | A preprint, a conference abstract, a paper on an author's own site |
| `preprint` | arXiv, bioRxiv, medRxiv, SSRN; not yet reviewed | A published version of the same work (that is `peer_reviewed`) |
| `official` | Government, regulator, standards body, central bank, court, intergovernmental body | A vendor describing its own compliance; a `.gov` press release summarizing someone else |
| `primary_data` | The dataset, filing, register, or statistical release itself | An article reporting on it |
| `quality_press` | Outlets with correction policies and named bylines | An outlet reprinting a wire story unchanged (that is the wire, one source) |
| `trade_press` | Industry publications, often advertiser-funded | — |
| `corporate` | Company blogs, press releases, whitepapers, sponsored research | — |
| `blog` | Individual or unaffiliated writing, Medium, Substack | — |
| `forum` | Reddit, HN, Stack Exchange, X, comment threads | — |

Two structural checks override how authoritative a page sounds:

- **The URL path.** A `/blog/`, `/newsroom/`, `/press-release/`, or `/opinion/`
  segment caps what the source can be, even on an authoritative domain. A post
  under `nature.com/news/blog/` is a blog, not a peer-reviewed paper.
- **Who paid.** Sponsored research, vendor-commissioned surveys, and
  industry-association reports are `corporate` regardless of how
  methodological they look. Look for a funding statement; its absence in a
  quantitative report is itself a warning.

## Independence is the check that matters most

Sources that agree are only evidence if they are independent. Before treating
agreement as corroboration, ask where each claim originated:

- Do they cite the same underlying study, dataset, or press release? Then they
  are **one** source.
- Is the shared figure phrased near-identically? Near-identical phrasing
  across outlets means copying, not convergence.
- Is the "expert" quoted in several places the same person, or affiliated with
  the same organization that produced the original?
- Same publisher, different brands, counts once.

Report the original and note the restatements as non-independent. Three
outlets covering one press release is a single data point, and presenting it
as three is the most common way a research report becomes falsely confident.

## Recency, weighted by domain

Staleness is domain-dependent, not absolute. Ask what the half-life of this
kind of claim is:

- Market sizes, prices, headcounts, model capabilities, regulatory status:
  months. A two-year-old figure is probably wrong.
- Clinical, policy, and macroeconomic estimates: a few years.
- Mathematical results, physical constants, historical facts: indefinite.

A source with **no date** is a defect in itself — it cannot be checked for
staleness. Note it and discount the claim.

## When to not cite at all

Skip a source rather than citing it weakly when: it has no date and makes a
quantitative claim; it paraphrases a source you could fetch directly instead;
it is a listicle or roundup with no original reporting; or its central claim
has no stated methodology. Citing a weak source to fill a gap is worse than
reporting the gap, because it makes the gap invisible.
