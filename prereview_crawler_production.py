#!/usr/bin/env python3
"""Collect a strict, version-aware PREreview dataset with resumable state.

The public PREreview v2 API documented on the legacy developer page is no
longer available (it currently returns HTTP 404). This collector therefore
uses the official "Reviews on PREreview" Zenodo community as the authoritative
review source. A paper/review association is accepted only when Zenodo
provides an explicit related_identifier with relation=reviews. DOI-like text
inside review prose, references, titles, or arbitrary links is never used to
associate a review with a paper.
"""
from __future__ import annotations

import argparse
import csv
import difflib
import hashlib
import html
import json
import logging
import os
import re
import sys
import time
import unicodedata
import xml.etree.ElementTree as ET
from collections import Counter, OrderedDict
from dataclasses import dataclass, field
from datetime import UTC, datetime
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import quote, urlparse

import requests
from bs4 import BeautifulSoup

try:
    import fitz  # PyMuPDF, used for the small number of PDF-only author responses
except ImportError:  # pragma: no cover - reported in collection statistics
    fitz = None

COLUMNS = ["DOI", "PaperTitle", "Authors", "Source", "Venue", "Year", "PeerReview", "Field"]
COMMUNITY = "prereview-reviews"
ZENODO_API = f"https://zenodo.org/api/communities/{COMMUNITY}/records"
DOI_RE = re.compile(r"10\.\d{4,9}/[-._;()/:A-Z0-9]+", re.I)
ZENODO_DOI_RE = re.compile(r"10\.5281/zenodo\.\d+", re.I)
ARXIV_RE = re.compile(r"(?:arxiv:|arxiv\.org/(?:abs|pdf)/)?([a-z.-]+/\d{7}|\d{4}\.\d{4,5})(?:v(\d+))?", re.I)
VERSION_SUFFIXES = [
    re.compile(r"(?i)([._-]v)(\d+)$"),
    re.compile(r"(?i)(/v)(\d+)$"),
    re.compile(r"(?i)([._-]version)(\d+)$"),
]
PRESERVATION_PHRASES = (
    "this zenodo record is a permanently preserved version of",
    "you can view the complete prereview at",
)
KNOWN_HTML_TAGS = frozenset({
    "a", "abbr", "article", "aside", "b", "blockquote", "br", "code", "dd", "div",
    "dl", "dt", "em", "figcaption", "figure", "h1", "h2", "h3", "h4", "h5",
    "h6", "hr", "i", "img", "li", "ol", "p", "pre", "q", "s", "section",
    "small", "span", "strong", "sub", "sup", "table", "tbody", "td", "th",
    "thead", "tr", "u", "ul",
})
FORBIDDEN_VENUE_FRAGMENTS = (
    " elsevier", "springer", "wiley", "taylor & francis", "sage publishing",
    "science and business media", "publishing group", "publications inc",
    "fapunifesp", "publisher", " llc", " bv",
)
CURRENT_YEAR = datetime.now(UTC).year
STATE_VERSION = 4
COMMENTING_RELAUNCH_DATE = "2024-11-12"


