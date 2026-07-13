#!/usr/bin/env python3
"""Staged PREreview data pipeline.

The monolithic crawler remains the source of parsing, validation, deduplication,
venue normalization, metadata-resolution, and CSV-writing logic. This module
exposes those capabilities as independently runnable stages with explicit
intermediate artifacts:

1. PREreview/Zenodo review records only.
2. DOI/arXiv metadata enrichment only.
3. Dataset assembly from frozen stage artifacts.
4. Final CSV validation.

The split makes every transformation inspectable and rerunnable without
re-downloading unrelated data.
"""
from __future__ import annotations

import hashlib
import logging
import os
from collections import Counter, OrderedDict
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterable

from prereview_crawler_production import (
    AuthorResponse,
    Collector,
    Family,
    Review,
    Target,
    TargetBucket,
    atomic_write_json,
    load_json,
    save_csv,
    validate_csv,
)

PIPELINE_SCHEMA_VERSION = 1


@dataclass(frozen=True)
class PipelineConfig:
    """Configuration shared by all stages.

    Secrets are deliberately not serialized into stage artifacts. The helper
    ``from_env`` reads optional credentials at execution time.
    """

    state_dir: str = "data/pipeline/state"
    seed: str = "PREreview-staged-pipeline-v1"
    delay: float = 0.05
    checkpoint_every: int = 25
    resume: bool = True
    refresh_zenodo: bool = False
    refresh_metadata: bool = False
    use_datacite: bool = True
    use_crossref: bool = True
    use_openalex: bool = False
    field_policy: str = "metadata"
    sampling_policy: str = "hash"
    crossref_mailto: str = ""
    openalex_api_key: str = ""

    @classmethod
    def from_env(cls, **overrides: Any) -> "PipelineConfig":
        values = {
            "crossref_mailto": os.getenv("CROSSREF_MAILTO", ""),
            "openalex_api_key": os.getenv("OPENALEX_API_KEY", ""),
        }
        values.update(overrides)
        return cls(**values)

    def make_collector(self, *, state_suffix: str = "") -> Collector:
        state_dir = Path(self.state_dir)
        if state_suffix:
            state_dir = state_dir / state_suffix
        return Collector(
            delay=self.delay,
            seed=self.seed,
            state_dir=state_dir,
            resume=self.resume,
            checkpoint_every=self.checkpoint_every,
            crossref_mailto=self.crossref_mailto,
            openalex_api_key=self.openalex_api_key,
            use_datacite=self.use_datacite,
            use_crossref=self.use_crossref,
            use_openalex=self.use_openalex,
            field_policy=self.field_policy,
            sampling_policy=self.sampling_policy,
            refresh_zenodo=self.refresh_zenodo,
            refresh_metadata=self.refresh_metadata,
        )

    def public_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value.pop("crossref_mailto", None)
        value.pop("openalex_api_key", None)
        return value


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def require_mapping(path: Path | str) -> dict[str, Any]:
    value = load_json(Path(path))
    if not isinstance(value, dict):
        raise ValueError(f"Expected a JSON object at {path}")
    return value


def target_to_dict(target: Target) -> dict[str, Any]:
    return {
        "kind": target.kind,
        "value": target.value,
        "doi": target.doi,
        "family_key": target.family_key,
        "version": target.version,
        "scheme": target.scheme,
        "source_identifier": target.source_identifier,
    }


def target_from_dict(value: dict[str, Any]) -> Target:
    return Target(
        kind=str(value.get("kind") or ""),
        value=str(value.get("value") or ""),
        doi=str(value.get("doi") or ""),
        family_key=str(value.get("family_key") or ""),
        version=value.get("version") if isinstance(value.get("version"), int) else None,
        scheme=str(value.get("scheme") or ""),
        source_identifier=str(value.get("source_identifier") or ""),
    )


def review_to_dict(review: Review) -> dict[str, Any]:
    return {
        "review_id": review.review_id,
        "record_id": review.record_id,
        "target": target_to_dict(review.target),
        "comment": review.comment,
        "review_date": review.review_date,
        "review_type": review.review_type,
        "title_hint": review.title_hint,
        "record_url": review.record_url,
        "creators": review.creators,
        "subjects": review.subjects,
    }


