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

## Dependencies

```bash
python -m pip install -r requirements_prereview_crawler.txt
```

Python 3.11 or newer is recommended.

## Recommended 1000-row run

```bash
export CROSSREF_MAILTO="your-email@example.com"

python prereview_crawler_production.py \
  --limit 1000 \
  --max-pages 100 \
  --output data/prereview/prereview_final_1000.csv \
  --extended-output data/prereview/prereview_final_1000_extended.csv \
  --stats data/prereview/prereview_collection_stats_1000.json \
  --audit data/prereview/prereview_audit_1000.json \
  --dedup-log data/prereview/prereview_review_dedup_1000.json \
  --validation-report data/prereview/prereview_validation_1000.json \
  --state-dir data/prereview/state_1000 \
  --checkpoint-every 25 \
  --field-policy metadata \
  --sampling-policy hash \
  --no-use-openalex
```

Run the same command after an interruption. The collector resumes from the last
atomic checkpoint and reuses downloaded Zenodo pages and DOI metadata.

## Refresh modes

Refresh the current Zenodo community snapshot while reusing DOI metadata:

```bash
python prereview_crawler_production.py ... --refresh-zenodo
```

Refresh DOI/arXiv metadata and rebuild accepted rows:

```bash
python prereview_crawler_production.py ... --refresh-metadata
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
  author responses.
- `coverage`: author-response families first, then multi-version families, then
  deterministic fill. This is useful for demonstrating complex review structure,
  but it is not a neutral sample.

## Strict and extended outputs

The strict CSV preserves the requested schema:

```text
DOI,PaperTitle,Authors,Source,Venue,Year,PeerReview,Field
```

Each strict `PeerReview` round contains only:

```json
{"Round": 1, "Comments": [], "Response": []}
```

The optional extended CSV also adds `Target_DOI` to every round. The audit JSON
always contains the target identifier, even when the strict CSV is used.

## Tests

```bash
python test_prereview_crawler_production.py
```

The tests cover:

- strict CSV validation;
- OSF/PsyArXiv/SocArXiv/EdArXiv/MetaArXiv/AfricArXiv venue mapping;
- exact review-body deduplication and audit retention;
- strict versus extended round shape;
- Field policies;
- DataCite metadata parsing;
- interruption and checkpoint recovery.
