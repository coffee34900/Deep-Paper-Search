---
name: claim-extraction
description: Turn a fetched source into atomic, quote-backed claims without drifting beyond what the text supports. Use when extracting findings from a fetched page, when a sentence bundles several assertions, or when deciding how to word a claim so it stays faithful to its quote.
---

# Claim extraction

A claim is an assertion that one named source supports and that could, in
principle, be shown false. Extraction quality determines everything
downstream: contradictions can only be detected between claims that are
atomic, and verification can only check claims that carry quotes.

## Atomicity

Split any sentence that asserts more than one thing. Compound sentences hide
conflicts, because a later source may contradict one half while agreeing with
the other.

> "Revenue grew 12% in 2024, driven by subscription growth in Europe."

is three claims:

1. Revenue grew 12% in 2024.
2. Subscription growth contributed to that revenue growth.
3. The subscription growth was concentrated in Europe.

The causal claim (2) is the one most likely to be the source's
interpretation rather than its data. Separating it is what lets the
contradiction check work.

Split on: conjunctions, "because"/"driven by"/"due to", "and therefore",
appositives, and any sentence containing both a number and an explanation.

## Quote discipline

Every claim carries a verbatim quote from the source. The rule: **if you
cannot quote it, you cannot claim it.** Paraphrase in the claim text if you
like, but the quote must independently support the paraphrase.

Then check your wording against the quote for drift — the most common
extraction error, and almost always in the direction of overstatement:

| Source says | Do not write |
| --- | --- |
| "may", "could", "suggests", "is associated with" | "does", "shows", "causes" |
| "in this sample", "among participants" | "in general", "people" |
| "up to 40%" | "40%" |
| "one of the factors" | "the reason" |
| "preliminary", "early results" | stated as settled |
| "correlated with" | "leads to", "drives" |

Preserve hedges, scope limits, and populations in the claim text. A claim
that drops the hedge is a different, stronger claim than the source made, and
it will be reported as the source's.

## No synthesis at extraction time

Do not combine two sources into one claim. Do not compute a figure the source
did not state. Do not resolve a conflict you notice — record both claims and
let the reconciliation step handle it. Extraction reports what sources say;
inferring is a separate step with different evidence standards.

If you notice a conflict while extracting, that is valuable — but record it as
two claims, not one hedged one.

## Record what the source attributes

If the source is itself reporting someone else's finding, the claim's subject
is "Source X reports that Y found Z", and the source URL is X while the
underlying origin is noted. This keeps the independence check possible later.
Collapsing it to "Z" destroys the provenance chain.

## Calibrating support

`support` is your confidence that **the quote genuinely supports the claim as
you worded it** — not your confidence that the claim is true, and not the
source's quality. Those are tracked separately.

- `0.9+` — the quote states the claim almost verbatim
- `0.6-0.8` — the quote supports it, but you compressed or paraphrased
- `0.4-0.5` — the quote implies it; a careful reader might disagree
- below `0.4` — do not record it; you are inferring, not extracting

## Deliberate disconfirmation

After extracting the claims that answer the subquestion, extract the claims
that undermine it with equal care. A worker returning only confirming claims
has failed even when every claim is accurate, because the orchestrator cannot
detect a conflict it was never shown. Spend real effort here: search for the
counter-position explicitly, and give contrary evidence the same quote
discipline.