def review_from_dict(value: dict[str, Any]) -> Review:
    return Review(
        review_id=str(value.get("review_id") or ""),
        record_id=str(value.get("record_id") or ""),
        target=target_from_dict(value.get("target") or {}),
        comment=str(value.get("comment") or ""),
        review_date=str(value.get("review_date") or ""),
        review_type=str(value.get("review_type") or ""),
        title_hint=str(value.get("title_hint") or ""),
        record_url=str(value.get("record_url") or ""),
        creators=[str(item) for item in value.get("creators") or [] if str(item).strip()],
        subjects=[str(item) for item in value.get("subjects") or [] if str(item).strip()],
    )


def response_to_dict(response: AuthorResponse) -> dict[str, Any]:
    return {
        "response_id": response.response_id,
        "record_id": response.record_id,
        "target_review_id": response.target_review_id,
        "family_key": response.family_key,
        "content": response.content,
        "response_date": response.response_date,
        "record_url": response.record_url,
        "creators": response.creators,
        "body_source": response.body_source,
    }


def response_from_dict(value: dict[str, Any]) -> AuthorResponse:
    return AuthorResponse(
        response_id=str(value.get("response_id") or ""),
        record_id=str(value.get("record_id") or ""),
        target_review_id=str(value.get("target_review_id") or ""),
        family_key=str(value.get("family_key") or ""),
        content=str(value.get("content") or ""),
        response_date=str(value.get("response_date") or ""),
        record_url=str(value.get("record_url") or ""),
        creators=[str(item) for item in value.get("creators") or [] if str(item).strip()],
        body_source=str(value.get("body_source") or ""),
    )


def family_to_dict(family: Family) -> dict[str, Any]:
    return {
        "family_key": family.key,
        "targets": [
            {
                "target": target_to_dict(bucket.target),
                "reviews": [review_to_dict(review) for review in bucket.reviews],
            }
            for bucket in family.targets.values()
        ],
    }


def family_from_dict(value: dict[str, Any]) -> Family:
    family = Family(str(value.get("family_key") or ""))
    for item in value.get("targets") or []:
        target = target_from_dict((item or {}).get("target") or {})
        bucket = TargetBucket(target)
        bucket.reviews = [review_from_dict(review) for review in (item or {}).get("reviews") or []]
        family.targets[target.value] = bucket
    return family


def load_families(stage1_payload: dict[str, Any]) -> OrderedDict[str, Family]:
    families: OrderedDict[str, Family] = OrderedDict()
    for value in stage1_payload.get("families") or []:
        family = family_from_dict(value)
        families[family.key] = family
    return families


def load_responses(stage1_payload: dict[str, Any]) -> dict[str, list[AuthorResponse]]:
    return {
        str(review_id): [response_from_dict(item) for item in values or []]
        for review_id, values in (stage1_payload.get("responses_by_review") or {}).items()
    }


def unique_targets(stage1_payload: dict[str, Any]) -> list[Target]:
    targets: dict[str, Target] = {}
    for family_value in stage1_payload.get("families") or []:
        for target_value in family_value.get("targets") or []:
            target = target_from_dict((target_value or {}).get("target") or {})
            if target.value:
                targets[target.value] = target
    return sorted(
        targets.values(),
        key=lambda target: (
            target.family_key,
            target.version is None,
            target.version if target.version is not None else 10**9,
            target.value,
        ),
    )