def atomic_write_json(path: Path, value: Any) -> None:
    """Atomically persist JSON so an interrupted write cannot corrupt state."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def atomic_write_bytes(path: Path, value: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_bytes(value)
    temporary.replace(path)


def load_json(path: Path) -> Any | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None


@dataclass(frozen=True)
class Target:
    kind: str
    value: str
    doi: str
    family_key: str
    version: int | None
    scheme: str
    source_identifier: str


@dataclass
class Review:
    review_id: str
    record_id: str
    target: Target
    comment: str
    review_date: str
    review_type: str
    title_hint: str
    record_url: str
    creators: list[str] = field(default_factory=list)
    creator_orcids: list[str] = field(default_factory=list)
    subjects: list[str] = field(default_factory=list)


@dataclass
class AuthorResponse:
    response_id: str
    record_id: str
    target_review_id: str
    family_key: str
    content: str
    response_date: str
    record_url: str
    creators: list[str] = field(default_factory=list)
    creator_orcids: list[str] = field(default_factory=list)
    body_source: str = ""


@dataclass
class DiscussionComment:
    comment_id: str
    record_id: str
    target_review_id: str
    family_key: str
    content: str
    comment_date: str
    record_url: str
    creators: list[str] = field(default_factory=list)
    creator_orcids: list[str] = field(default_factory=list)
    body_source: str = ""
    target_relation_verified: bool = False


@dataclass
class TargetBucket:
    target: Target
    reviews: list[Review] = field(default_factory=list)


@dataclass
class Family:
    key: str
    targets: OrderedDict[str, TargetBucket] = field(default_factory=OrderedDict)


def clean_text(value: Any, separator: str = "\n") -> str:
    if value is None:
        return ""
    raw = str(value)
    # Zenodo contains both ordinary HTML and historically double-escaped HTML.
    # Decode before parsing so tags do not reappear after BeautifulSoup has run.
    for _ in range(3):
        decoded = html.unescape(raw)
        if decoded == raw:
            break
        raw = decoded
    # Preserve Markdown-style autolinks as plain URLs instead of letting an HTML
    # parser interpret strings such as <https://example.org/path> as tag names.
    raw = re.sub(
        r"<\s*(https?://[^<>]+?)\s*>",
        lambda match: re.sub(r"\s+", "", match.group(1)),
        raw,
        flags=re.I | re.S,
    )
    if "<" not in raw and ">" not in raw:
        raw_text = raw
    else:
        soup = BeautifulSoup(raw, "html.parser")
        for tag in soup(["script", "style", "noscript"]):
            tag.decompose()
        raw_text = soup.get_text(separator)
    lines = [re.sub(r"[\t\r\f\v ]+", " ", line).strip() for line in raw_text.splitlines()]
    out: list[str] = []
    blank = False
    for line in lines:
        if not line:
            if out and not blank:
                out.append("")
            blank = True
            continue
        out.append(line)
        blank = False
    return "\n".join(out).strip()


def clean_review_html(value: Any) -> str:
    if not value:
        return ""
    soup = BeautifulSoup(str(value), "html.parser")
    for node in list(soup.find_all(string=True)):
        low = str(node).strip().lower()
        if any(phrase in low for phrase in PRESERVATION_PHRASES):
            parent = node.find_parent("p") or node.find_parent("div") or node.parent
            if parent is not None:
                parent.decompose()
    text_value = clean_text(str(soup))
    cleaned: list[str] = []
    for line in text_value.splitlines():
        low = line.strip().lower()
        if not line.strip():
            cleaned.append("")
            continue
        if any(phrase in low for phrase in PRESERVATION_PHRASES):
            continue
        if re.fullmatch(r"https?://(?:www\.)?prereview\.org/reviews/\d+/?", line.strip(), re.I):
            continue
        if line.strip() == ".":
            continue
        cleaned.append(line.strip())
    out: list[str] = []
    for line in cleaned:
        if not line and (not out or not out[-1]):
            continue
        out.append(line)
    return "\n".join(out).strip()



def load_dictionary() -> set[str]:
    path = Path("/usr/share/hunspell/en_US.dic")
    words: set[str] = set()
    if not path.exists():
        return words
    with path.open(encoding="utf-8", errors="ignore") as file:
        next(file, None)
        for line in file:
            word = line.strip().split("/")[0]
            if word:
                words.add(word.lower())
    return words


ENGLISH_DICTIONARY = load_dictionary()


def clean_title(value: Any) -> str:
    """Flatten markup artifacts while preserving meaningful compact slashes."""
    value = clean_text(value, " ")
    value = html.unescape(value).replace("\r", " ").replace("\n", " ")
    value = re.sub(r"\s+/\s+", " ", value)
    value = re.sub(r"\s+-\s+", "-", value)
    value = re.sub(r"\s+([,.;:!?])", r"\1", value)
    value = re.sub(r"\(\s+", "(", value)
    value = re.sub(r"\s+\)", ")", value)
    value = re.sub(r"\bB\s+12\b", "B12", value)
    value = re.sub(r"\b5-HT\s+2\b", "5-HT2", value)
    value = re.sub(r"\bGABA\s+A\b", "GABA_A", value)
    value = re.sub(r"\b(cis|trans)\s+-", r"\1-", value, flags=re.I)
    value = re.sub(r"\btrans\s+-species\b", "trans-species", value, flags=re.I)
    return re.sub(r"\s+", " ", value).strip()


def clean_comment(value: Any) -> str:
    """Normalize review text without deleting substantive questionnaire content."""
    text_value = clean_review_html(value)
    text_value = re.sub(
        r"(?is)^\s*(?:bioRxiv|medRxiv|arXiv|preprint)\s+preprint\s+doi\s*:\s*"
        r"(?:https?://doi\.org/)?10\.\S+?;\s*",
        "",
        text_value,
    )
    paragraphs: list[str] = []
    for paragraph in re.split(r"\n\s*\n+", text_value):
        paragraph = re.sub(r"[ \t]+", " ", paragraph)
        paragraph = re.sub(r" *\n *", " ", paragraph).strip()
        if paragraph:
            paragraphs.append(paragraph)
    return "\n\n".join(paragraphs).strip()


def normalized_comment_hash(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", clean_comment(value)).lower()
    normalized = re.sub(r"\s+", " ", normalized).strip()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def dewrap_pdf(value: str) -> str:
    """Repair common word and paragraph line wrapping from PDF text extraction."""
    raw_lines = html.unescape(value or "").replace("\r\n", "\n").replace("\r", "\n").splitlines()
    fixed: list[str] = []
    index = 0
    while index < len(raw_lines):
        line = raw_lines[index].strip()
        if not line:
            if fixed and fixed[-1] != "":
                fixed.append("")
            index += 1
            continue
        if index + 1 < len(raw_lines) and raw_lines[index + 1].strip():
            next_line = raw_lines[index + 1].strip()
            left = re.search(r"([A-Za-z]{2,})(-?)$", line)
            right = re.match(r"([a-z]{2,})\b", next_line)
            if left and right:
                first, hyphen, second = left.group(1), left.group(2), right.group(1)
                joined = (first + second).lower()
                if hyphen or joined in ENGLISH_DICTIONARY:
                    line = line[:left.start(1)] + first + second + next_line[right.end(1):]
                    raw_lines[index + 1] = ""
        fixed.append(line)
        index += 1
    paragraphs: list[str] = []
    for paragraph in re.split(r"\n\s*\n+", "\n".join(fixed)):
        paragraph = re.sub(r"\s*\n\s*", " ", paragraph)
        paragraph = re.sub(r"\s+", " ", paragraph).strip()
        if paragraph:
            paragraphs.append(paragraph)
    return "\n\n".join(paragraphs).strip()


def token_spans(value: str) -> list[tuple[str, int, int]]:
    return [(m.group(), m.start(), m.end()) for m in re.finditer(r"\w+(?:[-'][\w]+)*|[^\w\s]", value)]


def author_only_response(response: str, review: str) -> str:
    """Remove long verbatim blocks of the quoted review from a point-by-point PDF response."""
    response = dewrap_pdf(response)
    review = clean_comment(review)
    response_tokens = token_spans(response)
    review_tokens = token_spans(review)

    def normalize(token: str) -> str:
        return (
            unicodedata.normalize("NFKC", token)
            .lower()
            .replace("’", "'")
            .replace("“", '"')
            .replace("”", '"')
        )

    matcher = difflib.SequenceMatcher(
        a=[normalize(token[0]) for token in review_tokens],
        b=[normalize(token[0]) for token in response_tokens],
        autojunk=False,
    )
    blocks = [block for block in matcher.get_matching_blocks() if block.size >= 10]
    gaps: list[str] = []
    previous_end = 0
    for block in blocks:
        start = response_tokens[block.b][1]
        end = response_tokens[block.b + block.size - 1][2]
        gap = response[previous_end:start].strip(" \n;:")
        if gap:
            gaps.append(gap)
        previous_end = end
    tail = response[previous_end:].strip(" \n;:")
    if tail:
        gaps.append(tail)

    preamble: list[str] = []
    answers: list[str] = []
    for gap in gaps:
        word_count = len(re.findall(r"\w+", gap))
        lowered = gap.lower()
        if word_count < 12:
            if not answers and ("authors" in lowered or "responses" in lowered):
                preamble.append(gap)
            continue
        if not answers and ("we are the authors" in lowered or "here are our responses" in lowered):
            preamble.append(gap)
        else:
            answers.append(re.sub(r"\s+", " ", gap).strip())

    output: list[str] = []
    if preamble:
        joined = re.sub(r"\s+", " ", " ".join(preamble))
        joined = re.sub(r"\s+([,.;:)])", r"\1", joined)
        joined = re.sub(r"\(\s+", "(", joined)
        joined = re.sub(r'(preprint\s+“[^”]+?)\s+\(doi:', r'\1” (doi:', joined, flags=re.I)
        output.append(joined.strip())
    output.extend(f"Author response {number}: {answer}" for number, answer in enumerate(answers, 1))
    return "\n\n".join(output) if output else response


def normalized_person_tokens(value: str) -> tuple[str, ...]:
    normalized = unicodedata.normalize("NFKD", clean_text(value, " ")).casefold()
    normalized = "".join(character for character in normalized if not unicodedata.combining(character))
    return tuple(sorted(re.findall(r"[a-z0-9]+", normalized)))


def person_lists_overlap(left: Iterable[str], right: Iterable[str]) -> bool:
    left_keys = {key for value in left if (key := normalized_person_tokens(value))}
    right_keys = {key for value in right if (key := normalized_person_tokens(value))}
    return bool(left_keys & right_keys)


def normalize_orcid(value: Any) -> str:
    match = re.search(r"\b(\d{4}-\d{4}-\d{4}-\d{3}[\dX])\b", clean_text(value, " "), re.I)
    return match.group(1).upper() if match else ""


def orcid_lists_overlap(left: Iterable[str], right: Iterable[str]) -> bool:
    left_values = {normalized for value in left if (normalized := normalize_orcid(value))}
    right_values = {normalized for value in right if (normalized := normalize_orcid(value))}
    return bool(left_values & right_values)


AUTHOR_RESPONSE_PATTERNS = (
    r"\bauthor(?:s['’]?)?\s+(?:response|reply|rebuttal)\b",
    r"\bresponse(?:s)?\s+to\s+(?:the\s+)?reviewers?\b",
    r"\bwe,?\s+as\s+(?:the\s+)?authors?\b",
    r"\bwe\s+(?:sincerely\s+)?thank\s+(?:the\s+)?(?:anonymous\s+)?reviewers?\b",
    r"\bthank(?:s|\s+you)?\s+(?:very\s+much\s+)?for\s+your\s+(?:pre)?review\b",
    r"\bthank\s+you\s+for\s+reviewing\s+our\s+manuscript\b",
    r"\bwe\s+(?:gladly\s+)?respond\s+(?:below|to\s+(?:each|the))\b",
    r"\bour\s+responses?\s+(?:are|is)\s+below\b",
    r"\bpaper\s+has\s+been\s+updated\s+in\s+response\s+to\s+the\s+pre-?review\b",
    r"\bgracias\s+por\s+su\s+revisi[oó]n\b",
    r"\bagrade[cç]o\s+(?:pelas?|por\s+suas?)\s+(?:sugest[oõ]es|revis[aã]o|coment[aá]rios)\b",
)


def author_response_text_evidence(value: str) -> str:
    opening = clean_text(value, " ")[:2000]
    for pattern in AUTHOR_RESPONSE_PATTERNS:
        if re.search(pattern, opening, re.I):
            return f"comment text matches author-response pattern: {pattern}"
    return ""


def discussion_participant_role(
    discussion: DiscussionComment,
    paper_authors: Iterable[str],
    review_creators: Iterable[str],
    paper_author_orcids: Iterable[str] = (),
    review_creator_orcids: Iterable[str] = (),
) -> tuple[str, str]:
    if orcid_lists_overlap(discussion.creator_orcids, paper_author_orcids):
        return "author", "commenter ORCID matches resolved paper author"
    if orcid_lists_overlap(discussion.creator_orcids, review_creator_orcids):
        return "reviewer", "commenter ORCID matches PREreview creator"
    text_evidence = author_response_text_evidence(discussion.content)
    if text_evidence:
        return "author", text_evidence
    if person_lists_overlap(discussion.creators, paper_authors):
        return "author", "commenter name matches resolved paper author"
    if person_lists_overlap(discussion.creators, review_creators):
        return "reviewer", "commenter name matches PREreview creator"
    return "commenter", "role is not asserted by Zenodo metadata"

BROAD_FIELD_VENUE_MAP = {
    "bioRxiv": "Biological Sciences",
    "medRxiv": "Medicine and Health Sciences",
    "ChemRxiv": "Chemistry",
    "PsyArXiv": "Psychology and Behavioral Sciences",
    "SocArXiv": "Social Sciences",
    "EdArXiv": "Education",
    "MetaArXiv": "Metascience and Scholarly Communication",
}


def infer_broad_field(title: str, venue: str) -> str:
    """Last-resort broad classification; provenance must label this as inferred."""
    if venue in BROAD_FIELD_VENUE_MAP:
        return BROAD_FIELD_VENUE_MAP[venue]
    text_value = clean_title(title).lower()
    rules = [
        ("Metascience and Scholarly Communication", ("open science", "preprint", "peer review", "reproducib", "research integrity")),
        ("Computer Science and Artificial Intelligence", ("artificial intelligence", "machine learning", "neural network", "llm", "reinforcement learning", "computer vision", "multi-agent")),
        ("Medicine and Health Sciences", ("patient", "clinical", "disease", "cancer", "covid", "health", "treatment", "diagnostic", "hospital", "trial", "mortality")),
        ("Biological Sciences", ("gene", "genome", "protein", "cell", "neuron", "plant", "mouse", "rna", "dna", "bacterial", "microbiome")),
        ("Physics", ("quantum", "black hole", "thermodynamic", "laser", "wave field")),
        ("Chemistry", ("chemical", "molecule", "cataly", "synthesis")),
        ("Psychology and Behavioral Sciences", ("psycholog", "cognition", "behavior", "belief", "aggression")),
        ("Engineering and Technology", ("lidar", "uav", "manufactur", "automation", "robot", "engineering")),
        ("Environmental and Earth Sciences", ("climate", "environment", "geospatial", "gis", "earth")),
        ("Economics and Business", ("econom", "investment", "business", "finance", "market")),
        ("Education", ("education", "learning", "student", "teaching")),
        ("Mathematics", ("theorem", "polynomial", "integer", "number theory", "mathemat")),
        ("Social Sciences", ("social media", "news media", "survey", "society", "political")),
    ]
    for label, keywords in rules:
        if any(keyword in text_value for keyword in keywords):
            return label
    if venue == "Research Square":
        return "Medicine and Health Sciences"
    if venue == "SSRN":
        return "Social Sciences"
    return "Interdisciplinary"

def normalize_doi(value: Any) -> str:
    if not value:
        return ""
    raw = html.unescape(str(value)).strip()
    raw = re.sub(r"^https?://(?:dx\.)?doi\.org/", "", raw, flags=re.I)
    raw = re.sub(r"^doi:\s*", "", raw, flags=re.I)
    match = DOI_RE.fullmatch(raw.rstrip(".,;:)]}>"))
    return match.group(0).lower() if match else ""


def family_and_version(identifier: str, kind: str) -> tuple[str, int | None]:
    value = identifier.lower().strip()
    if kind == "arxiv":
        match = re.fullmatch(r"(.+?)(?:v(\d+))?", value, re.I)
        assert match
        return f"arxiv:{match.group(1)}", int(match.group(2)) if match.group(2) else None
    for pattern in VERSION_SUFFIXES:
        match = pattern.search(value)
        if match:
            return f"doi:{value[:match.start()]}", int(match.group(2))
    return f"doi:{value}", None


def explicit_target(record: dict[str, Any]) -> tuple[Target | None, str]:
    metadata = record.get("metadata") or {}
    related = metadata.get("related_identifiers") or record.get("related_identifiers") or []
    accepted: list[Target] = []
    for item in related:
        if not isinstance(item, dict):
            continue
        relation = str(item.get("relation") or item.get("relation_type") or "").lower()
        resource_type = item.get("resource_type") or ""
        if isinstance(resource_type, dict):
            resource_type = " ".join(str(x) for x in resource_type.values())
        resource_low = str(resource_type).lower()
        if relation != "reviews":
            continue
        if resource_low and "preprint" not in resource_low:
            continue
        raw = str(item.get("identifier") or item.get("id") or item.get("value") or "").strip()
        scheme = str(item.get("scheme") or "").lower()
        candidate_doi = normalize_doi(raw)
        if not candidate_doi and raw.lower().startswith(("http://", "https://")):
            path = urlparse(raw).path.lstrip("/")
            candidate_doi = normalize_doi(path)
        if candidate_doi:
            family, version = family_and_version(candidate_doi, "doi")
            accepted.append(Target("doi", candidate_doi, candidate_doi, family, version, scheme or "doi", raw))
            continue
        arxiv_match = ARXIV_RE.fullmatch(raw) or ARXIV_RE.search(raw)
        if arxiv_match:
            arxiv_id = arxiv_match.group(1).lower()
            version = int(arxiv_match.group(2)) if arxiv_match.group(2) else None
            family, _ = family_and_version(arxiv_id, "arxiv")
            accepted.append(Target("arxiv", arxiv_id, "", family, version, scheme or "arxiv", raw))
    unique = OrderedDict((target.value, target) for target in accepted)
    if not unique:
        return None, "no_explicit_target"
    if len(unique) > 1:
        families = {target.family_key for target in unique.values()}
        if len(families) != 1:
            return None, "ambiguous_explicit_targets"
        target = sorted(unique.values(), key=lambda x: (x.version is not None, x.version or 0, x.value))[-1]
        return target, "multiple_same_family"
    return next(iter(unique.values())), "ok"


def review_doi(record: dict[str, Any]) -> str:
    metadata, pids = record.get("metadata") or {}, record.get("pids") or {}
    candidates = [
        (pids.get("doi") or {}).get("identifier") if isinstance(pids, dict) else None,
        metadata.get("doi"), record.get("doi"),
    ]
    for candidate in candidates:
        normalized = normalize_doi(candidate)
        if normalized:
            return normalized
    recid = str(record.get("id") or "")
    return f"10.5281/zenodo.{recid}" if recid.isdigit() else f"PREreview:{recid}"


def review_type_and_title(title: Any) -> tuple[str, str]:
    title_text = clean_text(title, " ")
    low = title_text.lower()
    review_type = "PREreview"
    for label in ("Structured PREreview", "Rapid PREreview", "Full PREreview"):
        if low.startswith(label.lower()):
            review_type = label
            break
    patterns = [
        r"(?is)^(?:structured|rapid|full)?\s*prereview\s+of\s+[\"“](.*?)[\"”]\s*$",
        r"(?is)^prereview\s+of\s+(.*?)\s*$",
    ]
    hint = ""
    for pattern in patterns:
        match = re.match(pattern, title_text)
        if match:
            hint = clean_text(match.group(1), " ").strip('"“”')
            break
    return review_type, hint


def creator_identities_from_record(record: dict[str, Any]) -> tuple[list[str], list[str]]:
    names_out: list[str] = []
    orcids_out: list[str] = []
    for creator in (record.get("metadata") or {}).get("creators") or []:
        name = clean_text(creator.get("name") if isinstance(creator, dict) else creator, " ")
        if name and name not in names_out:
            names_out.append(name)
        if isinstance(creator, dict):
            orcid = normalize_orcid(creator.get("orcid") or creator.get("nameIdentifiers"))
            if orcid and orcid not in orcids_out:
                orcids_out.append(orcid)
    return names_out, orcids_out


def creators_from_record(record: dict[str, Any]) -> list[str]:
    return creator_identities_from_record(record)[0]


def subjects_from_record(record: dict[str, Any]) -> list[str]:
    out: list[str] = []
    for subject in (record.get("metadata") or {}).get("subjects") or []:
        term = clean_text(subject.get("term") if isinstance(subject, dict) else subject, " ")
        if term and term not in out:
            out.append(term)
    return out


def title_similarity(left: str, right: str) -> float:
    def norm(value: str) -> str:
        return re.sub(r"[^a-z0-9]+", " ", value.lower()).strip()
    a, b = norm(left), norm(right)
    if not a or not b:
        return 1.0
    sequence = SequenceMatcher(None, a, b).ratio()
    sa, sb = set(a.split()), set(b.split())
    jaccard = len(sa & sb) / len(sa | sb) if sa | sb else 1.0
    return max(sequence, jaccard)


def names(items: Any) -> list[str]:
    out: list[str] = []
    for item in items if isinstance(items, list) else []:
        if isinstance(item, str):
            name = clean_text(item, " ")
        elif isinstance(item, dict):
            person = item.get("person_or_org")
            if isinstance(person, dict):
                name = person.get("name") or " ".join(filter(None, [person.get("given_name"), person.get("family_name")]))
            else:
                name = " ".join(filter(None, [item.get("given"), item.get("family")])) or item.get("name") or ""
            name = clean_text(name, " ")
        else:
            name = ""
        if name and name not in out:
            out.append(name)
    return out


def orcids_from_people(items: Any) -> list[str]:
    out: list[str] = []
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict):
            continue
        candidates: list[Any] = [item.get("ORCID"), item.get("orcid")]
        person = item.get("person_or_org")
        if isinstance(person, dict):
            candidates.extend([person.get("orcid"), person.get("ORCID")])
        for identifier in item.get("nameIdentifiers") or []:
            if isinstance(identifier, dict):
                candidates.append(identifier.get("nameIdentifier"))
        for candidate in candidates:
            orcid = normalize_orcid(candidate)
            if orcid and orcid not in out:
                out.append(orcid)
    return out


def date_year(obj: dict[str, Any], keys: Iterable[str]) -> str:
    for key in keys:
        value = obj.get(key) or {}
        if isinstance(value, dict):
            parts = value.get("date-parts") or []
            if parts and parts[0]:
                year = str(parts[0][0])
                if re.fullmatch(r"\d{4}", year):
                    return year
    return ""


VENUE_ALIASES = OrderedDict([
    # Specific names must precede the generic substring "arxiv".
    ("psyarxiv", "PsyArXiv"), ("socarxiv", "SocArXiv"), ("eartharxiv", "EarthArXiv"),
    ("ecoevorxiv", "EcoEvoRxiv"), ("engrxiv", "engrXiv"), ("lawarxiv", "LawArXiv"),
    ("edarxiv", "EdArXiv"), ("metaarxiv", "MetaArXiv"), ("africarxiv", "AfricArXiv"),
    ("medrxiv", "medRxiv"), ("biorxiv", "bioRxiv"), ("openrxiv", "openRxiv"),
    ("chemrxiv", "ChemRxiv"), ("arxiv", "arXiv"), ("preprints.org", "Preprints.org"),
    ("research square", "Research Square"), ("researchsquare", "Research Square"),
    ("osf preprints", "OSF Preprints"), ("osf.io", "OSF Preprints"),
    ("authorea", "Authorea"), ("ssrn", "SSRN"), ("peerj preprints", "PeerJ Preprints"),
    ("zenodo", "Zenodo"), ("scielo preprints", "SciELO Preprints"),
])

OSF_PREFIX_VENUES = OrderedDict([
    ("10.31234/osf.io/", "PsyArXiv"),
    ("10.31235/osf.io/", "SocArXiv"),
    ("10.35542/osf.io/", "EdArXiv"),
    ("10.31222/osf.io/", "MetaArXiv"),
    ("10.31730/osf.io/", "AfricArXiv"),
    ("10.31219/osf.io/", "OSF Preprints"),
    ("10.17605/osf.io/", "OSF Preprints"),
])

LIKELY_DATACITE_PREFIXES = (
    "10.26434/", "10.31219/", "10.31222/", "10.31234/", "10.31235/",
    "10.31730/", "10.35542/", "10.48550/", "10.17605/", "10.22541/",
)


def venue_from_identifier(target: Target) -> str:
    doi_value = target.doi.lower()
    for prefix, label in OSF_PREFIX_VENUES.items():
        if doi_value.startswith(prefix):
            return label
    prefix_map = [
        ("10.48550/arxiv.", "arXiv"), ("10.26434/chemrxiv", "ChemRxiv"),
        ("10.20944/preprints", "Preprints.org"), ("10.64898/", "openRxiv"),
        ("10.21203/", "Research Square"), ("10.22541/", "Authorea"),
        ("10.1590/scielopreprints.", "SciELO Preprints"),
        ("10.36227/techrxiv.", "TechRxiv"),
        ("10.32942/", "EcoEvoRxiv"),
    ]
    for prefix, label in prefix_map:
        if doi_value.startswith(prefix):
            return label
    source_blob = f"{target.source_identifier}\n{target.value}".lower()
    for needle, label in VENUE_ALIASES.items():
        if needle in source_blob:
            return label
    return ""


def normalize_venue(candidates: Iterable[Any], target: Target) -> str:
    identifier_venue = venue_from_identifier(target)
    if identifier_venue:
        return identifier_venue
    blob = "\n".join(clean_text(value, " ") for value in candidates if value).lower()
    for needle, label in VENUE_ALIASES.items():
        if needle in blob:
            return label
    return ""



class Collector:
    def __init__(
        self,
        delay: float = 0.05,
        seed: str = "PREreview-production-v1",
        state_dir: Path | str = Path("data/prereview/state"),
        resume: bool = True,
        checkpoint_every: int = 10,
        crossref_mailto: str = "",
        openalex_api_key: str = "",
        use_datacite: bool = True,
        use_crossref: bool = True,
        use_openalex: bool = False,
        field_policy: str = "metadata",
        sampling_policy: str = "hash",
        refresh_zenodo: bool = False,
        refresh_metadata: bool = False,
        allow_partial_scan: bool = False,
    ) -> None:
        self.delay = delay
        self.seed = seed
        self.state_dir = Path(state_dir)
        self.resume = resume
        self.checkpoint_every = max(1, checkpoint_every)
        self.crossref_mailto = crossref_mailto.strip()
        self.openalex_api_key = openalex_api_key.strip()
        self.use_datacite = use_datacite
        self.use_crossref = use_crossref
        self.use_openalex = use_openalex
        self.field_policy = field_policy
        self.sampling_policy = sampling_policy
        self.refresh_zenodo = refresh_zenodo
        self.refresh_metadata = refresh_metadata
        self.allow_partial_scan = allow_partial_scan
        if field_policy not in {"empty", "native", "metadata", "broad"}:
            raise ValueError(f"unsupported field policy: {field_policy}")
        if sampling_policy not in {"hash", "coverage"}:
            raise ValueError(f"unsupported sampling policy: {sampling_policy}")
        if self.use_openalex and not self.openalex_api_key:
            logging.warning("OpenAlex is enabled without OPENALEX_API_KEY; requests may fail because the API requires a key.")
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.session = requests.Session()
        user_agent = "Lortzing-OpenReview-PREreview-collector/4.0 (https://github.com/Lortzing/OpenReview"
        if self.crossref_mailto:
            user_agent += f"; mailto:{self.crossref_mailto}"
        user_agent += ")"
        self.session.headers.update({"Accept": "application/json", "User-Agent": user_agent})
        self.metadata_cache: dict[str, dict[str, Any] | None] = {}
        self.request_counts: Counter[str] = Counter()
        self.zenodo_duplicate_records = 0
        self.zenodo_reported_total: int | None = None
        self.zenodo_scan_complete = False

    def cache_path(self, namespace: str, key: str, suffix: str = ".json") -> Path:
        digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
        return self.state_dir / namespace / f"{digest}{suffix}"

    def read_json_cache(self, namespace: str, key: str) -> Any | None:
        if not self.resume:
            return None
        if self.refresh_zenodo and namespace == "zenodo_pages":
            return None
        if self.refresh_metadata and namespace in {"crossref", "datacite", "openalex", "resolved_metadata"}:
            return None
        return load_json(self.cache_path(namespace, key))

    def write_json_cache(self, namespace: str, key: str, value: Any) -> None:
        atomic_write_json(self.cache_path(namespace, key), value)

    def cached_json_request(
        self,
        namespace: str,
        key: str,
        url: str,
        params: dict[str, Any] | None = None,
        retries: int = 5,
        not_found_ok: bool = False,
    ) -> Any:
        cached = self.read_json_cache(namespace, key)
        if isinstance(cached, dict) and cached.get("__cache_status__") == "not_found":
            return None
        if cached is not None:
            return cached
        payload = self.get_json(url, params=params, retries=retries, not_found_ok=not_found_ok)
        if payload is None and not_found_ok:
            self.write_json_cache(namespace, key, {"__cache_status__": "not_found"})
            return None
        self.write_json_cache(namespace, key, payload)
        return payload

    def cached_text_request(self, namespace: str, key: str, url: str, retries: int = 4) -> str:
        path = self.cache_path(namespace, key, ".txt")
        refresh = (self.refresh_metadata and namespace == "arxiv") or (
            self.refresh_zenodo and namespace == "zenodo_files"
        )
        if self.resume and not refresh:
            try:
                return path.read_text(encoding="utf-8")
            except (FileNotFoundError, OSError):
                pass
        value = self.get_text(url, retries=retries)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(path.name + ".tmp")
        temporary.write_text(value, encoding="utf-8")
        temporary.replace(path)
        return value

    def cached_bytes_request(self, namespace: str, key: str, url: str, retries: int = 4) -> bytes:
        path = self.cache_path(namespace, key, ".bin")
        refresh = self.refresh_zenodo and namespace == "zenodo_files"
        if self.resume and not refresh:
            try:
                return path.read_bytes()
            except (FileNotFoundError, OSError):
                pass
        value = self.get_bytes(url, retries=retries)
        atomic_write_bytes(path, value)
        return value

    def get_json(
        self,
        url: str,
        params: dict[str, Any] | None = None,
        retries: int = 5,
        not_found_ok: bool = False,
    ) -> Any:
        error: Exception | None = None
        for attempt in range(retries):
            try:
                response = self.session.get(url, params=params, timeout=60)
                self.request_counts[urlparse(response.url).netloc] += 1
                if response.status_code == 404 and not_found_ok:
                    return None
                if response.status_code == 429:
                    retry_after = response.headers.get("Retry-After")
                    wait = float(retry_after) if retry_after and retry_after.isdigit() else min(30, 2 ** attempt)
                    time.sleep(wait)
                    continue
                if 400 <= response.status_code < 500:
                    response.raise_for_status()
                response.raise_for_status()
                return response.json()
            except requests.HTTPError as exc:
                error = exc
                if exc.response is not None and 400 <= exc.response.status_code < 500 and exc.response.status_code != 429:
                    break
                time.sleep(1 + attempt)
            except Exception as exc:
                error = exc
                time.sleep(1 + attempt)
        raise RuntimeError(f"GET failed: {url}: {error}")

    def get_text(self, url: str, retries: int = 4) -> str:
        error: Exception | None = None
        for attempt in range(retries):
            try:
                response = self.session.get(url, timeout=60)
                self.request_counts[urlparse(response.url).netloc] += 1
                if response.status_code == 429:
                    time.sleep(min(30, 2 ** attempt))
                    continue
                response.raise_for_status()
                return response.text
            except Exception as exc:
                error = exc
                time.sleep(1 + attempt)
        raise RuntimeError(f"GET failed: {url}: {error}")

    def get_bytes(self, url: str, retries: int = 4) -> bytes:
        error: Exception | None = None
        for attempt in range(retries):
            try:
                response = self.session.get(url, timeout=90)
                self.request_counts[urlparse(response.url).netloc] += 1
                if response.status_code == 429:
                    time.sleep(min(30, 2 ** attempt))
                    continue
                response.raise_for_status()
                return response.content
            except Exception as exc:
                error = exc
                time.sleep(1 + attempt)
        raise RuntimeError(f"GET failed: {url}: {error}")

    def iter_zenodo_records(self, max_pages: int, page_size: int = 25):
        records_seen = 0
        seen_record_ids: set[str] = set()
        reported_total: int | None = None
        exhausted = False
        for page in range(1, max_pages + 1):
            params = {
                "sort": "publication-desc",
                "access_status": "open",
                "size": page_size,
                "page": page,
            }
            cache_key = f"community-v2|{COMMUNITY}|publication-desc|open|{page_size}|{page}"
            cache_hit = (
                self.resume
                and not self.refresh_zenodo
                and self.cache_path("zenodo_pages", cache_key).exists()
            )
            payload = self.cached_json_request("zenodo_pages", cache_key, ZENODO_API, params=params)
            hits = (payload.get("hits") or {}).get("hits") or []
            total = (payload.get("hits") or {}).get("total")
            if isinstance(total, dict):
                total = total.get("value")
            if isinstance(total, int):
                reported_total = total
                self.zenodo_reported_total = total
            logging.info(
                "Zenodo page %d: %d records (reported total=%s, source=%s)",
                page,
                len(hits),
                total,
                "cache" if cache_hit else "network",
            )
            if not hits:
                exhausted = True
                break
            for record in hits:
                record_id = str(record.get("id") or record.get("recid") or record.get("doi") or "")
                if not record_id or record_id in seen_record_ids:
                    self.zenodo_duplicate_records += 1
                    continue
                seen_record_ids.add(record_id)
                records_seen += 1
                yield record
            if len(hits) < page_size:
                exhausted = True
                break
        self.zenodo_scan_complete = (
            records_seen >= reported_total if reported_total is not None else exhausted
        )
        if not self.zenodo_scan_complete:
            message = (
                f"Zenodo scan incomplete or changed during pagination: scanned {records_seen} unique records "
                f"of {reported_total}; increase --max-pages above {max_pages} or rerun with --refresh-zenodo."
            )
            if not self.allow_partial_scan:
                raise RuntimeError(message)
            logging.warning("%s Partial output was explicitly allowed for testing.", message)

    def html_file_body(self, record: dict[str, Any]) -> tuple[str, str]:
        record_id = str(record.get("id") or record.get("recid") or "")
        for file_info in record.get("files") or []:
            if not isinstance(file_info, dict) or not str(file_info.get("key") or "").lower().endswith(".html"):
                continue
            url = (file_info.get("links") or {}).get("self")
            if not url:
                continue
            try:
                body = clean_review_html(self.cached_text_request("zenodo_files", f"{record_id}|html", url))
                if body:
                    return body, "html_attachment"
            except Exception as exc:
                logging.warning("Unable to read HTML body for record %s: %s", record_id, exc)
        return "", ""

    def review_body(self, record: dict[str, Any]) -> str:
        body = clean_review_html((record.get("metadata") or {}).get("description"))
        if body:
            return body
        body, _ = self.html_file_body(record)
        return body

    def discussion_body(self, record: dict[str, Any]) -> tuple[str, str]:
        body, source = self.html_file_body(record)
        if body:
            return body, source
        description = clean_review_html((record.get("metadata") or {}).get("description"))
        return description, "description" if description else ""

    def response_body(self, record: dict[str, Any]) -> tuple[str, str]:
        # Legacy PREreview author responses may store only a one-sentence landing
        # description and keep the full response in a PDF attachment.
        if fitz is not None:
            for file_info in record.get("files") or []:
                if not isinstance(file_info, dict):
                    continue
                if not str(file_info.get("key") or "").lower().endswith(".pdf"):
                    continue
                url = (file_info.get("links") or {}).get("self")
                if not url:
                    continue
                try:
                    document = fitz.open(stream=self.cached_bytes_request("zenodo_files", f"{record.get('id') or url}|pdf", url), filetype="pdf")
                    body = clean_text("\n\n".join(page.get_text("text") for page in document))
                    document.close()
                    if body:
                        return body, "pdf_attachment"
                except Exception as exc:
                    logging.warning("Unable to extract response PDF for record %s: %s", record.get("id"), exc)
        html_body, html_source = self.html_file_body(record)
        if html_body:
            return html_body, html_source
        description = clean_review_html((record.get("metadata") or {}).get("description"))
        return description, "description" if description else ""

    @staticmethod
    def is_legacy_response_record(record: dict[str, Any]) -> bool:
        title = clean_text((record.get("metadata") or {}).get("title"), " ")
        return bool(re.match(
            r"^(?:(?:author\s+)?response\s+to\s+.+?\breview\b|rebuttal\b|reply\s+to\b)",
            title,
            re.I,
        ))

    @staticmethod
    def is_discussion_record(record: dict[str, Any]) -> bool:
        metadata = record.get("metadata") or {}
        title = clean_text(metadata.get("title"), " ")
        description = clean_text(metadata.get("description"), " ")[:500]
        resource_type = metadata.get("resource_type") or {}
        is_other_publication = (
            isinstance(resource_type, dict)
            and resource_type.get("type") == "publication"
            and resource_type.get("subtype") == "other"
        )
        references_review = any(
            isinstance(item, dict)
            and str(item.get("relation") or "").casefold() == "references"
            and str((item.get("resource_type") or "")).casefold() == "publication-peerreview"
            for item in metadata.get("related_identifiers") or []
        )
        has_comment_html = any(
            isinstance(item, dict) and str(item.get("key") or "").casefold() == "comment.html"
            for item in record.get("files") or []
        )
        return bool(
            (is_other_publication and references_review and has_comment_html)
            or
            re.match(r"^comment\s+on\s+a\s+PREreview\b", title, re.I)
            or "permanently preserved version of a comment on a prereview" in description.casefold()
        )

    @staticmethod
    def interaction_relations(record: dict[str, Any]) -> list[str]:
        identifiers: list[str] = []
        for item in (record.get("metadata") or {}).get("related_identifiers") or []:
            if not isinstance(item, dict):
                continue
            if str(item.get("relation") or "").lower() not in {"cites", "isresponseTo".lower(), "references"}:
                continue
            candidate = normalize_doi(item.get("identifier") or item.get("id") or item.get("value"))
            if candidate and candidate not in identifiers:
                identifiers.append(candidate)
        return identifiers

    @staticmethod
    def relation_matches_target(identifier: str, target: Target) -> bool:
        if not identifier:
            return False
        family_key, _ = family_and_version(identifier, "doi")
        return family_key == target.family_key or identifier == target.doi

    def scan(self, max_pages: int) -> tuple[
        OrderedDict[str, Family],
        dict[str, Any],
        dict[str, list[AuthorResponse]],
        dict[str, list[DiscussionComment]],
        set[str],
    ]:
        families: OrderedDict[str, Family] = OrderedDict()
        seen_records: set[str] = set()
        stats: Counter[str] = Counter()
        review_types: Counter[str] = Counter()
        possible_responses: list[dict[str, Any]] = []
        possible_discussions: list[dict[str, Any]] = []
        responses_by_review: dict[str, list[AuthorResponse]] = {}
        discussions_by_review: dict[str, list[DiscussionComment]] = {}
        interaction_family_keys: set[str] = set()
        pending_interactions: list[tuple[str, dict[str, Any]]] = []

        for record in self.iter_zenodo_records(max_pages):
            record_id = str(record.get("id") or record.get("recid") or "")
            if not record_id or record_id in seen_records:
                stats["duplicate_or_missing_record_id"] += 1
                continue
            seen_records.add(record_id)
            stats["records_seen"] += 1
            metadata = record.get("metadata") or {}
            raw_title = clean_text(metadata.get("title"), " ")
            if self.is_legacy_response_record(record):
                pending_interactions.append(("author_response", record))
                stats["author_response_records_seen"] += 1
                continue
            if self.is_discussion_record(record):
                pending_interactions.append(("discussion", record))
                stats["discussion_records_seen"] += 1
                continue
            target, reason = explicit_target(record)
            if target is None:
                stats[reason] += 1
                continue
            if reason != "ok":
                stats[reason] += 1
            body = self.review_body(record)
            if not body:
                stats["missing_review_body"] += 1
                continue
            if any(phrase in body.lower() for phrase in PRESERVATION_PHRASES):
                stats["boilerplate_cleaning_failed"] += 1
                continue
            review_type, title_hint = review_type_and_title(raw_title)
            review_types[review_type] += 1
            creator_names, creator_orcids = creator_identities_from_record(record)
            review = Review(
                review_id=review_doi(record),
                record_id=record_id,
                target=target,
                comment=body,
                review_date=str(metadata.get("publication_date") or record.get("created") or "")[:10],
                review_type=review_type,
                title_hint=title_hint,
                record_url=(record.get("links") or {}).get("self_html") or f"https://zenodo.org/records/{record_id}",
                creators=creator_names,
                creator_orcids=creator_orcids,
                subjects=subjects_from_record(record),
            )
            family = families.setdefault(target.family_key, Family(target.family_key))
            bucket = family.targets.setdefault(target.value, TargetBucket(target))
            if review.review_id in {item.review_id for item in bucket.reviews}:
                stats["duplicate_review_id"] += 1
                continue
            bucket.reviews.append(review)
            stats["reviews_accepted"] += 1

        reviews_by_id = {
            review.review_id: review
            for family in families.values()
            for bucket in family.targets.values()
            for review in bucket.reviews
        }
        for interaction_kind, record in pending_interactions:
            record_id = str(record.get("id") or record.get("recid") or "")
            metadata = record.get("metadata") or {}
            raw_title = clean_text(metadata.get("title"), " ")
            related = self.interaction_relations(record)
            linked_review_ids = [identifier for identifier in related if identifier in reviews_by_id]
            target_review_id = linked_review_ids[0] if len(linked_review_ids) == 1 else ""
            target_review = reviews_by_id.get(target_review_id)
            target_verified = bool(
                target_review
                and any(
                    identifier != target_review_id
                    and self.relation_matches_target(identifier, target_review.target)
                    for identifier in related
                )
            )
            body, body_source = (
                self.response_body(record)
                if interaction_kind == "author_response"
                else self.discussion_body(record)
            )
            detail = {
                "record_id": record_id,
                "title": raw_title,
                "interaction_doi": review_doi(record),
                "related_identifiers": related,
                "target_review_id": target_review_id,
                "target_paper_identifier": target_review.target.value if target_review else "",
                "target_relation_verified": target_verified,
                "body_source": body_source,
                "body_length": len(body),
            }
            if interaction_kind == "author_response":
                possible_responses.append(detail)
            else:
                possible_discussions.append(detail)
            if not target_review_id or target_review is None or not body:
                stats[f"{interaction_kind}_unresolved"] += 1
                continue
            family_key = target_review.target.family_key
            interaction_family_keys.add(family_key)
            creator_names, creator_orcids = creator_identities_from_record(record)
            if interaction_kind == "author_response":
                response = AuthorResponse(
                    response_id=review_doi(record),
                    record_id=record_id,
                    target_review_id=target_review_id,
                    family_key=family_key,
                    content=body,
                    response_date=str(metadata.get("publication_date") or record.get("created") or "")[:10],
                    record_url=(record.get("links") or {}).get("self_html") or f"https://zenodo.org/records/{record_id}",
                    creators=creator_names,
                    creator_orcids=creator_orcids,
                    body_source=body_source,
                )
                responses_by_review.setdefault(target_review_id, []).append(response)
                stats["author_responses_accepted"] += 1
            else:
                discussion = DiscussionComment(
                    comment_id=review_doi(record),
                    record_id=record_id,
                    target_review_id=target_review_id,
                    family_key=family_key,
                    content=body,
                    comment_date=str(metadata.get("publication_date") or record.get("created") or "")[:10],
                    record_url=(record.get("links") or {}).get("self_html") or f"https://zenodo.org/records/{record_id}",
                    creators=creator_names,
                    creator_orcids=creator_orcids,
                    body_source=body_source,
                    target_relation_verified=target_verified,
                )
                discussions_by_review.setdefault(target_review_id, []).append(discussion)
                stats["discussion_comments_accepted"] += 1
                if not target_verified:
                    stats["discussion_target_relation_unverified"] += 1

        report = dict(stats)
        report["zenodo_duplicate_records_during_pagination"] = self.zenodo_duplicate_records
        report["zenodo_reported_total"] = self.zenodo_reported_total
        report["zenodo_scan_complete"] = self.zenodo_scan_complete
        review_dates = [
            review.review_date
            for family in families.values()
            for bucket in family.targets.values()
            for review in bucket.reviews
            if review.review_date
        ]
        report["reviews_before_commenting_relaunch"] = sum(
            date < COMMENTING_RELAUNCH_DATE for date in review_dates
        )
        report["reviews_on_or_after_commenting_relaunch"] = sum(
            date >= COMMENTING_RELAUNCH_DATE for date in review_dates
        )
        report["historical_comment_completeness"] = "unverified_before_commenting_relaunch"
        report["strict_families"] = len(families)
        report["strict_target_versions"] = sum(len(family.targets) for family in families.values())
        report["review_types"] = dict(review_types)
        report["author_response_records"] = possible_responses[:50]
        report["author_response_record_count"] = len(possible_responses)
        report["discussion_records"] = possible_discussions[:100]
        report["discussion_record_count"] = len(possible_discussions)
        report["author_response_family_count"] = len({
            response.family_key for values in responses_by_review.values() for response in values
        })
        report["discussion_family_count"] = len({
            discussion.family_key for values in discussions_by_review.values() for discussion in values
        })
        report["interaction_family_count"] = len(interaction_family_keys)
        return families, report, responses_by_review, discussions_by_review, interaction_family_keys

    def crossref_metadata(self, doi_value: str) -> dict[str, Any]:
        params = {"mailto": self.crossref_mailto} if self.crossref_mailto else None
        payload = self.cached_json_request(
            "crossref", doi_value,
            "https://api.crossref.org/works/" + quote(doi_value, safe=""),
            params=params, retries=3, not_found_ok=True,
        )
        if not payload:
            return {}
        msg = payload.get("message") or {}
        title_items = msg.get("title") or []
        title = clean_title(title_items[0] if isinstance(title_items, list) and title_items else title_items)
        authors = names(msg.get("author"))
        year = date_year(msg, ("posted", "published-online", "published", "issued", "published-print"))
        venue_candidates: list[Any] = []
        for key in ("container-title", "short-container-title", "institution", "group-title"):
            value = msg.get(key)
            venue_candidates.extend(value if isinstance(value, list) else [value])
        venue_candidates.append(msg.get("URL"))
        for link in msg.get("link") or []:
            if isinstance(link, dict):
                venue_candidates.append(link.get("URL"))
        return {
            "title": title, "authors": authors, "author_orcids": orcids_from_people(msg.get("author")), "year": year,
            "venue_candidates": venue_candidates,
            "fields": [clean_text(item, " ") for item in msg.get("subject") or [] if clean_text(item, " ")],
            "source": "Crossref",
        }

    def datacite_metadata(self, doi_value: str) -> dict[str, Any]:
        payload = self.cached_json_request(
            "datacite", doi_value,
            "https://api.datacite.org/dois/" + quote(doi_value, safe=""),
            retries=3, not_found_ok=True,
        )
        if not payload:
            return {}
        attributes = ((payload.get("data") or {}).get("attributes") or {})
        titles = attributes.get("titles") or []
        title = ""
        for item in titles:
            if isinstance(item, dict) and item.get("title"):
                title = clean_title(item["title"])
                break
        creators = attributes.get("creators") or []
        authors: list[str] = []
        for creator in creators:
            if not isinstance(creator, dict):
                continue
            name = creator.get("name") or " ".join(filter(None, [creator.get("givenName"), creator.get("familyName")]))
            name = clean_text(name, " ")
            if name and name not in authors:
                authors.append(name)
        year = str(attributes.get("publicationYear") or "")
        if not re.fullmatch(r"\d{4}", year):
            published = str(attributes.get("published") or "")
            year = published[:4] if re.match(r"\d{4}", published) else ""
        container = attributes.get("container") or {}
        venue_candidates: list[Any] = [
            attributes.get("publisher"), attributes.get("url"),
            container.get("title") if isinstance(container, dict) else None,
        ]
        fields = []
        for subject in attributes.get("subjects") or []:
            value = subject.get("subject") if isinstance(subject, dict) else subject
            value = clean_text(value, " ")
            if value and value not in fields:
                fields.append(value)
        return {
            "title": title, "authors": authors, "author_orcids": orcids_from_people(creators), "year": year,
            "venue_candidates": venue_candidates, "fields": fields, "source": "DataCite",
        }

    def openalex_metadata(self, doi_value: str) -> dict[str, Any]:
        params = {"api_key": self.openalex_api_key} if self.openalex_api_key else None
        payload = self.cached_json_request(
            "openalex", doi_value,
            "https://api.openalex.org/works/https://doi.org/" + quote(doi_value, safe=""),
            params=params, retries=3, not_found_ok=True,
        )
        if not payload:
            return {}
        source = ((payload.get("primary_location") or {}).get("source") or {})
        fields: list[str] = []
        primary_topic = payload.get("primary_topic") or {}
        if primary_topic.get("display_name"):
            fields.append(clean_text(primary_topic["display_name"], " "))
        for topic in payload.get("topics") or []:
            name = clean_text((topic or {}).get("display_name"), " ") if isinstance(topic, dict) else ""
            if name and name not in fields:
                fields.append(name)
        return {
            "title": clean_title(payload.get("title") or payload.get("display_name")),
            "authors": [
                clean_text((authorship.get("author") or {}).get("display_name"), " ")
                for authorship in payload.get("authorships") or []
                if clean_text((authorship.get("author") or {}).get("display_name"), " ")
            ],
            "author_orcids": [
                orcid
                for authorship in payload.get("authorships") or []
                if (orcid := normalize_orcid((authorship.get("author") or {}).get("orcid")))
            ],
            "year": str(payload.get("publication_year") or ""),
            "venue_candidates": [
                source.get("display_name"), source.get("host_organization_name"),
                (payload.get("primary_location") or {}).get("landing_page_url"),
                (payload.get("primary_location") or {}).get("pdf_url"),
            ],
            "fields": fields, "source": "OpenAlex",
        }

    def arxiv_metadata(self, arxiv_id: str) -> dict[str, Any]:
        xml = self.cached_text_request(
            "arxiv", arxiv_id,
            "https://export.arxiv.org/api/query?id_list=" + quote(arxiv_id, safe=""), retries=3,
        )
        root = ET.fromstring(xml)
        ns = {"atom": "http://www.w3.org/2005/Atom"}
        entry = root.find("atom:entry", ns)
        if entry is None:
            return {}
        title = clean_title(entry.findtext("atom:title", default="", namespaces=ns))
        authors = [clean_text(a.findtext("atom:name", default="", namespaces=ns), " ") for a in entry.findall("atom:author", ns)]
        published = entry.findtext("atom:published", default="", namespaces=ns)
        year = published[:4] if re.match(r"\d{4}", published) else ""
        fields = [
            clean_text(node.attrib.get("term"), " ") for node in entry.findall("atom:category", ns)
            if clean_text(node.attrib.get("term"), " ")
        ]
        return {
            "title": title, "authors": [a for a in authors if a], "author_orcids": [], "year": year,
            "venue_candidates": ["arXiv"], "fields": fields, "source": "arXiv API",
        }

    def resolve(self, target: Target) -> dict[str, Any] | None:
        resolver_signature = f"dc={int(self.use_datacite)}|cr={int(self.use_crossref)}|oa={int(self.use_openalex)}"
        cache_key = f"{target.kind}:{target.value}|{resolver_signature}"
        if cache_key in self.metadata_cache:
            return self.metadata_cache[cache_key]
        persisted = self.read_json_cache("resolved_metadata", cache_key)
        if isinstance(persisted, dict) and persisted.get("state_version") == STATE_VERSION:
            value = persisted.get("value") if persisted.get("found") else None
            self.metadata_cache[cache_key] = value
            return value
        merged: dict[str, Any] = {
            "title": "", "authors": [], "author_orcids": [], "year": "", "venue_candidates": [],
            "field_candidates": [], "sources": [],
            "provenance": {"title": [], "authors": [], "author_orcids": [], "year": [], "venue": [], "field": []},
        }
        resolvers: list[Any] = []
        if target.kind == "arxiv":
            resolvers = [lambda: self.arxiv_metadata(target.value)]
        elif target.kind == "doi":
            datacite_first = target.doi.startswith(LIKELY_DATACITE_PREFIXES)
            registry_resolvers = []
            if datacite_first:
                if self.use_datacite: registry_resolvers.append(lambda: self.datacite_metadata(target.doi))
                if self.use_crossref: registry_resolvers.append(lambda: self.crossref_metadata(target.doi))
            else:
                if self.use_crossref: registry_resolvers.append(lambda: self.crossref_metadata(target.doi))
                if self.use_datacite: registry_resolvers.append(lambda: self.datacite_metadata(target.doi))
            resolvers.extend(registry_resolvers)
            if target.doi.startswith("10.48550/arxiv."):
                arxiv_id = target.doi.split("10.48550/arxiv.", 1)[1]
                resolvers.append(lambda arxiv_id=arxiv_id: self.arxiv_metadata(arxiv_id))
            if self.use_openalex:
                resolvers.append(lambda: self.openalex_metadata(target.doi))
        for resolver in resolvers:
            try:
                value = resolver() or {}
            except Exception as exc:
                logging.debug("metadata resolver failed for %s: %s", target.value, exc)
                continue
            source = value.get("source") or "unknown"
            if not value:
                continue
            if source not in merged["sources"]:
                merged["sources"].append(source)
            for field_name in ("title", "authors", "year"):
                candidate = value.get(field_name)
                if field_name == "year" and candidate and not re.fullmatch(r"\d{4}", str(candidate)):
                    candidate = ""
                if not merged[field_name] and candidate:
                    merged[field_name] = candidate
                    merged["provenance"][field_name].append(source)
            for orcid in value.get("author_orcids") or []:
                normalized = normalize_orcid(orcid)
                if normalized and normalized not in merged["author_orcids"]:
                    merged["author_orcids"].append(normalized)
                    if source not in merged["provenance"]["author_orcids"]:
                        merged["provenance"]["author_orcids"].append(source)
            venue_values = value.get("venue_candidates") or []
            if venue_values:
                merged["venue_candidates"].extend(venue_values)
                merged["provenance"]["venue"].append(source)
            for field_name in value.get("fields") or []:
                normalized = clean_text(field_name, " ")
                if normalized and not any(item["value"] == normalized for item in merged["field_candidates"]):
                    merged["field_candidates"].append({"value": normalized, "source": source})
                    merged["provenance"]["field"].append(source)
            # OpenAlex is a final fallback, not a reason to make an extra request after core metadata is complete.
            if merged["title"] and merged["authors"] and merged["year"] and normalize_venue(merged["venue_candidates"], target):
                break
        merged["title"] = clean_title(merged["title"])
        merged["venue"] = normalize_venue(merged["venue_candidates"], target)
        result = merged if any((merged["title"], merged["authors"], merged["year"])) else None
        self.metadata_cache[cache_key] = result
        self.write_json_cache(
            "resolved_metadata", cache_key,
            {"state_version": STATE_VERSION, "found": result is not None, "value": result},
        )
        time.sleep(self.delay)
        return result

    def family_hash(self, key: str) -> str:
        return hashlib.sha256(f"{self.seed}\0{key}".encode()).hexdigest()

    def build_family(
        self,
        family: Family,
        responses_by_review: dict[str, list[AuthorResponse]],
        discussions_by_review: dict[str, list[DiscussionComment]] | None = None,
    ) -> tuple[dict[str, Any] | None, dict[str, Any]]:
        discussions_by_review = discussions_by_review or {}
        buckets = list(family.targets.values())
        buckets.sort(key=lambda bucket: (
            bucket.target.version is None,
            bucket.target.version if bucket.target.version is not None else 10**9,
            min((review.review_date for review in bucket.reviews), default="9999"),
            bucket.target.value,
        ))
        resolved: list[tuple[TargetBucket, dict[str, Any]]] = []
        rejection: dict[str, Any] = {"family_key": family.key, "targets": [b.target.value for b in buckets]}
        latest_bucket = sorted(
            buckets,
            key=lambda bucket: (bucket.target.version is not None, bucket.target.version or 0, bucket.target.value),
        )[-1]
        for bucket in buckets:
            metadata = self.resolve(bucket.target)
            if metadata:
                resolved.append((bucket, metadata))
        if not resolved:
            rejection["reason"] = "metadata_failed_all_versions"
            return None, rejection
        latest = next((metadata for bucket, metadata in resolved if bucket.target.value == latest_bucket.target.value), None)
        if latest is None:
            rejection["reason"] = "latest_version_metadata_failed"
            return None, rejection
        hints = [clean_title(review.title_hint) for bucket in buckets for review in bucket.reviews if review.title_hint]
        hint = Counter(hints).most_common(1)[0][0] if hints else ""
        title = clean_title(latest.get("title") or hint)
        similarity = title_similarity(title, hint) if hint else 1.0
        authors = latest.get("authors") or []
        author_orcids = latest.get("author_orcids") or []
        years = [str(metadata.get("year")) for _, metadata in resolved if re.fullmatch(r"\d{4}", str(metadata.get("year") or ""))]
        year = min(years) if years else ""
        venue = (
            latest.get("venue")
            or next((metadata.get("venue") for _, metadata in reversed(resolved) if metadata.get("venue")), "")
            or venue_from_identifier(latest_bucket.target)
        )
        missing = [name for name, value in (("title", title), ("authors", authors), ("year", year), ("venue", venue)) if not value]
        if missing:
            rejection.update({"reason": "metadata_incomplete", "missing": missing, "metadata_sources": latest.get("sources")})
            return None, rejection
        metadata_warnings: list[dict[str, Any]] = []
        if similarity < 0.58:
            metadata_warnings.append({
                "warning": "title_mismatch",
                "metadata_title": title,
                "review_title": hint,
                "similarity": similarity,
                "resolution": "accepted because Zenodo explicitly declares relation=reviews to the DOI",
            })
        if any(fragment in f" {venue.lower()}" for fragment in FORBIDDEN_VENUE_FRAGMENTS):
            rejection.update({"reason": "publisher_used_as_venue", "venue": venue})
            return None, rejection
        if not (1900 <= int(year) <= CURRENT_YEAR + 1):
            rejection.update({"reason": "invalid_year", "year": year})
            return None, rejection

        rounds: list[dict[str, Any]] = []
        audit_rounds: list[dict[str, Any]] = []
        all_dedup: list[dict[str, Any]] = []
        all_discussion_dedup: list[dict[str, Any]] = []
        for round_index, bucket in enumerate(buckets, start=1):
            original_reviews = sorted(bucket.reviews, key=lambda review: (review.review_date, review.review_id))
            kept_reviews: list[Review] = []
            cleaned_review_bodies: dict[str, str] = {}
            body_hash_to_review: dict[str, Review] = {}
            duplicate_to_kept: dict[str, str] = {}
            duplicate_details: list[dict[str, Any]] = []
            for review in original_reviews:
                cleaned_review = clean_comment(review.comment)
                cleaned_review_bodies[review.review_id] = cleaned_review
                body_hash = normalized_comment_hash(cleaned_review)
                existing = body_hash_to_review.get(body_hash)
                if existing is None:
                    body_hash_to_review[body_hash] = review
                    kept_reviews.append(review)
                else:
                    duplicate_to_kept[review.review_id] = existing.review_id
                    detail = {
                        "target_identifier": bucket.target.value,
                        "round": round_index,
                        "kept_review_id": existing.review_id,
                        "kept_review_record_id": existing.record_id,
                        "kept_review_url": existing.record_url,
                        "removed_review_id": review.review_id,
                        "removed_review_record_id": review.record_id,
                        "removed_review_url": review.record_url,
                        "removed_review_date": review.review_date,
                        "removed_reviewers": review.creators,
                        "normalized_comment_sha256": body_hash,
                    }
                    duplicate_details.append(detail)
                    all_dedup.append(detail)
            comments = [
                {
                    "Reviewer_ID": review.review_id,
                    "Reviewer": review.creators,
                    "Reviewer_ORCID": review.creator_orcids,
                    "Review_Date": review.review_date,
                    "Comment": cleaned_review_bodies[review.review_id],
                }
                for review in kept_reviews
            ]
            round_responses: list[dict[str, Any]] = []
            audit_responses: list[dict[str, Any]] = []
            round_discussions: list[dict[str, Any]] = []
            audit_discussions: list[dict[str, Any]] = []
            seen_response_ids: set[str] = set()
            seen_discussion_ids: set[str] = set()
            discussion_keys: dict[tuple[str, str, tuple[tuple[str, ...], ...]], str] = {}
            duplicate_discussion_details: list[dict[str, Any]] = []
            review_by_id = {review.review_id: review for review in original_reviews}
            for original_review in original_reviews:
                canonical_review_id = duplicate_to_kept.get(original_review.review_id, original_review.review_id)
                canonical_review = review_by_id.get(canonical_review_id, original_review)
                for response in responses_by_review.get(original_review.review_id, []):
                    if response.family_key != family.key or response.response_id in seen_response_ids:
                        continue
                    seen_response_ids.add(response.response_id)
                    cleaned_response = author_only_response(
                        response.content,
                        cleaned_review_bodies.get(canonical_review.review_id, canonical_review.comment),
                    )
                    round_responses.append({
                        "Response_ID": response.response_id,
                        "To_Reviewer_ID": canonical_review_id,
                        "Responder": response.creators,
                        "Responder_ORCID": response.creator_orcids,
                        "Response_Date": response.response_date,
                        "Response": cleaned_response,
                    })
                    audit_responses.append({
                        "response_id": response.response_id,
                        "response_record_id": response.record_id,
                        "response_url": response.record_url,
                        "response_date": response.response_date,
                        "authors": response.creators,
                        "author_orcids": response.creator_orcids,
                        "body_source": response.body_source,
                        "to_review_id_original": original_review.review_id,
                        "to_review_id_output": canonical_review_id,
                    })
                for discussion in sorted(
                    discussions_by_review.get(original_review.review_id, []),
                    key=lambda value: (value.comment_date, value.comment_id),
                ):
                    if discussion.family_key != family.key or discussion.comment_id in seen_discussion_ids:
                        continue
                    seen_discussion_ids.add(discussion.comment_id)
                    cleaned_discussion = clean_comment(discussion.content)
                    participant_key = tuple(sorted(
                        key for creator in discussion.creators
                        if (key := normalized_person_tokens(creator))
                    ))
                    duplicate_key = (
                        canonical_review_id,
                        normalized_comment_hash(cleaned_discussion),
                        participant_key,
                    )
                    kept_discussion_id = discussion_keys.get(duplicate_key)
                    if kept_discussion_id:
                        duplicate_detail = {
                            "round": round_index,
                            "target_review_id": canonical_review_id,
                            "kept_comment_id": kept_discussion_id,
                            "removed_comment_id": discussion.comment_id,
                            "removed_comment_record_id": discussion.record_id,
                            "removed_comment_url": discussion.record_url,
                            "commenters": discussion.creators,
                            "normalized_comment_sha256": duplicate_key[1],
                        }
                        duplicate_discussion_details.append(duplicate_detail)
                        all_discussion_dedup.append(duplicate_detail)
                        continue
                    discussion_keys[duplicate_key] = discussion.comment_id
                    role, role_evidence = discussion_participant_role(
                        discussion,
                        authors,
                        canonical_review.creators,
                        author_orcids,
                        canonical_review.creator_orcids,
                    )
                    round_discussions.append({
                        "Comment_ID": discussion.comment_id,
                        "In_Reply_To_Reviewer_ID": canonical_review_id,
                        "Commenter": discussion.creators,
                        "Commenter_ORCID": discussion.creator_orcids,
                        "Commenter_Role": role,
                        "Comment_Type": {
                            "author": "author_response",
                            "reviewer": "reviewer_followup",
                            "commenter": "community_comment",
                        }[role],
                        "Comment_Date": discussion.comment_date,
                        "Comment": cleaned_discussion,
                    })
                    audit_discussions.append({
                        "comment_id": discussion.comment_id,
                        "comment_record_id": discussion.record_id,
                        "comment_url": discussion.record_url,
                        "comment_date": discussion.comment_date,
                        "commenters": discussion.creators,
                        "commenter_orcids": discussion.creator_orcids,
                        "participant_role": role,
                        "participant_role_evidence": role_evidence,
                        "body_source": discussion.body_source,
                        "target_relation_verified": discussion.target_relation_verified,
                        "to_review_id_original": original_review.review_id,
                        "to_review_id_output": canonical_review_id,
                    })
            round_responses.sort(key=lambda value: (value.get("Response_Date") or "", value.get("Response_ID") or ""))
            round_discussions.sort(key=lambda value: (value.get("Comment_Date") or "", value.get("Comment_ID") or ""))
            audit_responses.sort(key=lambda value: (value.get("response_date") or "", value.get("response_id") or ""))
            audit_discussions.sort(key=lambda value: (value.get("comment_date") or "", value.get("comment_id") or ""))
            timeline = [
                {
                    "Event_Type": "review",
                    "Event_ID": review.review_id,
                    "Actor_Role": "reviewer",
                    "Date": review.review_date,
                    "In_Reply_To": "",
                }
                for review in kept_reviews
            ]
            timeline.extend({
                "Event_Type": "legacy_author_response",
                "Event_ID": response["Response_ID"],
                "Actor_Role": "author",
                "Date": response["Response_Date"],
                "In_Reply_To": response["To_Reviewer_ID"],
            } for response in round_responses)
            timeline.extend({
                "Event_Type": discussion["Comment_Type"],
                "Event_ID": discussion["Comment_ID"],
                "Actor_Role": discussion["Commenter_Role"],
                "Date": discussion["Comment_Date"],
                "In_Reply_To": discussion["In_Reply_To_Reviewer_ID"],
            } for discussion in round_discussions)
            timeline.sort(key=lambda value: (
                value.get("Date") or "",
                0 if value.get("Event_Type") == "review" else 1,
                value.get("Event_ID") or "",
            ))
            rounds.append({
                "Round": round_index,
                "Target_DOI": bucket.target.doi,
                "Comments": comments,
                "Response": round_responses,
                "Discussion": round_discussions,
                "Timeline": timeline,
            })
            audit_rounds.append({
                "round": round_index,
                "target_identifier": bucket.target.value,
                "target_doi": bucket.target.doi,
                "explicit_version": bucket.target.version,
                "reviews": [
                    {
                        "review_id": review.review_id,
                        "review_record_id": review.record_id,
                        "review_url": review.record_url,
                        "review_date": review.review_date,
                        "review_type": review.review_type,
                        "reviewers": review.creators,
                        "reviewer_orcids": review.creator_orcids,
                    }
                    for review in kept_reviews
                ],
                "duplicate_review_records_removed": duplicate_details,
                "duplicate_discussion_records_removed": duplicate_discussion_details,
                "responses": audit_responses,
                "discussion": audit_discussions,
            })

        field_candidates: list[dict[str, str]] = []
        if self.field_policy != "empty":
            for bucket in buckets:
                for review in bucket.reviews:
                    for value in review.subjects:
                        normalized = clean_text(value, " ")
                        if normalized and not any(item["value"] == normalized for item in field_candidates):
                            field_candidates.append({"value": normalized, "source": "PREreview/Zenodo subject"})
            for _, metadata in resolved:
                for item in metadata.get("field_candidates") or []:
                    source = item.get("source") or ""
                    allowed = source == "arXiv API" or self.field_policy in {"metadata", "broad"} and source in {"Crossref", "DataCite"}
                    if allowed and item.get("value") and not any(existing["value"] == item["value"] for existing in field_candidates):
                        field_candidates.append({"value": item["value"], "source": source})
        if self.field_policy == "broad" and not field_candidates:
            field_candidates.append({"value": infer_broad_field(title, venue), "source": "controlled title/venue inference"})
        field_value = "; ".join(item["value"] for item in field_candidates[:8])

        latest_doi = latest_bucket.target.doi
        paper = {
            "DOI": latest_doi,
            "PaperTitle": title,
            "Authors": authors,
            "Source": "PREreview",
            "Venue": venue,
            "Year": year,
            "PeerReview": rounds,
            "Field": field_value,
        }
        provenance = latest.get("provenance") or {}
        year_sources = sorted({
            source
            for _, metadata in resolved
            if str(metadata.get("year") or "") == year
            for source in (metadata.get("provenance") or {}).get("year", [])
        })
        identifier_venue = venue_from_identifier(latest_bucket.target)
        venue_sources = (
            ["DOI prefix / explicit target identifier"]
            if identifier_venue and identifier_venue == venue
            else provenance.get("venue") or []
        )
        audit = {
            "family_key": family.key,
            "output_doi": latest_doi,
            "title_similarity": round(similarity, 4),
            "title_hint": hint,
            "metadata_warnings": metadata_warnings,
            "metadata_sources": latest.get("sources") or [],
            "field": field_value,
            "field_candidates": field_candidates,
            "field_policy": self.field_policy,
            "field_level_provenance": {
                "DOI": ["Zenodo related_identifier with relation=reviews"],
                "PaperTitle": provenance.get("title") or (["PREreview review title"] if title == hint and hint else []),
                "Authors": provenance.get("authors") or [],
                "Author_ORCIDs": provenance.get("author_orcids") or [],
                "Source": ["constant: PREreview"],
                "Venue": venue_sources,
                "Year": year_sources,
                "PeerReview": [
                    "PREreview review and discussion records preserved by Zenodo",
                    "explicit related_identifiers connect each discussion to its PREreview record",
                ],
                "Field": sorted({item["source"] for item in field_candidates}),
            },
            "versions": len(buckets),
            "duplicate_review_records_removed": all_dedup,
            "duplicate_discussion_records_removed": all_discussion_dedup,
            "rounds": audit_rounds,
        }
        return paper, audit

    def collection_checkpoint_path(self) -> Path:
        return self.state_dir / "collection_checkpoint.json"

    def save_collection_checkpoint(
        self,
        *,
        limit: int,
        max_pages: int,
        order_hash: str,
        next_family_index: int,
        papers: list[dict[str, Any]],
        audit: list[dict[str, Any]],
        rejection_counts: Counter[str],
        rejection_examples: list[dict[str, Any]],
        complete: bool,
    ) -> None:
        atomic_write_json(self.collection_checkpoint_path(), {
            "state_version": STATE_VERSION,
            "seed": self.seed,
            "limit": limit,
            "max_pages": max_pages,
            "field_policy": self.field_policy,
            "sampling_policy": self.sampling_policy,
            "use_datacite": self.use_datacite,
            "use_crossref": self.use_crossref,
            "use_openalex": self.use_openalex,
            "order_hash": order_hash,
            "next_family_index": next_family_index,
            "papers": papers,
            "audit": audit,
            "rejection_counts": dict(rejection_counts),
            "rejection_examples": rejection_examples,
            "request_counts": dict(self.request_counts),
            "complete": complete,
            "updated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        })

    def load_collection_checkpoint(
        self,
        *,
        limit: int,
        max_pages: int,
        order_hash: str,
    ) -> dict[str, Any] | None:
        if not self.resume or self.refresh_zenodo or self.refresh_metadata:
            return None
        checkpoint = load_json(self.collection_checkpoint_path())
        if not isinstance(checkpoint, dict):
            return None
        expected = {
            "state_version": STATE_VERSION,
            "seed": self.seed,
            "limit": limit,
            "max_pages": max_pages,
            "field_policy": self.field_policy,
            "sampling_policy": self.sampling_policy,
            "use_datacite": self.use_datacite,
            "use_crossref": self.use_crossref,
            "use_openalex": self.use_openalex,
            "order_hash": order_hash,
        }
        if any(checkpoint.get(key) != value for key, value in expected.items()):
            logging.info("Existing collection checkpoint does not match current arguments; starting a new build phase.")
            return None
        return checkpoint

    def collect(self, limit: int, max_pages: int) -> tuple[list[dict[str, Any]], dict[str, Any], list[dict[str, Any]]]:
        families, scan_stats, responses_by_review, discussions_by_review, interaction_family_keys = self.scan(max_pages)
        family_values = list(families.values())
        response_linked = sorted(
            (family for family in family_values if family.key in interaction_family_keys),
            key=lambda family: self.family_hash(family.key),
        )
        multi_version = sorted(
            (family for family in family_values if len(family.targets) > 1 and family.key not in interaction_family_keys),
            key=lambda family: self.family_hash(family.key),
        )
        single_version = sorted(
            (family for family in family_values if len(family.targets) == 1 and family.key not in interaction_family_keys),
            key=lambda family: self.family_hash(family.key),
        )
        if self.sampling_policy == "coverage":
            ordered = response_linked + multi_version + single_version
        else:
            ordered = sorted(family_values, key=lambda family: self.family_hash(family.key))
        order_hash = hashlib.sha256("\n".join(family.key for family in ordered).encode("utf-8")).hexdigest()

        checkpoint = self.load_collection_checkpoint(limit=limit, max_pages=max_pages, order_hash=order_hash)
        if checkpoint:
            papers = checkpoint.get("papers") or []
            audit = checkpoint.get("audit") or []
            rejection_counts = Counter(checkpoint.get("rejection_counts") or {})
            rejection_examples = checkpoint.get("rejection_examples") or []
            next_family_index = int(checkpoint.get("next_family_index") or 0)
            self.request_counts.update(checkpoint.get("request_counts") or {})
            logging.info(
                "Resuming collection at family %d/%d with %d accepted papers.",
                next_family_index,
                len(ordered),
                len(papers),
            )
            if checkpoint.get("complete") and len(papers) >= limit:
                papers = papers[:limit]
                audit = audit[:limit]
                next_family_index = min(next_family_index, len(ordered))
        else:
            papers = []
            audit = []
            rejection_counts = Counter()
            rejection_examples = []
            next_family_index = 0

        if len(papers) < limit:
            try:
                for family_index in range(next_family_index, len(ordered)):
                    family = ordered[family_index]
                    paper, detail = self.build_family(family, responses_by_review, discussions_by_review)
                    if paper is None:
                        rejection_counts[detail.get("reason", "unknown")] += 1
                        if len(rejection_examples) < 30:
                            rejection_examples.append(detail)
                    else:
                        papers.append(paper)
                        detail["sample_index"] = len(papers)
                        audit.append(detail)
                    processed = family_index + 1
                    if processed % self.checkpoint_every == 0 or len(papers) >= limit:
                        self.save_collection_checkpoint(
                            limit=limit,
                            max_pages=max_pages,
                            order_hash=order_hash,
                            next_family_index=processed,
                            papers=papers,
                            audit=audit,
                            rejection_counts=rejection_counts,
                            rejection_examples=rejection_examples,
                            complete=len(papers) >= limit,
                        )
                    if len(papers) >= limit:
                        next_family_index = processed
                        break
                else:
                    next_family_index = len(ordered)
            except BaseException:
                self.save_collection_checkpoint(
                    limit=limit,
                    max_pages=max_pages,
                    order_hash=order_hash,
                    next_family_index=locals().get("processed", next_family_index),
                    papers=papers,
                    audit=audit,
                    rejection_counts=rejection_counts,
                    rejection_examples=rejection_examples,
                    complete=False,
                )
                raise

        complete = len(papers) >= limit
        self.save_collection_checkpoint(
            limit=limit,
            max_pages=max_pages,
            order_hash=order_hash,
            next_family_index=next_family_index,
            papers=papers,
            audit=audit,
            rejection_counts=rejection_counts,
            rejection_examples=rejection_examples,
            complete=complete,
        )

        papers = papers[:limit]
        audit = audit[:limit]
        selected_reviews = sum(
            len(round_data["Comments"])
            for paper in papers
            for round_data in paper["PeerReview"]
        )
        selected_review_types: Counter[str] = Counter()
        for row in audit:
            for round_data in row["rounds"]:
                for review in round_data["reviews"]:
                    selected_review_types[review["review_type"]] += 1
        stats = {
            "platform": "PREreview",
            "source": "Zenodo community prereview-reviews",
            "association_policy": (
                "Reviews require explicit Zenodo related_identifiers with relation=reviews; "
                "responses and discussions require explicit links to a known review DOI; "
                "no DOI extraction from prose, titles, or arbitrary links."
            ),
            "sample_policy": (
                "Deterministic SHA-256 ordering across all strict families after a complete community scan."
                if self.sampling_policy == "hash" else
                "Coverage-prioritized ordering: discussion/response families, multi-version families, then deterministic SHA-256 fill."
            ),
            "seed": self.seed,
            "requested": limit,
            "written": len(papers),
            "resume": {
                "enabled": self.resume,
                "state_dir": str(self.state_dir),
                "checkpoint_every_families": self.checkpoint_every,
                "checkpoint_complete": complete,
                "next_family_index": next_family_index,
                "ordered_family_count": len(ordered),
                "order_hash": order_hash,
                "refresh_zenodo": self.refresh_zenodo,
                "refresh_metadata": self.refresh_metadata,
            },
            "field_policy": self.field_policy,
            "metadata_policy": {
                "DataCite": self.use_datacite,
                "Crossref": self.use_crossref,
                "OpenAlex": self.use_openalex,
                "OpenAlex_role": "optional final fallback only",
            },
            "scan": scan_stats,
            "metadata_rejections": dict(rejection_counts),
            "metadata_rejection_examples": rejection_examples,
            "selected": {
                "papers": len(papers),
                "review_comments": selected_reviews,
                "multi_version_papers": sum(len(paper["PeerReview"]) > 1 for paper in papers),
                "rounds": sum(len(paper["PeerReview"]) for paper in papers),
                "years": dict(sorted(Counter(paper["Year"] for paper in papers).items())),
                "venues": dict(Counter(paper["Venue"] for paper in papers).most_common()),
                "review_types": dict(selected_review_types),
                "empty_doi": sum(not paper["DOI"] for paper in papers),
                "nonempty_field": sum(bool(paper["Field"]) for paper in papers),
                "responses_found": sum(len(round_data["Response"]) for paper in papers for round_data in paper["PeerReview"]),
                "discussion_comments_found": sum(
                    len(round_data.get("Discussion") or [])
                    for paper in papers
                    for round_data in paper["PeerReview"]
                ),
            },
            "request_counts": dict(self.request_counts),
            "reviewer_id_semantics": "Stable PREreview review-record DOI, analogous to the F1000 report ID used in the provided schema.",
        }
        return papers, stats, audit


def serializable_round(round_data: dict[str, Any], extended: bool) -> dict[str, Any]:
    value = {
        "Round": round_data["Round"],
        "Comments": round_data["Comments"],
        "Response": round_data["Response"],
        "Discussion": round_data.get("Discussion", []),
        "Timeline": round_data.get("Timeline", []),
    }
    if extended:
        value["Target_DOI"] = round_data.get("Target_DOI", "")
    return value


def save_csv(records: list[dict[str, Any]], output: Path, *, extended: bool = False) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(output.name + ".tmp")
    with temporary.open("w", encoding="utf-8-sig", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=COLUMNS)
        writer.writeheader()
        for record in records:
            writer.writerow({
                "DOI": record["DOI"],
                "PaperTitle": record["PaperTitle"],
                "Authors": json.dumps(record["Authors"], ensure_ascii=False),
                "Source": record["Source"],
                "Venue": record["Venue"],
                "Year": record["Year"],
                "PeerReview": json.dumps(
                    [serializable_round(item, extended) for item in record["PeerReview"]],
                    ensure_ascii=False,
                ),
                "Field": record["Field"],
            })
    temporary.replace(output)


def canonical_family_from_output_doi(value: str) -> str:
    normalized = normalize_doi(value)
    if not normalized:
        return ""
    return family_and_version(normalized, "doi")[0]


def contains_real_html(value: str) -> bool:
    # Do not flag mathematical comparisons such as pLDDT < 50 or quoted text
    # spanning angle brackets. Only recognized HTML elements count.
    soup = BeautifulSoup(value or "", "html.parser")
    return any((tag.name or "").lower() in KNOWN_HTML_TAGS for tag in soup.find_all())


def valid_orcid_list(value: Any) -> bool:
    return isinstance(value, list) and all(
        isinstance(item, str) and normalize_orcid(item) == item for item in value
    )


def valid_iso_date(value: Any) -> bool:
    return bool(re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(value or "")))


def validate_csv(path: Path, expected: int) -> list[str]:
    with path.open(encoding="utf-8-sig", newline="") as file:
        reader = csv.DictReader(file)
        rows = list(reader)
        columns = reader.fieldnames
    issues: list[str] = []
    if columns != COLUMNS:
        issues.append(f"columns={columns}, expected={COLUMNS}")
    if len(rows) != expected:
        issues.append(f"rows={len(rows)}, expected={expected}")
    family_keys: set[str] = set()
    review_ids: set[str] = set()
    for row_number, row in enumerate(rows, start=2):
        for required in ("PaperTitle", "Authors", "Source", "Venue", "Year", "PeerReview"):
            if not str(row.get(required) or "").strip():
                issues.append(f"row {row_number}: missing {required}")
        if contains_real_html(row.get("PaperTitle") or ""):
            issues.append(f"row {row_number}: HTML remains in title")
        if "\n" in (row.get("PaperTitle") or "") or "\r" in (row.get("PaperTitle") or ""):
            issues.append(f"row {row_number}: newline remains in title")
        doi_value = row.get("DOI") or ""
        if doi_value:
            normalized = normalize_doi(doi_value)
            if not normalized:
                issues.append(f"row {row_number}: invalid paper DOI {doi_value!r}")
            family_key = canonical_family_from_output_doi(doi_value)
            if family_key in family_keys:
                issues.append(f"row {row_number}: duplicate version-family {family_key}")
            family_keys.add(family_key)
        if row.get("Source") != "PREreview":
            issues.append(f"row {row_number}: Source must be PREreview")
        if not re.fullmatch(r"\d{4}", row.get("Year") or ""):
            issues.append(f"row {row_number}: invalid year")
        elif not (1900 <= int(row["Year"]) <= CURRENT_YEAR + 1):
            issues.append(f"row {row_number}: out-of-range year {row['Year']}")
        venue = row.get("Venue") or ""
        if any(fragment in f" {venue.lower()}" for fragment in FORBIDDEN_VENUE_FRAGMENTS):
            issues.append(f"row {row_number}: publisher-like Venue {venue!r}")
        for prefix, expected_venue in OSF_PREFIX_VENUES.items():
            if doi_value.lower().startswith(prefix) and venue != expected_venue:
                issues.append(f"row {row_number}: Venue {venue!r} conflicts with DOI prefix {prefix}; expected {expected_venue!r}")
        try:
            authors = json.loads(row.get("Authors") or "")
            peer_reviews = json.loads(row.get("PeerReview") or "")
        except Exception as exc:
            issues.append(f"row {row_number}: JSON error {exc}")
            continue
        if not isinstance(authors, list) or not authors or any(not isinstance(name, str) or not name.strip() for name in authors):
            issues.append(f"row {row_number}: invalid Authors")
        if not isinstance(peer_reviews, list) or not peer_reviews:
            issues.append(f"row {row_number}: invalid PeerReview")
            continue
        expected_rounds = list(range(1, len(peer_reviews) + 1))
        rounds = [item.get("Round") for item in peer_reviews if isinstance(item, dict)]
        if rounds != expected_rounds:
            issues.append(f"row {row_number}: non-consecutive rounds {rounds}")
        for round_data in peer_reviews:
            if not isinstance(round_data, dict):
                issues.append(f"row {row_number}: round is not object")
                continue
            comments = round_data.get("Comments")
            responses = round_data.get("Response")
            discussions = round_data.get("Discussion")
            timeline = round_data.get("Timeline")
            if not isinstance(comments, list) or not comments:
                issues.append(f"row {row_number}: empty Comments")
                continue
            if not isinstance(responses, list):
                issues.append(f"row {row_number}: Response is not list")
                responses = []
            if not isinstance(discussions, list):
                issues.append(f"row {row_number}: Discussion is not list")
                discussions = []
            if not isinstance(timeline, list) or not timeline:
                issues.append(f"row {row_number}: Timeline is not a non-empty list")
                timeline = []
            round_review_ids = {
                str(comment.get("Reviewer_ID") or "") for comment in comments if isinstance(comment, dict)
            }
            seen_response_ids: set[str] = set()
            for response in responses:
                if not isinstance(response, dict):
                    issues.append(f"row {row_number}: invalid response")
                    continue
                response_id = str(response.get("Response_ID") or "")
                response_target = str(response.get("To_Reviewer_ID") or "")
                if not response_id or not response_target or not str(response.get("Response") or "").strip():
                    issues.append(f"row {row_number}: invalid response")
                elif response_target not in round_review_ids:
                    issues.append(f"row {row_number}: response points to a review not present in the same round")
                elif contains_real_html(str(response.get("Response") or "")):
                    issues.append(f"row {row_number}: HTML remains in response")
                if response_id in seen_response_ids:
                    issues.append(f"row {row_number}: duplicate response ID {response_id}")
                seen_response_ids.add(response_id)
                if not valid_iso_date(response.get("Response_Date")):
                    issues.append(f"row {row_number}: invalid response date")
                if not valid_orcid_list(response.get("Responder_ORCID")):
                    issues.append(f"row {row_number}: invalid responder ORCID list")
            seen_discussion_ids: set[str] = set()
            previous_discussion_order: tuple[str, str] | None = None
            for discussion in discussions:
                if not isinstance(discussion, dict):
                    issues.append(f"row {row_number}: discussion entry is not object")
                    continue
                discussion_id = str(discussion.get("Comment_ID") or "")
                target_review_id = str(discussion.get("In_Reply_To_Reviewer_ID") or "")
                body = str(discussion.get("Comment") or "")
                role = str(discussion.get("Commenter_Role") or "")
                comment_type = str(discussion.get("Comment_Type") or "")
                date = str(discussion.get("Comment_Date") or "")
                if not discussion_id or not target_review_id or not body:
                    issues.append(f"row {row_number}: invalid discussion entry")
                if not valid_iso_date(date):
                    issues.append(f"row {row_number}: invalid discussion date")
                if not valid_orcid_list(discussion.get("Commenter_ORCID")):
                    issues.append(f"row {row_number}: invalid commenter ORCID list")
                if target_review_id not in round_review_ids:
                    issues.append(f"row {row_number}: discussion points to a review not present in the same round")
                if role not in {"author", "reviewer", "commenter"}:
                    issues.append(f"row {row_number}: invalid discussion participant role {role!r}")
                expected_type = {
                    "author": "author_response",
                    "reviewer": "reviewer_followup",
                    "commenter": "community_comment",
                }.get(role)
                if comment_type != expected_type:
                    issues.append(f"row {row_number}: discussion type {comment_type!r} conflicts with role {role!r}")
                if discussion_id in seen_discussion_ids:
                    issues.append(f"row {row_number}: duplicate discussion ID {discussion_id}")
                seen_discussion_ids.add(discussion_id)
                if contains_real_html(body):
                    issues.append(f"row {row_number}: HTML remains in discussion")
                order_key = (date, discussion_id)
                if previous_discussion_order is not None and order_key < previous_discussion_order:
                    issues.append(f"row {row_number}: discussion is not chronologically ordered")
                previous_discussion_order = order_key
            expected_event_ids = round_review_ids | {
                str(response.get("Response_ID") or "") for response in responses if isinstance(response, dict)
            } | seen_discussion_ids
            timeline_event_ids: list[str] = []
            previous_timeline_order: tuple[str, int, str] | None = None
            for event in timeline:
                if not isinstance(event, dict):
                    issues.append(f"row {row_number}: timeline event is not object")
                    continue
                event_type = str(event.get("Event_Type") or "")
                event_id = str(event.get("Event_ID") or "")
                actor_role = str(event.get("Actor_Role") or "")
                date = str(event.get("Date") or "")
                reply_to = str(event.get("In_Reply_To") or "")
                if event_type not in {"review", "legacy_author_response", "author_response", "reviewer_followup", "community_comment"}:
                    issues.append(f"row {row_number}: invalid timeline event type {event_type!r}")
                expected_actor_role = {
                    "review": "reviewer",
                    "legacy_author_response": "author",
                    "author_response": "author",
                    "reviewer_followup": "reviewer",
                    "community_comment": "commenter",
                }.get(event_type)
                if actor_role != expected_actor_role or not event_id or not valid_iso_date(date):
                    issues.append(f"row {row_number}: invalid timeline event")
                if event_type != "review" and reply_to not in round_review_ids:
                    issues.append(f"row {row_number}: timeline reply target is not in the same round")
                timeline_event_ids.append(event_id)
                order_key = (date, 0 if event_type == "review" else 1, event_id)
                if previous_timeline_order is not None and order_key < previous_timeline_order:
                    issues.append(f"row {row_number}: timeline is not chronologically ordered")
                previous_timeline_order = order_key
            if len(timeline_event_ids) != len(set(timeline_event_ids)):
                issues.append(f"row {row_number}: duplicate timeline event ID")
            if set(timeline_event_ids) != expected_event_ids:
                issues.append(f"row {row_number}: timeline does not cover all review-thread events")
            comment_hashes: set[str] = set()
            for comment in comments:
                review_id = str(comment.get("Reviewer_ID") or "") if isinstance(comment, dict) else ""
                body = str(comment.get("Comment") or "") if isinstance(comment, dict) else ""
                if not review_id or not body:
                    issues.append(f"row {row_number}: invalid comment")
                if not valid_iso_date(comment.get("Review_Date") if isinstance(comment, dict) else ""):
                    issues.append(f"row {row_number}: invalid review date")
                if not valid_orcid_list(comment.get("Reviewer_ORCID") if isinstance(comment, dict) else None):
                    issues.append(f"row {row_number}: invalid reviewer ORCID list")
                if review_id in review_ids:
                    issues.append(f"row {row_number}: duplicate review ID {review_id}")
                review_ids.add(review_id)
                if any(phrase in body.lower() for phrase in PRESERVATION_PHRASES):
                    issues.append(f"row {row_number}: preservation boilerplate remains")
                if contains_real_html(body):
                    issues.append(f"row {row_number}: HTML remains in comment")
                body_hash = normalized_comment_hash(body)
                if body_hash in comment_hashes:
                    issues.append(f"row {row_number}: exact duplicate review body in round {round_data.get('Round')}")
                comment_hashes.add(body_hash)
    return issues


def validate_audit(
    audit: list[dict[str, Any]],
    expected: int,
    csv_path: Path | None = None,
) -> list[str]:
    issues: list[str] = []
    if len(audit) != expected:
        issues.append(f"audit rows={len(audit)}, expected={expected}")
    csv_rows: list[dict[str, str]] = []
    if csv_path is not None:
        with csv_path.open(encoding="utf-8-sig", newline="") as file:
            csv_rows = list(csv.DictReader(file))
        if len(csv_rows) != len(audit):
            issues.append(f"audit/CSV row mismatch: audit={len(audit)}, CSV={len(csv_rows)}")
    for index, item in enumerate(audit, start=1):
        if index <= len(csv_rows):
            csv_doi = normalize_doi(csv_rows[index - 1].get("DOI") or "")
            audit_doi = normalize_doi(str(item.get("output_doi") or ""))
            if audit_doi != csv_doi:
                issues.append(
                    f"audit item {index}: output DOI {audit_doi!r} does not match CSV DOI {csv_doi!r}"
                )
            if csv_doi:
                csv_family = canonical_family_from_output_doi(csv_doi)
                audit_family = str(item.get("family_key") or "")
                if audit_family != csv_family:
                    issues.append(
                        f"audit item {index}: family {audit_family!r} does not match CSV family {csv_family!r}"
                    )
        provenance = item.get("field_level_provenance") or {}
        for field_name in ("DOI", "PaperTitle", "Authors", "Source", "Venue", "Year", "PeerReview", "Field"):
            if field_name not in provenance:
                issues.append(f"audit item {index}: missing provenance for {field_name}")
        for round_data in item.get("rounds") or []:
            if not round_data.get("target_identifier"):
                issues.append(f"audit item {index}: missing target identifier for round {round_data.get('round')}")
            for discussion in round_data.get("discussion") or []:
                if not discussion.get("target_relation_verified"):
                    issues.append(f"audit item {index}: discussion target relation is not independently verified")
                if discussion.get("participant_role") not in {"author", "reviewer", "commenter"}:
                    issues.append(f"audit item {index}: invalid discussion participant role")
                if not discussion.get("participant_role_evidence"):
                    issues.append(f"audit item {index}: missing discussion role evidence")
    return issues


def main() -> None:
    parser = argparse.ArgumentParser(description="Strict, resumable PREreview/Zenodo collector")
    parser.add_argument("--limit", type=int, default=200)
    parser.add_argument("--max-pages", type=int, default=100)
    parser.add_argument("--output", default="data/prereview/prereview_final.csv")
    parser.add_argument("--extended-output", default="", help="Optional CSV whose PeerReview rounds include Target_DOI")
    parser.add_argument("--stats", default="data/prereview/prereview_collection_stats.json")
    parser.add_argument("--audit", default="data/prereview/prereview_audit.json")
    parser.add_argument("--dedup-log", default="data/prereview/prereview_review_dedup_log.json")
    parser.add_argument("--validation-report", default="data/prereview/prereview_validation.json")
    parser.add_argument("--seed", default="PREreview-production-v1")
    parser.add_argument("--state-dir", default="data/prereview/state")
    parser.add_argument("--resume", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--checkpoint-every", type=int, default=25)
    parser.add_argument("--refresh-zenodo", action="store_true", help="Refresh the Zenodo community snapshot but reuse metadata caches")
    parser.add_argument("--allow-partial-scan", action="store_true", help="Allow an intentionally incomplete Zenodo snapshot for small tests")
    parser.add_argument("--refresh-metadata", action="store_true", help="Refresh DOI/arXiv metadata and rebuild rows")
    parser.add_argument("--field-policy", choices=("empty", "native", "metadata", "broad"), default="metadata")
    parser.add_argument("--sampling-policy", choices=("hash", "coverage"), default="hash")
    parser.add_argument("--use-datacite", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--use-crossref", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--use-openalex", action=argparse.BooleanOptionalAction, default=False)
    parser.add_argument("--crossref-mailto", default=os.getenv("CROSSREF_MAILTO", ""))
    parser.add_argument("--openalex-api-key", default=os.getenv("OPENALEX_API_KEY", ""))
    args = parser.parse_args()
    if args.limit <= 0:
        parser.error("--limit must be positive")

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    collector = Collector(
        seed=args.seed,
        state_dir=Path(args.state_dir),
        resume=args.resume,
        checkpoint_every=args.checkpoint_every,
        crossref_mailto=args.crossref_mailto,
        openalex_api_key=args.openalex_api_key,
        use_datacite=args.use_datacite,
        use_crossref=args.use_crossref,
        use_openalex=args.use_openalex,
        field_policy=args.field_policy,
        sampling_policy=args.sampling_policy,
        refresh_zenodo=args.refresh_zenodo,
        refresh_metadata=args.refresh_metadata,
        allow_partial_scan=args.allow_partial_scan,
    )
    records, report, audit = collector.collect(args.limit, args.max_pages)
    output = Path(args.output)
    save_csv(records, output, extended=False)
    if args.extended_output:
        save_csv(records, Path(args.extended_output), extended=True)
    csv_issues = validate_csv(output, args.limit)
    audit_issues = validate_audit(audit, args.limit, output)
    issues = csv_issues + audit_issues
    report["validation_issues"] = issues
    review_dedup_log = [
        {"record_type": "review", **detail}
        for item in audit
        for detail in item.get("duplicate_review_records_removed") or []
    ]
    discussion_dedup_log = [
        {"record_type": "discussion", **detail}
        for item in audit
        for detail in item.get("duplicate_discussion_records_removed") or []
    ]
    dedup_log = review_dedup_log + discussion_dedup_log
    for path in (Path(args.stats), Path(args.audit), Path(args.dedup_log), Path(args.validation_report)):
        path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(Path(args.stats), report)
    atomic_write_json(Path(args.audit), audit)
    atomic_write_json(Path(args.dedup_log), dedup_log)
    validation = {
        "status": "passed" if not issues and len(records) == args.limit else "failed",
        "rows": len(records),
        "validation_issues": issues,
        "duplicate_review_records_removed": len(review_dedup_log),
        "duplicate_discussion_records_removed": len(discussion_dedup_log),
        "strict_output": str(output),
        "extended_output": args.extended_output,
    }
    atomic_write_json(Path(args.validation_report), validation)
    print(json.dumps({**report, "validation": validation}, ensure_ascii=False, indent=2))
    sys.exit(2 if issues or len(records) != args.limit else 0)


if __name__ == "__main__":
    main()
