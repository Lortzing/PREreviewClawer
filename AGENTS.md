# Repository guidance

## Project overview

PREreviewClawer is a Python 3.11+ pipeline that collects public PREreview review records from Zenodo, enriches their targets with external metadata, assembles CSV datasets, and validates the outputs.

Review rounds are version-aware conversation threads. `Comments` contains formal PREreview records, `Response` contains explicitly identified legacy author responses, `Discussion` contains date-ordered PREreview comment records linked to a specific review DOI, and `Timeline` indexes every retained event chronologically. Do not collapse general discussion comments into author responses.

## Environment and commands

- Run all commands from the repository root.
- uv is the sole dependency and environment manager. Run `uv sync` after checkout; it creates or updates the project-local `.venv` from `pyproject.toml` and `uv.lock`.
- Run Python through `uv run python`; never install dependencies with `pip`. Add or remove dependencies with `uv add` and `uv remove`, and commit both `pyproject.toml` and `uv.lock` when dependency resolution changes.
- Start long-running collection or enrichment jobs with a small `--max-pages` or `--limit` value before running the full dataset.
- Long-running or batch-processing additions should expose visible progress, such as a `tqdm` progress bar.
- Preserve the two-pass association policy: build the authoritative review DOI-to-target map first, then attach response and discussion records only through explicit Zenodo relations.
- Use the current open Zenodo community endpoint. Treat `comment.html` as the canonical newer-comment body, with the description only as fallback.
- Participant roles must prefer ORCID evidence, then conservative text/name evidence, and record the decision in the audit. Exact duplicate interactions are represented once but retained in the dedup log.
- Full scans are the default. Small live tests must pass `--allow-partial-scan`, and their artifacts must remain explicitly marked incomplete.
- Pipeline intermediate artifacts use schema v3; reject mismatched versions instead of silently loading stale checkpoints.

## Verification

- Staged pipeline tests: `uv run python -m unittest discover -s tests -v`
- Production crawler tests: `uv run python test_prereview_crawler_production.py`
- Final validation cross-checks each audit record against the corresponding CSV row, including DOI family when a DOI is available; always provide the stage-3 audit file to stage 4.
- Documentation-only changes should be checked for valid links, accurate commands, and consistency with current CLI arguments.
- Dependency changes should pass `uv lock --check` and a clean `uv sync` before tests are run.

## Maintenance

- Keep paths in source code and documentation relative whenever possible.
- When code changes, update this file if the commands, architecture, dependencies, or contributor guidance are affected.
- For matplotlib or seaborn plots, configure the `STHeiti` Chinese font and set `axes.unicode_minus` to `False` as specified by the repository owner.
- After a complex change, ask a sub-agent to verify that the requested task is fully complete, and repeat implementation and verification until it passes.