def stage1_collect_reviews(
    *,
    output: Path | str,
    stats_output: Path | str,
    max_pages: int,
    config: PipelineConfig,
) -> dict[str, Any]:
    """Collect only PREreview/Zenodo review-side data.

    No Crossref, DataCite, OpenAlex, or arXiv metadata resolution occurs here.
    The reviewed DOI/arXiv identifier is retained only because it is part of the
    explicit PREreview-to-paper relation.
    """

    collector = config.make_collector(state_suffix="stage1_reviews")
    families, scan_stats, responses_by_review, response_family_keys = collector.scan(max_pages)
    payload = {
        "pipeline_schema_version": PIPELINE_SCHEMA_VERSION,
        "stage": "01_reviews",
        "generated_at": utc_now(),
        "source": {
            "platform": "PREreview",
            "archive": "Zenodo community prereview-reviews",
            "association_policy": (
                "Only explicit Zenodo related_identifiers with relation=reviews; "
                "DOIs found in prose, titles, references, or arbitrary links are ignored."
            ),
        },
        "config": config.public_dict(),
        "stats": scan_stats,
        "families": [family_to_dict(family) for family in families.values()],
        "responses_by_review": {
            review_id: [response_to_dict(response) for response in responses]
            for review_id, responses in responses_by_review.items()
        },
        "response_family_keys": sorted(response_family_keys),
    }
    atomic_write_json(Path(output), payload)
    stats = {
        "stage": "01_reviews",
        "generated_at": payload["generated_at"],
        "families": len(families),
        "target_versions": sum(len(family.targets) for family in families.values()),
        "review_records": sum(
            len(bucket.reviews)
            for family in families.values()
            for bucket in family.targets.values()
        ),
        "linked_author_responses": sum(len(items) for items in responses_by_review.values()),
        "scan": scan_stats,
        "output": str(output),
    }
    atomic_write_json(Path(stats_output), stats)
    return stats


def stage2_resolve_metadata(
    *,
    reviews_input: Path | str,
    output: Path | str,
    stats_output: Path | str,
    config: PipelineConfig,
    retry_missing: bool = False,
) -> dict[str, Any]:
    """Resolve paper metadata for every explicit target from stage 1.

    Results are checkpointed into the stage output itself. Rerunning the command
    skips already resolved targets. Set ``retry_missing`` to retry records whose
    previous attempt produced no metadata.
    """

    stage1 = require_mapping(reviews_input)
    targets = unique_targets(stage1)
    output_path = Path(output)
    existing = load_json(output_path) if config.resume else None
    records: dict[str, Any] = {}
    if isinstance(existing, dict) and existing.get("pipeline_schema_version") == PIPELINE_SCHEMA_VERSION:
        records = dict(existing.get("records") or {})

    collector = config.make_collector(state_suffix="stage2_metadata")
    processed_since_checkpoint = 0
    errors: list[dict[str, str]] = []

    for index, target in enumerate(targets, start=1):
        previous = records.get(target.value)
        if previous and (previous.get("status") == "resolved" or not retry_missing):
            continue
        try:
            metadata = collector.resolve(target)
            record = {
                "target": target_to_dict(target),
                "status": "resolved" if metadata else "missing",
                "metadata": metadata,
                "updated_at": utc_now(),
            }
        except Exception as exc:
            logging.exception("Metadata resolution failed for %s", target.value)
            record = {
                "target": target_to_dict(target),
                "status": "error",
                "metadata": None,
                "error": f"{type(exc).__name__}: {exc}",
                "updated_at": utc_now(),
            }
            errors.append({"target": target.value, "error": record["error"]})
        records[target.value] = record
        processed_since_checkpoint += 1
        if processed_since_checkpoint >= config.checkpoint_every:
            atomic_write_json(
                output_path,
                {
                    "pipeline_schema_version": PIPELINE_SCHEMA_VERSION,
                    "stage": "02_metadata",
                    "generated_at": utc_now(),
                    "reviews_input": str(reviews_input),
                    "config": config.public_dict(),
                    "records": records,
                },
            )
            processed_since_checkpoint = 0
            logging.info("Stage 2 checkpoint: %d/%d targets recorded", index, len(targets))

    payload = {
        "pipeline_schema_version": PIPELINE_SCHEMA_VERSION,
        "stage": "02_metadata",
        "generated_at": utc_now(),
        "reviews_input": str(reviews_input),
        "config": config.public_dict(),
        "records": records,
    }
    atomic_write_json(output_path, payload)
    counts = Counter(str(item.get("status") or "unknown") for item in records.values())
    sources = Counter(
        source
        for item in records.values()
        for source in ((item.get("metadata") or {}).get("sources") or [])
    )
    stats = {
        "stage": "02_metadata",
        "generated_at": payload["generated_at"],
        "targets_in_stage1": len(targets),
        "records_written": len(records),
        "status": dict(counts),
        "metadata_sources": dict(sources),
        "request_counts": dict(collector.request_counts),
        "errors": errors[:30],
        "output": str(output),
    }
    atomic_write_json(Path(stats_output), stats)
    return stats


