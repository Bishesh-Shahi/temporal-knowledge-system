# Temporal Knowledge System

A deterministic question-answering engine over **versioned website snapshots**. It answers with
what is true **now**, or **as of a given date**, at the correct tier, with a source citation. It
lists where the site contradicts itself and which value wins. When the site does not say, it
answers **"I don't know"**.

It was built for a hackathon. The assignment supplies five crawls of a credit union's website taken
over five months, and the crawls contradict each other. The solution plugs into the organizers'
scorer through a `build()` function with three methods: `ingest`, `answer` and `conflicts`. It uses
only the Python standard library, needs no API keys and makes no network calls.

> The website snapshots, the participant kit and the evaluation questions are **not** part of this
> repository; they are not ours to redistribute. The tests use small synthetic snapshots instead.

---

## 1. The problem

Every crawl of a website contains several kinds of mess:

| Mess | Example of the failure it causes |
|---|---|
| A rate changes, but stale copies survive on calculators, campaign landing pages and guidance pages | answering with last year's rate |
| Tables have tiers (term, balance, loan amount, model year, jumbo vs standard, personal vs business) | quoting the wrong row |
| Rate vs APY, dividend vs APY | answering with the other figure |
| Expired promotions, old press releases with outdated counts | presenting history as current |
| Questions about things the site never states | inventing a number |

The scorer **zeroes** an answer that contains any stale or wrong-tier value, even next to the right
one. Hedging by listing every number therefore fails, and precision of the *answer text* is
essential.

## 2. Core capabilities

- **Temporal views.** It answers as the site stood on any date covered by the crawls (`as_of`). The
  date can also come from the question itself ("as of May 2026").
- **Stale-copy detection.** It compares editions of the same table across pages by their in-page
  *effective dates*, so a newer rate sheet beats an older calculator copy even within the latest
  crawl.
- **Tier-exact answers.** It parses qualifier slots from the question (term, balance amount, loan
  amount, model year, rate kind, negation such as "not the jumbo", superlatives) and matches them
  against table rows.
- **Grounded narrative answers.** Counts come from the most recently *published* statement. Event
  dates are compared with the reference date ("is it still open?"). Hours, addresses, phone numbers
  and links are extracted from the pages.
- **Honest abstention.** When the evidence does not support an answer, the response has
  `confident=False` and contains no digits.
- **Contradiction report.** It produces one conflict per fact, with every observed value, the
  winner, and the rule that decided it.
- **Citations.** Every answer cites the URL, snapshot id and a supporting quote.

## 3. Architecture

```
                 ingest(snapshot)  — called oldest → newest
                        │
          ┌─────────────▼─────────────┐
          │ parse.py                  │  markdown cleanup, boilerplate & teaser removal,
          │  page → blocks            │  heading paths, tables → typed rows (+caption, effective
          └─────────────┬─────────────┘  date), "label: value" facts, text chunks, byline dates
                        │
          ┌─────────────▼─────────────┐
          │ store.py                  │  content-hash dedup; per-block bitmasks
          │  versioned evidence store │  observed / active per snapshot  →  view(k) = bit test
          │  + BM25 inverted index    │  page classes & authority (pages.py)
          └─────────────┬─────────────┘
                        │
 answer(q, as_of) ──────▼──────────────────────────────────────────────────────────────────┐
          │ query.py   intent + slots (term, amount, model year, rate kind, negation,         │
          │            superlative, embedded date, product modifiers, entities)               │
          │ engine.py  view = latest snapshot ≤ as_of                                         │
          │   ├─ rates/fees → table rows: slot filters → stale-edition removal → resolution  │
          │   ├─ hours / counts / events / links → specialised extractors                    │
          │   └─ other → BM25 passages → coverage, entity and head-noun checks → best window   │
          │ compose:   values copied from the winning evidence only; losers → conflict_note   │
          └──────────────────────────────────────────────────────────────────────────────────┘
          conflicts()  →  conflicts.py: table facts aligned across editions + repeated
                          boilerplate claims; winner by the same evidence hierarchy
```

