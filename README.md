# PREreviewClawer

A staged, reproducible pipeline for collecting PREreview peer-review data from the public Zenodo community and enriching the reviewed preprints with DOI/arXiv metadata.

## Why a staged pipeline?

The original production crawler can complete the whole task in one command, but a research workflow benefits from explicit intermediate artifacts. This branch separates the process into four independently inspectable steps:

```text
PREreview / Zenodo
        |
        v
01_reviews.json
(review text, target identifier, review record, response relation)
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

The paper-review relationship is established only in stage 1 from explicit Zenodo `related_identifiers` with `relation=reviews`. Metadata services never decide which paper a review belongs to.

## Installation

```bash
python -m pip install -r requirements_prereview_crawler.txt
```

For the notebook:

```bash
python -m pip install -e ".[notebook]"
```

## Run each stage separately

### 1. Collect review-side data only

```bash
python scripts/01_collect_reviews.py \
  --max-pages 100 \
  --output data/pipeline/01_reviews.json \
  --stats data/pipeline/01_reviews_stats.json
```

This stage does not query Crossref, DataCite, OpenAlex, or arXiv. It writes review text, explicit target identifiers, review record identifiers, author-response links, version information, and Zenodo-side subjects.

### 2. Resolve DOI/arXiv metadata

```bash
export CROSSREF_MAILTO="your-email@example.com"

python scripts/02_enrich_metadata.py \
  --reviews data/pipeline/01_reviews.json \
  --output data/pipeline/02_metadata.json \
  --stats data/pipeline/02_metadata_stats.json \
  --no-use-openalex
```

The stage output is also its checkpoint. Rerunning the command skips targets already recorded. Use `--retry-missing` to retry unresolved targets and `--refresh-metadata` to bypass provider caches.

### 3. Assemble the dataset

```bash
python scripts/03_build_dataset.py \
  --reviews data/pipeline/01_reviews.json \
  --metadata data/pipeline/02_metadata.json \
  --limit 1000 \
  --field-policy metadata \
  --sampling-policy hash
```

Outputs:

- `03_dataset.csv`: strict eight-column schema compatible with the F1000 sample.
- `03_dataset_extended.csv`: adds `Target_DOI` to every review round.
- `03_audit.json`: field-level provenance and version mapping.
- `03_dedup.json`: exact duplicate review records removed during assembly.
- `03_build_stats.json`: acceptance and rejection statistics.

### 4. Validate the final CSV

```bash
python scripts/04_validate_dataset.py \
  --input data/pipeline/03_dataset.csv \
  --expected 1000
```

## Run the whole pipeline

```bash
python scripts/run_pipeline.py \
  --limit 1000 \
  --max-pages 100 \
  --field-policy metadata \
  --sampling-policy hash
```

All stages are resumable. The default cache and checkpoint directory is `data/pipeline/state`.

## Notebook

Open:

```text
notebooks/prereview_pipeline.ipynb
```

The notebook calls the same functions used by the command-line scripts. It does not contain a second implementation of the crawler, so notebook results and script results follow identical parsing and validation rules.

## Tests

```bash
python -m unittest discover -s tests -v
```

The staged-pipeline tests cover serialization round trips, target deduplication, frozen-artifact dataset assembly, and final CSV validation.

## Main modules

- `prereview_crawler_production.py`: parsing, normalization, metadata providers, version grouping, review deduplication, author-response extraction, and validation.
- `pipeline_stages.py`: stage boundaries, intermediate schemas, checkpointing, and orchestration.
- `scripts/`: one CLI entry point per stage plus an all-in-one runner.
- `notebooks/`: interactive execution and inspection of every stage.
