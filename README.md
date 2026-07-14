# PREreviewClawer

[简体中文](README.zh-CN.md) | English

A staged, reproducible pipeline for collecting PREreview peer-review data from the public Zenodo community and enriching the reviewed preprints with DOI/arXiv metadata.

## Why a staged pipeline?

The original production crawler can complete the whole task in one command, but a research workflow benefits from explicit intermediate artifacts. This branch separates the process into four independently inspectable steps:

```text
PREreview / Zenodo
        |
        v
01_reviews.json
(review text, target identifier, review record, responses, discussion threads)
        |
        v
02_metadata.json
(DataCite / Crossref / arXiv metadata for each target)
        |
        v
03_dataset.csv + 03_dataset_extended.csv
(version grouping, rounds, deduplication, Field policy, provenance)
        |
        v
04_validation.json
(strict schema and data-quality checks)
```

The paper-review relationship is established only in stage 1 from explicit Zenodo `related_identifiers` with `relation=reviews`. Review discussions are joined in a second pass through explicit `references`, `cites`, or `isResponseTo` links to a known review DOI. Metadata services never decide which paper a review or discussion belongs to.

## Installation

Install [uv](https://docs.astral.sh/uv/) first, then synchronize the locked project environment:

```bash
uv sync
```

For the notebook:

```bash
uv sync --extra notebook
```

`pyproject.toml` is the dependency manifest and `uv.lock` pins the resolved versions. Run Python commands from the repository root through the project-local uv environment. Add or remove dependencies with `uv add` and `uv remove`; do not edit the lockfile manually.

## Run each stage separately

### 1. Collect review-side data only

```bash
uv run python scripts/01_collect_reviews.py \
  --max-pages 100 \
  --output data/pipeline/01_reviews.json \
  --stats data/pipeline/01_reviews_stats.json
```

This stage uses the same open `prereview-reviews` community endpoint and comment relation used by the current PREreview site. It does not query Crossref, DataCite, OpenAlex, or arXiv. Review/comment associations require explicit Zenodo relations, and `comment.html` is the canonical discussion body. Zenodo-hosted preprints are accepted only when the review record explicitly declares `relation=reviews`.

For a deliberately small connectivity test, add `--max-pages 2 --allow-partial-scan`. Partial scans are marked incomplete and are rejected by default.

### 2. Resolve DOI/arXiv metadata

```bash
export CROSSREF_MAILTO="your-email@example.com"

uv run python scripts/02_enrich_metadata.py \
  --reviews data/pipeline/01_reviews.json \
  --output data/pipeline/02_metadata.json \
  --stats data/pipeline/02_metadata_stats.json \
  --no-use-openalex
```

The stage output is also its checkpoint. Rerunning the command skips targets already recorded. Use `--retry-missing` to retry unresolved targets. To refresh every target, including resolved stage-output entries, combine `--refresh-metadata` with `--no-resume`.

### 3. Assemble the dataset

```bash
uv run python scripts/03_build_dataset.py \
  --reviews data/pipeline/01_reviews.json \
  --metadata data/pipeline/02_metadata.json \
  --limit 300 \
  --field-policy metadata \
  --sampling-policy coverage
```

Outputs:

- `03_dataset.csv`: strict eight-column schema compatible with the F1000 sample.
- `03_dataset_extended.csv`: adds `Target_DOI` to every review round.
- `03_audit.json`: field-level provenance and version mapping.
- `03_dedup.json`: exact duplicate review and discussion records removed during assembly, while retaining source-record evidence.
- `03_build_stats.json`: acceptance and rejection statistics.

### 4. Validate the final CSV

```bash
uv run python scripts/04_validate_dataset.py \
  --input data/pipeline/03_dataset.csv \
  --audit data/pipeline/03_audit.json \
  --expected 300
```

## Run the whole pipeline

```bash
uv run python scripts/run_pipeline.py \
  --limit 300 \
  --max-pages 100 \
  --field-policy metadata \
  --sampling-policy coverage
```

The collection, enrichment, and assembly stages support caching or resumable checkpoints. The default cache and checkpoint directory is `data/pipeline/state`.

## Notebook

Open:

```text
notebooks/prereview_pipeline.ipynb
```

The notebook calls the same functions used by the command-line scripts. It does not contain a second implementation of the crawler, so notebook results and script results follow identical parsing and validation rules.

## Tests

```bash
uv run python -m unittest discover -s tests -v
```

The staged-pipeline tests cover serialization round trips, target deduplication, frozen-artifact dataset assembly, and final CSV validation.

## Complete review rounds

Each round represents one reviewed preprint version and contains:

- `Comments`: the formal PREreview records for that version, including review DOI, creators, date, and body.
- `Response`: legacy records that are explicitly preserved as author responses.
- `Discussion`: all newer “Comment on a PREreview” records, ordered by publication date and linked to their exact review DOI.
- `Timeline`: a chronological, ID-only event index that covers every retained review, legacy response, and discussion event in the round.

Discussion roles are evidence-based, with ORCID matches taking precedence over names and explicit response text used as a fallback. `Comment_Type` distinguishes `author_response`, `reviewer_followup`, and `community_comment`. Exact duplicate discussion deposits are represented once and retained in the audit/dedup log. Title changes on an explicitly linked DOI are warnings, not reasons to delete a verified thread.

The staged artifacts use schema version 3 and reject older intermediate files with an instruction to rerun the preceding stage. Historical comment completeness before commenting relaunched on 2024-11-12 is not asserted: the dataset is complete for the current open Zenodo community snapshot, not a claim that every legacy PREreview database comment was migrated.

## Main modules

- `prereview_crawler_production.py`: parsing, normalization, metadata providers, version grouping, review deduplication, author-response extraction, and validation.
- `pipeline_stages.py`: stage boundaries, intermediate schemas, checkpointing, and orchestration.
- `scripts/`: one CLI entry point per stage plus an all-in-one runner.
- `notebooks/`: interactive execution and inspection of every stage.