| Module | Responsibility |
|---|---|
| `solution.py` | Contract entry point (`build()`), answer cache, `Source`/`Answer`/`Conflict` objects |
| `tks/text.py` | Markdown cleanup, tokenisation and stemming, money/term/model-year range and date parsing |
| `tks/pages.py` | URL → page class → authority prior; canonical URLs |
| `tks/parse.py` | Segmentation into table rows and text chunks; effective dates, bylines, expiry dates |
| `tks/store.py` | Versioned, deduplicated block store; snapshot views; BM25 index; derived site identity |
| `tks/query.py` | Question parsing: intents, slots, domain lexicon (synonyms and generic words) |
| `tks/engine.py` | Evidence selection, temporal resolution, answer composition, abstention |
| `tks/conflicts.py` | Contradiction detection and resolution |

**Technology stack:** Python standard library only (`re`, `dataclasses`, `hashlib`, `math`,
`collections`). No embeddings, vector database or LLM, for the reasons in §10.

## 4. Temporal knowledge representation

- **Snapshot.** Each crawl gets an index `k` in ingest order. Ingesting out of order raises an error.
- **Page version.** One version per URL per crawl. A version is re-parsed only if its content hash
  changed.
- **Block.** A table row or text chunk, identified by `(canonical URL, content signature)` and
  stored once. Each block carries:
  - `observed`: a bitmask of the crawls in which it literally appeared;
  - `active`: a bitmask of the snapshots at which it was part of the site. For each page, this is
    the latest version at or before that snapshot. A page absent from a later crawl is carried
    forward, because crawls are partial. A block removed from a re-crawled page is superseded.
  - `first_snapshot` / `last_snapshot`, which give the observed validity interval;
  - `eff_date`: the "effective as of" date of its table;
  - `published`: the article byline date;
  - `expires`: the end date of a promotion.
- **View.** `view(as_of)` is the latest snapshot taken on or before `as_of`. Testing whether a block
  belongs to a view is a single bit test. An explicit `as_of` before the first crawl returns "I
  don't know". A date that is only mentioned in the question falls back to the earliest crawl.

## 5. Retrieval

- **Table rows (rates, fees).**
  1. Hard slot filters: the term must equal the asked term; the asked balance or loan amount must
     fall inside the row's range; the asked model year must fall inside the row's band. Rows
     carrying a word the question negates are dropped.
  2. Coverage: words that name the product (a "lot" loan, a "reverse" mortgage) must match. Other
     words must match only if the site's rate catalog uses them at all; otherwise they are just
     phrasing.
  3. Scoring: IDF-weighted matches, minus a penalty for variant words the question did not ask
     for (for example *jumbo*, *Roth*, *business*).
- **Text (everything else).** Uses an inverted BM25 index (k1 = 1.2, b = 0.75) over the deduplicated
  blocks, filtered to the view. Re-ranking combines:
  - BM25 score;
  - authority;
  - IDF-weighted coverage of the question's content words;
  - whether named entities are present;
  - *answer-shape* evidence: an address, phone number, dollar amount or date when the question asks
    for one.

  The answer is the best one- or two-sentence window from the chosen page.
- **Domain lexicon.** A small, documented lexicon in `query.py`:
  - synonyms (CD ↔ certificate, mortgage ↔ conventional, SUV → auto);
  - generic words that never gate coverage;
  - number words ("two-year" → 24 months).

## 6. Conflict resolution

**Editions of a table.** Two tables on different pages, or in different crawls, are treated as
editions of the same table (`same_table_edition`) when all of these hold:

- they are both personal or both business products;
- their product context overlaps;
- their rows correspond, in one of two ways:
  - labels that carry product content match on *shape*, with numbers abstracted, so shifting
    model-year bands still align;
  - otherwise rows match on their *exact tiers*, and the two tables name the same product (or one
    of them lists bare tiers).

Within a view, an edition with an older effective date is **superseded**.

**Evidence hierarchy.** It is applied by both answers and `conflicts()`:

1. Newest explicit effective date.
2. Authority of the page class.
3. Most recent observation.

Narrative claims are ordered by publish date. Expired promotions are excluded.

