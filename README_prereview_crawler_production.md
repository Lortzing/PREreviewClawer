# PREreview production crawler

This collector builds a strict, version-aware PREreview dataset from the public
`prereview-reviews` Zenodo community.

## Core rules

1. The paper-review relation comes only from an explicit Zenodo
   `related_identifier` with `relation=reviews`.
2. DOI-like strings in review prose, references, titles, and arbitrary links are
   never used to associate a review with a paper.
3. Paper metadata and review data are kept separate:
   - PREreview/Zenodo: review relationship, review text, review record, response.
   - arXiv/DataCite/Crossref: title, authors, year, venue and optional subjects.
   - OpenAlex: disabled by default; optional final fallback only.
4. Different versions of one preprint are grouped into one paper family and
   represented as consecutive rounds.
5. Exact duplicate review bodies within the same target version are merged, while
   every removed Zenodo record remains in the audit and deduplication logs.
6. Every field has provenance in the audit JSON.
7. Complete review rounds retain formal reviews, legacy author responses, and
   newer PREreview discussion comments. Discussion records are joined only by
   explicit links to a known review DOI and remain distinct from confirmed
   author responses.
8. Zenodo-hosted preprints are valid review targets when the review record
   explicitly marks their DOI with `relation=reviews`.
9. Current PREreview comments are queried from the open community endpoint and
   use `comment.html` as the canonical body. ORCID evidence precedes name/text
   evidence when assigning participant roles.
10. Exact duplicate discussion deposits are represented once, with every source
    record retained in the audit. `Timeline` must cover every retained event.
11. Explicit DOI title differences are audited as version/title-change warnings;
    they do not delete an otherwise verified review thread.
12. Historical comment completeness before the 2024-11-12 commenting relaunch is
    unverified and is never presented as complete legacy coverage.

## Dependencies

```bash
uv sync
```

Python 3.11 or newer and uv are required. Project dependencies are declared in
`pyproject.toml` and reproducibly pinned in `uv.lock`.

## Recommended 300-row complete-thread run

```bash
export CROSSREF_MAILTO="your-email@example.com"

uv run python prereview_crawler_production.py \
  --limit 300 \
  --max-pages 100 \
  --output data/prereview/prereview_final_300.csv \
  --extended-output data/prereview/prereview_final_300_extended.csv \
  --stats data/prereview/prereview_collection_stats_300.json \
  --audit data/prereview/prereview_audit_300.json \
  --dedup-log data/prereview/prereview_dedup_300.json \
  --validation-report data/prereview/prereview_validation_300.json \
  --state-dir data/prereview/state_300 \
  --checkpoint-every 25 \
  --field-policy metadata \
  --sampling-policy coverage \
  --no-use-openalex
```

Run the same command after an interruption. The collector resumes from the last
atomic checkpoint and reuses downloaded Zenodo pages and DOI metadata.

For a small connectivity test, use `--max-pages 2 --allow-partial-scan`. Without
that explicit flag, an incomplete community scan fails instead of producing a
silently truncated dataset.

## Refresh modes

Refresh the current Zenodo community snapshot while reusing DOI metadata:

```bash
uv run python prereview_crawler_production.py ... --refresh-zenodo
```

Refresh DOI/arXiv metadata and rebuild accepted rows:

```bash
uv run python prereview_crawler_production.py ... --refresh-metadata
```

For an entirely independent collection, use a new `--state-dir`.

## Field policies

- `empty`: always write `""`.
- `native`: only PREreview/Zenodo subjects and original-platform categories such
  as arXiv categories.
- `metadata` (default): native fields plus DataCite/Crossref subjects.
- `broad`: use `metadata`; when still empty, apply an explicitly marked broad
  title/venue inference.

The inferred value is never presented as a PREreview-native field. The audit JSON
records the source of each selected Field value.

## Sampling policies

- `hash` (default): deterministic SHA-256 ordering over all eligible paper
  families. This avoids intentionally over-representing multi-version papers or
  responses or discussion threads.
- `coverage`: families with legacy author responses or newer discussion comments
  first, then multi-version families, then deterministic fill. This maximizes
  complete review-thread coverage, but it is not a neutral sample.

## Strict and extended outputs

The strict CSV preserves the requested schema:

```text
DOI,PaperTitle,Authors,Source,Venue,Year,PeerReview,Field
```

Each strict `PeerReview` round contains:

```json
{"Round": 1, "Comments": [{"Reviewer_ID": "10.5281/zenodo.xxxxx", "Reviewer": ["Reviewer"], "Reviewer_ORCID": [], "Review_Date": "2026-01-01", "Comment": "Review body"}], "Response": [], "Discussion": [], "Timeline": [{"Event_Type": "review", "Event_ID": "10.5281/zenodo.xxxxx", "Actor_Role": "reviewer", "Date": "2026-01-01", "In_Reply_To": ""}]}
```

The optional extended CSV also adds `Target_DOI` to every round. The audit JSON
always contains the target identifier, even when the strict CSV is used.

## Tests

```bash
uv run python test_prereview_crawler_production.py
```

The tests cover:

- strict CSV validation;
- OSF/PsyArXiv/SocArXiv/EdArXiv/MetaArXiv/AfricArXiv venue mapping;
- exact review-body deduplication and audit retention;
- exact discussion deduplication and complete timeline validation;
- ORCID and explicit author-response role evidence;
- canonical `comment.html` extraction and pagination-drift detection;
- strict versus extended round shape;
- Field policies;
- DataCite metadata parsing;
- interruption and checkpoint recovery.