class SnapshotCollector(Collector):
    """Collector that reads metadata from a frozen stage-2 artifact."""

    def __init__(self, metadata_by_target: dict[str, dict[str, Any] | None], config: PipelineConfig):
        super().__init__(
            delay=0,
            seed=config.seed,
            state_dir=Path(config.state_dir) / "stage3_build",
            resume=config.resume,
            checkpoint_every=config.checkpoint_every,
            use_datacite=False,
            use_crossref=False,
            use_openalex=False,
            field_policy=config.field_policy,
            sampling_policy=config.sampling_policy,
        )
        self._snapshot_metadata = metadata_by_target

    def resolve(self, target: Target) -> dict[str, Any] | None:
        return self._snapshot_metadata.get(target.value)


def ordered_families(
    collector: Collector,
    families: Iterable[Family],
    response_family_keys: set[str],
) -> list[Family]:
    values = list(families)
    if collector.sampling_policy == "coverage":
        response_linked = sorted(
            (family for family in values if family.key in response_family_keys),
            key=lambda family: collector.family_hash(family.key),
        )
        multi_version = sorted(
            (
                family
                for family in values
                if len(family.targets) > 1 and family.key not in response_family_keys
            ),
            key=lambda family: collector.family_hash(family.key),
        )
        single_version = sorted(
            (
                family
                for family in values
                if len(family.targets) == 1 and family.key not in response_family_keys
            ),
            key=lambda family: collector.family_hash(family.key),
        )
        return response_linked + multi_version + single_version
    return sorted(values, key=lambda family: collector.family_hash(family.key))