**Undecided cases.** If evidence remains tied with different values, the answer discloses the
competing values in `conflict_note`.

**`conflicts()` output.** One record per fact: `topic`, all `values` observed across pages and
crawls, `resolution` and a human-readable `reason`. Example reason: *"newest effective date wins:
&lt;newer date&gt; on the rates page … supersedes &lt;older date&gt;; stale copies remain on
calculator pages"*.

## 7. Citations

Every answer lists up to four `Source(url, snapshot_id, quote)` entries, taken from the blocks the
answer text was composed from:

- the winning row, plus other pages carrying the same row and value;
- for link answers, the target page itself.

URLs in answer text are only ones that appear in the pages. The scorer penalises invented links.

## 8. Uncertainty and abstention

The refusal answer has `confident=False`, and its text contains **no digits**: the scorer fails a
refusal that mentions any number. Abstention triggers:

- no table row matches the product named in the question;
- the question's term, tier or model year matches no row;
- a named entity (a branch or person) is absent from the best evidence;
- the head noun of the question is absent (a question about a "withdrawal *limit*" is not answered
  with an overdraft fee);
- passage coverage is below 0.5;
- `as_of` predates the first crawl.

The thresholds were calibrated on a held-out development set rather than on the public questions
(see §11).

## 9. Performance and complexity

Measured with `scripts/benchmark.py` on one development laptop (Windows, Python 3.14) over the
five crawls (3,294 page versions). These are single-machine measurements, not guarantees:

| Measure | Value |
|---|---|
| Ingest, all five crawls (untimed by the scorer; must be < 30 min) | **6.8 s** |
| Unique blocks after de-duplication | 15,028 (2,841 table rows), 10,061 index terms |
| Peak traced memory during ingest (`tracemalloc`) | 116 MB |
| Answer latency over 116 questions: mean / p50 / p95 / max | **17 / 22 / 36 / 65 ms** |
| `conflicts()` | 0.7 s, computed once and cached |

The scorer gives full speed marks at p95 ≤ 2 s.

| Operation | Complexity |
|---|---|
| Ingest | O(N) in page bytes. Unchanged pages are detected by hash and not re-parsed. Blocks are de-duplicated by signature (hash map). |
| Index build | O(Σ tokens) over unique blocks. Built eagerly at ingest so the first question pays nothing. |
| View filter | O(1) per candidate (bitmask). |
| Text query | O(Σ posting-list lengths of the query terms), then a top-k sort. |
| Rate query | O(R) over the view's table rows (R ≈ 2–3k), plus a per-view cached stale-edition set (pairs restricted to tables sharing a row signature). |
| Conflicts | O(B) grouping plus union-find over table instances. |

Answers are cached per `(question, as_of)`, which guarantees identical output when the same
question is asked twice.

## 10. Design trade-offs

| Decision | Why | Cost |
|---|---|---|
| Deterministic rules plus BM25 instead of an LLM | No API key in the grading environment. The scorer zeroes any stale number, so free-text generation is a liability. The output is deterministic, which helps the consistency score. Latency is in milliseconds. | Less fluent prose. Multi-part or very open questions are answered with the best passage rather than a synthesis. |
| Structured table rows instead of chunk-only retrieval | Tier correctness and stale-copy detection need the row, its tier and its effective date together. | Parsing depends on the site's table conventions (with fallbacks for inline "label: value" facts). |
| No embeddings | The domain is closed and has exact vocabulary. Lexical matching is explainable and needs no model download in a clean environment. | Paraphrases rely on a hand-maintained lexicon. |
| Answer text only from winning evidence | The scorer zeroes stale mentions, and it is the honest behaviour. | Historical context goes to `conflict_note`, not the answer. |
| One conflict per fact | Matches the expected output shape and keeps precision high. | A single fact with several stale copies yields one record. |

## 11. Evaluation

| Run | Final score | Notes |
|---|---|---|
| Starter solution, official public set (40 questions) | 0.093 | baseline |
| This solution, official public set | **0.9132** | 0 stale-trap hits; consistency 1.0; speed 1.0 |
| This solution, harness self-test fixture (with conflicts gold) | **1.0** | conflicts F1 1.0, correct winner |
| This solution, held-out development set (76 questions written independently from the code) | **0.9013** | 0 stale-trap hits; refusals 1.0 |

