---
name: search-strategy
description: Formulate web search queries that surface primary sources and dissent rather than SEO content. Use when searching the web for evidence on a research subquestion, when initial searches return only blogs or listicles, or when you need to find the strongest counter-evidence to a claim.
---

# Search strategy

Most shallow research is a search-query failure, not a reasoning failure. One
natural-language query returns the SEO layer: listicles, vendor blogs, and
aggregators summarizing each other. Primary sources require deliberate query
construction.

## Run a query ladder, not a query

For each subquestion, issue searches at several levels before reading anything:

1. **Naive** — the question as a person would type it. Establishes what the
   consensus narrative is, and who is repeating it.
2. **Technical** — the same question in the vocabulary the field actually uses.
   Replace popular terms with the terms of art (not "AI chip shortage" but
   "HBM allocation constraint", not "brain training" but "far transfer").
   This is the single highest-yield move; the SEO layer does not use jargon.
3. **Primary-source targeted** — add the artifact type that would contain the
   answer: `filetype:pdf`, `site:arxiv.org`, `site:*.gov`, "annual report",
   "randomized", "pre-registered", "dataset", "docket", "transcript".
4. **Dissent-targeted** — search explicitly for the counter-position:
   "failed to replicate", "criticism of", "overstated", "retraction",
   "we disagree", "methodological problems with", "corrigendum".
5. **Quantity-targeted** — if a number is at stake, search for the number's
   source rather than the number: "methodology", "how we calculated",
   "survey instrument", "sample size", "margin of error".

## Operators worth using

- `site:` to force a domain class — `site:.gov`, `site:.edu`, `site:arxiv.org`
- `filetype:pdf` surfaces papers, filings, and reports over web pages
- `-site:` to exclude an aggregator that dominates results
- Quoted phrases for exact claim text — the fastest way to find everyone who
  copied one original, which tells you the claim has one source, not many
- A distinctive phrase from a good source, re-searched, finds its citers and
  its critics

## Finding the original

When several sources agree, assume they share one origin until shown
otherwise. To find it: take the most specific sentence or figure they share,
search it as an exact quoted phrase, and sort by date. The earliest hit is
usually the original; everything later is often a restatement. Report the
original as the source, and note that the others are not independent.

## Stop conditions

Stop searching a subquestion when:

- two independently-originated sources agree and you have found the strongest
  available dissent, or
- three successive query levels return only material you have already read, or
- the question turns out to be unanswerable from public sources — say so
  explicitly, which is a finding, not a failure.

Do not stop merely because the first page of results looks consistent.
Consistency on page one is what the SEO layer produces by construction.

## Fetch discipline

Search snippets are not evidence. A snippet tells you a page may be relevant;
it does not tell you what the page claims, how it hedged, or what its
methodology was. Fetch any page you intend to cite. If a page cannot be
fetched, do not cite it from its snippet — note it as unverified instead.
