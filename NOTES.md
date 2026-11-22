# NOTES

## How it works (short version)

`solution.py` builds a **deterministic temporal evidence engine** that uses only the Python standard
library. It makes no LLM calls, uses no embeddings, needs no network access and **needs no API keys**.

1. **Ingest (oldest → newest).** Every page is cleaned and split into evidence *blocks*:
   - table rows, each carrying its caption, tier labels, typed values (rate / dividend / APY / APR /
     dollar amount) and the "Rates are effective as of" date that governs its table;
   - text chunks under their heading path.

   Blocks are de-duplicated by content hash. Each block records two bitmasks over the
   snapshots:
   - *observed*: the block was literally present in that crawl;
   - *active*: the block was part of the site as it stood at that snapshot.

   A page missing from a later crawl is carried forward. A block that disappears from a re-crawled
   page is superseded.
2. **Answer.** `answer(question, as_of)` first selects a view: the latest snapshot taken on or before
   `as_of`, or the latest snapshot when no date is given. It then routes the question by intent:
   - rates and fees go to the table rows;
   - hours, counts, links and closure dates each have their own extractor;
   - everything else goes to BM25 passage retrieval.

   The answer text is composed **only from the selected evidence values**. Losing or stale values
   are reported in `conflict_note`, never in the answer text.
3. **Conflicts.** One entry is produced per contradicting fact:
   - table rows aligned across pages and editions;
   - repeated boilerplate sentences that disagree on a number.

   Each entry carries all observed values, the winning value and the rule that decided it.

## How "current" is decided

These rules apply in order. The same rules drive answers and `conflicts()`.

1. **Effective date wins.** When two pages carry editions of the same table, the table with the
   newer "Rates are effective as of" date supersedes the other, even inside the newest snapshot.
   This is how calculators, campaign landing pages and guidance pages with frozen rates lose to the
   official rate sheet.
2. **Source authority.** Ties go to the more authoritative page class: rate sheet > product page >
   info > landing > newsroom/article/document > calculator.
3. **Recency of observation.** The last snapshot in which a row was seen breaks remaining ties.
4. **Narrative claims** (counts such as branch numbers, ordinals) are ordered by the article's own
   publish date, not by how many pages repeat them. Teaser cards for other posts are stripped so
   that their dates cannot leak into the host page.
5. **Promotions** whose stated end date has passed are not current.
6. **Qualifiers are facts, not conflicts.** Term, balance tier, loan amount, model-year band,
   jumbo vs standard, personal vs business, and rate vs APY are all matched explicitly. Rows that
   differ in these qualifiers are never treated as contradicting each other.

## Abstention

The system says "I don't know … please contact the credit union" with `confident=False` and **no
digits** when any of these holds:
- no evidence row matches the product the question names (e.g. a loan type the site never prices);
- the question's head noun or a named entity is absent from the best evidence;
- passage coverage is too low;
- an explicit `as_of` predates the first snapshot.

## API keys

None. There are no paid APIs and no network calls. `requirements.txt` is intentionally empty
(standard library only). Source grammar is verified for Python 3.9 and 3.11 with
`ast.parse(feature_version=...)`; the test suite and the scorer were run on Python 3.13 and 3.14.