def stage3_build_dataset(
    *,
    reviews_input: Path | str,
    metadata_input: Path | str,
    output: Path | str,
    extended_output: Path | str | None,
    audit_output: Path | str,
    dedup_output: Path | str,
    stats_output: Path | str,
    limit: int,
    config: PipelineConfig,
) -> dict[str, Any]:
    """Assemble the final dataset from frozen review and metadata artifacts."""

    stage1 = require_mapping(reviews_input)
    stage2 = require_mapping(metadata_input)
    families = load_families(stage1)
    responses_by_review = load_responses(stage1)
    response_family_keys = set(stage1.get("response_family_keys") or [])
    metadata_by_target = {
        key: (value.get("metadata") if value.get("status") == "resolved" else None)
        for key, value in (stage2.get("records") or {}).items()
    }
    collector = SnapshotCollector(metadata_by_target, config)
    ordered = ordered_families(collector, families.values(), response_family_keys)
    order_hash = hashlib.sha256(
        "\n".join(family.key for family in ordered).encode("utf-8")
    ).hexdigest()

    checkpoint_path = Path(config.state_dir) / "stage3_build" / "pipeline_checkpoint.json"
    checkpoint = load_json(checkpoint_path) if config.resume else None
    checkpoint_matches = isinstance(checkpoint, dict) and all(
        checkpoint.get(key) == expected
        for key, expected in {
            "pipeline_schema_version": PIPELINE_SCHEMA_VERSION,
            "limit": limit,
            "seed": config.seed,
            "field_policy": config.field_policy,
            "sampling_policy": config.sampling_policy,
            "order_hash": order_hash,
        }.items()
    )
    if checkpoint_matches:
        papers = list(checkpoint.get("papers") or [])
        audit = list(checkpoint.get("audit") or [])
        rejection_counts = Counter(checkpoint.get("rejection_counts") or {})
        rejection_examples = list(checkpoint.get("rejection_examples") or [])
        next_index = int(checkpoint.get("next_index") or 0)
    else:
        papers = []
        audit = []
        rejection_counts = Counter()
        rejection_examples = []
        next_index = 0

    def save_checkpoint(next_value: int, complete: bool) -> None:
        atomic_write_json(
            checkpoint_path,
            {
                "pipeline_schema_version": PIPELINE_SCHEMA_VERSION,
                "stage": "03_build",
                "updated_at": utc_now(),
                "limit": limit,
                "seed": config.seed,
                "field_policy": config.field_policy,
                "sampling_policy": config.sampling_policy,
                "order_hash": order_hash,
                "next_index": next_value,
                "papers": papers,
                "audit": audit,
                "rejection_counts": dict(rejection_counts),
                "rejection_examples": rejection_examples,
                "complete": complete,
            },
        )

    try:
        for index in range(next_index, len(ordered)):
            if len(papers) >= limit:
                next_index = index
                break
            paper, detail = collector.build_family(ordered[index], responses_by_review)
            if paper is None:
                reason = str(detail.get("reason") or "unknown")
                rejection_counts[reason] += 1
                if len(rejection_examples) < 50:
                    rejection_examples.append(detail)
            else:
                papers.append(paper)
                detail["sample_index"] = len(papers)
                audit.append(detail)
            next_index = index + 1
            if next_index % config.checkpoint_every == 0 or len(papers) >= limit:
                save_checkpoint(next_index, len(papers) >= limit)
    except BaseException:
        save_checkpoint(next_index, False)
        raise

    papers = papers[:limit]
    audit = audit[:limit]
    save_checkpoint(next_index, len(papers) >= limit)
    save_csv(papers, Path(output), extended=False)
    if extended_output:
        save_csv(papers, Path(extended_output), extended=True)
    atomic_write_json(Path(audit_output), audit)
    dedup = [
        detail
        for row in audit
        for detail in row.get("duplicate_review_records_removed") or []
    ]
    atomic_write_json(Path(dedup_output), dedup)

    selected_reviews = sum(
        len(round_value.get("Comments") or [])
        for paper in papers
        for round_value in paper.get("PeerReview") or []
    )
    stats = {
        "stage": "03_build",
        "generated_at": utc_now(),
        "requested": limit,
        "written": len(papers),
        "input_families": len(families),
        "metadata_records": len(metadata_by_target),
        "selected_review_comments": selected_reviews,
        "selected_rounds": sum(len(paper.get("PeerReview") or []) for paper in papers),
        "multi_version_papers": sum(len(paper.get("PeerReview") or []) > 1 for paper in papers),
        "responses": sum(
            len(round_value.get("Response") or [])
            for paper in papers
            for round_value in paper.get("PeerReview") or []
        ),
        "duplicate_review_records_removed": len(dedup),
        "nonempty_field": sum(bool(paper.get("Field")) for paper in papers),
        "metadata_rejections": dict(rejection_counts),
        "metadata_rejection_examples": rejection_examples,
        "sampling_policy": config.sampling_policy,
        "field_policy": config.field_policy,
        "order_hash": order_hash,
        "outputs": {
            "strict_csv": str(output),
            "extended_csv": str(extended_output) if extended_output else "",
            "audit": str(audit_output),
            "dedup": str(dedup_output),
        },
    }
    atomic_write_json(Path(stats_output), stats)
    return stats


def stage4_validate_dataset(
    *,
    csv_input: Path | str,
    report_output: Path | str,
    expected: int,
) -> dict[str, Any]:
    issues = validate_csv(Path(csv_input), expected)
    report = {
        "stage": "04_validate",
        "generated_at": utc_now(),
        "input": str(csv_input),
        "expected_rows": expected,
        "valid": not issues,
        "issue_count": len(issues),
        "issues": issues,
    }
    atomic_write_json(Path(report_output), report)
    return report


def default_paths(root: Path | str = "data/pipeline") -> dict[str, Path]:
    root = Path(root)
    return {
        "reviews": root / "01_reviews.json",
        "reviews_stats": root / "01_reviews_stats.json",
        "metadata": root / "02_metadata.json",
        "metadata_stats": root / "02_metadata_stats.json",
        "csv": root / "03_dataset.csv",
        "extended_csv": root / "03_dataset_extended.csv",
        "audit": root / "03_audit.json",
        "dedup": root / "03_dedup.json",
        "build_stats": root / "03_build_stats.json",
        "validation": root / "04_validation.json",
    }