One public item has no `must_include` tokens, so the scorer can never award it points. The
development set and the snapshots are kept out of this repository. Paraphrase consistency and
repeat consistency were 1.0 on both sets.

## 12. Project structure

```
solution.py            build() → TemporalKnowledgeSystem (ingest / answer / conflicts)
tks/                   engine package (see §3)
tests/                 unittest suite on synthetic snapshots (no private data)
scripts/kit.py         loads snapshot folders in the kit layout (manifest.json + pages/*.md)
scripts/benchmark.py   ingest/latency/memory benchmark
NOTES.md               short approach summary required by the assignment
requirements.txt       empty: standard library only
```

## 13. Installation and configuration

- Python 3.9+ (grammar-checked for 3.9/3.11; run on 3.13 and 3.14). No packages to install.
- No configuration and no environment variables. The organisation name and site domain are derived
  from the crawled URLs.

## 14. Running

Clone this repository **inside** the participant kit folder (next to `harness/`, `eval/` and
`snapshots_zip/`):

```bash
git clone <this-repo-url> temporal-knowledge-system

# official scorer (unpacks the snapshots on first run)
python harness/run_eval.py --submission temporal-knowledge-system --eval eval/public_eval.jsonl

# official scorer on the self-test fixture, including the conflicts pillar
python harness/run_eval.py --submission temporal-knowledge-system \
    --snapshots harness/selftest/snapshots --eval harness/selftest/eval.jsonl \
    --conflicts-gold harness/selftest/conflicts_gold.json
```

Programmatic use:

```python
from solution import build
ks = build()
for snapshot in snapshots_oldest_first:      # objects with snapshot_id, taken_on, pages
    ks.ingest(snapshot)
ks.answer("What is the 6-month certificate APY?")                 # current
ks.answer("What is the 6-month certificate APY?", as_of="2026-05-31")
ks.conflicts()
```

## 15. Tests and benchmarks

```bash
cd temporal-knowledge-system
python -m unittest discover -s tests -t .          # 30 tests, synthetic data
python scripts/benchmark.py --snapshots ../snapshots --questions ../eval/public_eval.jsonl
python scripts/benchmark.py --memory --snapshots ../snapshots --questions ../eval/public_eval.jsonl
```

The integration tests cover:

- current vs as-of answers, and a question-embedded date;
- a date before coverage, and an expired promotion;
- a stale calculator copy beaten by a newer effective date;
- balance-tier selection, superlatives, value-check questions and list questions;
- refusals with no digits;
- newest-dated counts with teaser cards ignored;
- closure dates compared with the reference date;
- hours, addresses and links;
- determinism, citations, conflicts and malformed input.

## 16. Known limitations

- Answers are extractive. Questions that need two separate passages combined (e.g. "who can join
  **and** what deposit is needed") may get only one half.
- Rates that exist only in prose or footnotes, not in a table or a `label: value` line (e.g. a
  promotional rate inside a sentence, a rate cap in a disclosure), are not extracted as facts.
- Counts spelled out in words ("Thirty-two organizations") are not recognised as count claims.
- The domain lexicon is hand-written. Unusual phrasings outside it can cause over-cautious
  refusals.
- The hidden conflicts gold standard is unknown. Conflict precision and recall were checked on the
  self-test fixture and by manual review only.

## 17. Future improvements

- An optional, opt-in LLM reranker or answer polisher behind the same evidence guard (values
  copied only from cited evidence), for multi-part questions.
- Prose rate extraction (`introductory 4.00% dividend rate for 6 months`) with promotion-scope
  detection.
- Learned synonym expansion from the site's own vocabulary, replacing part of the hand lexicon.
- Incremental index updates for very large crawl histories (currently rebuilt after each ingest).

## 18. Development note

This project was developed with AI assistance (Claude Code), which the assignment permits. Design,
review, evaluation and submission decisions are the author's.
