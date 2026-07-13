from __future__ import annotations

import csv
import importlib.util
import json
import sys
import tempfile
import unittest
from collections import OrderedDict
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
MODULE_PATH = BASE_DIR / 'prereview_crawler_production.py'
spec = importlib.util.spec_from_file_location('prereview_crawler_production', MODULE_PATH)
assert spec and spec.loader
crawler = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = crawler
spec.loader.exec_module(crawler)


class ProductionCrawlerTests(unittest.TestCase):
    def make_target(self, doi: str) -> crawler.Target:
        family, version = crawler.family_and_version(doi, 'doi')
        return crawler.Target('doi', doi, doi, family, version, 'doi', doi)

    def make_family(self, doi: str, comments: list[tuple[str, str]], subjects=None):
        target = self.make_target(doi)
        reviews = [
            crawler.Review(
                review_id=review_id,
                record_id=review_id.rsplit('.', 1)[-1],
                target=target,
                comment=body,
                review_date=f'2024-01-{index:02d}',
                review_type='PREreview',
                title_hint='A test paper',
                record_url='https://zenodo.org/records/test',
                creators=['Reviewer'],
                subjects=subjects or [],
            )
            for index, (review_id, body) in enumerate(comments, 1)
        ]
        family = crawler.Family(target.family_key)
        family.targets[target.value] = crawler.TargetBucket(target, reviews)
        return family

    def fake_metadata(self, target, fields=None, source='DataCite'):
        return {
            'title': 'A test paper',
            'authors': ['Alice Example'],
            'year': '2024',
            'venue': crawler.normalize_venue([], target) or 'Preprints.org',
            'sources': [source],
            'field_candidates': [
                {'value': value, 'source': source} for value in (fields or [])
            ],
            'provenance': {
                'title': [source], 'authors': [source], 'year': [source],
                'venue': [source], 'field': [source] if fields else [],
            },
        }

    def test_repaired_sample_passes_strict_validation(self):
        sample = BASE_DIR / 'prereview_final_200_repaired.csv'
        if not sample.exists():
            self.skipTest('optional repaired sample is not present')
        issues = crawler.validate_csv(sample, 200)
        self.assertEqual(issues, [])

    def test_specific_osf_venues_precede_generic_arxiv(self):
        expected = {
            '10.31234/osf.io/x': 'PsyArXiv',
            '10.31235/osf.io/x': 'SocArXiv',
            '10.35542/osf.io/x': 'EdArXiv',
            '10.31222/osf.io/x': 'MetaArXiv',
            '10.31730/osf.io/x': 'AfricArXiv',
            '10.48550/arxiv.2401.00001': 'arXiv',
        }
        for doi, venue in expected.items():
            self.assertEqual(crawler.normalize_venue([], self.make_target(doi)), venue)

    def test_exact_duplicate_reviews_are_merged_and_audited(self):
        family = self.make_family(
            '10.31234/osf.io/example_v1',
            [
                ('10.5281/zenodo.1', 'Same\nreview text'),
                ('10.5281/zenodo.2', 'Same review   text'),
            ],
        )
        collector = crawler.Collector(
            state_dir=Path(tempfile.mkdtemp()), resume=False,
            use_datacite=False, use_crossref=False, use_openalex=False,
            field_policy='broad',
        )
        collector.resolve = lambda target: self.fake_metadata(target)
        paper, audit = collector.build_family(family, {})
        self.assertIsNotNone(paper)
        self.assertEqual(len(paper['PeerReview'][0]['Comments']), 1)
        self.assertEqual(len(audit['duplicate_review_records_removed']), 1)

    def test_strict_and_extended_peer_review_shapes(self):
        family = self.make_family(
            '10.31234/osf.io/example_v1',
            [('10.5281/zenodo.1', 'Review text')],
        )
        collector = crawler.Collector(
            state_dir=Path(tempfile.mkdtemp()), resume=False,
            use_datacite=False, use_crossref=False, field_policy='broad',
        )
        collector.resolve = lambda target: self.fake_metadata(target)
        paper, _ = collector.build_family(family, {})
        with tempfile.TemporaryDirectory() as directory:
            strict = Path(directory) / 'strict.csv'
            extended = Path(directory) / 'extended.csv'
            crawler.save_csv([paper], strict, extended=False)
            crawler.save_csv([paper], extended, extended=True)
            with strict.open(encoding='utf-8-sig', newline='') as file:
                strict_round = json.loads(next(csv.DictReader(file))['PeerReview'])[0]
            with extended.open(encoding='utf-8-sig', newline='') as file:
                extended_round = json.loads(next(csv.DictReader(file))['PeerReview'])[0]
        self.assertEqual(set(strict_round), {'Round', 'Comments', 'Response'})
        self.assertIn('Target_DOI', extended_round)

    def test_field_policies_are_explicit(self):
        family = self.make_family(
            '10.20944/preprints2024.1.v1',
            [('10.5281/zenodo.1', 'Review text')],
            subjects=['Native Subject'],
        )
        for policy, expected in [
            ('empty', ''),
            ('native', 'Native Subject'),
            ('metadata', 'Native Subject; Registry Subject'),
        ]:
            collector = crawler.Collector(
                state_dir=Path(tempfile.mkdtemp()), resume=False,
                use_datacite=False, use_crossref=False, field_policy=policy,
            )
            collector.resolve = lambda target: self.fake_metadata(target, ['Registry Subject'])
            paper, audit = collector.build_family(family, {})
            self.assertEqual(paper['Field'], expected)
            self.assertEqual(audit['field_policy'], policy)

    def test_datacite_parser(self):
        collector = crawler.Collector(
            state_dir=Path(tempfile.mkdtemp()), resume=False,
            use_crossref=False, use_openalex=False,
        )
        collector.cached_json_request = lambda *args, **kwargs: {
            'data': {'attributes': {
                'titles': [{'title': 'A <i>DataCite</i> title'}],
                'creators': [{'givenName': 'Ada', 'familyName': 'Lovelace'}],
                'publicationYear': 2024,
                'publisher': 'PsyArXiv',
                'url': 'https://osf.io/example',
                'subjects': [{'subject': 'Psychology'}],
            }}
        }
        metadata = collector.datacite_metadata('10.31234/osf.io/example')
        self.assertEqual(metadata['title'], 'A DataCite title')
        self.assertEqual(metadata['authors'], ['Ada Lovelace'])
        self.assertEqual(metadata['year'], '2024')
        self.assertEqual(metadata['fields'], ['Psychology'])

    def test_checkpoint_resume_continues_after_last_completed_family(self):
        state_dir = Path(tempfile.mkdtemp())
        families = OrderedDict()
        for index in range(3):
            family = self.make_family(
                f'10.20944/preprints2024.{index}.v1',
                [(f'10.5281/zenodo.{index + 1}', f'Review {index}')],
            )
            families[family.key] = family

        class FakeCollector(crawler.Collector):
            def __init__(self, fail_on_call=None, **kwargs):
                super().__init__(**kwargs)
                self.fail_on_call = fail_on_call
                self.calls = 0

            def scan(self, max_pages):
                return families, {'records_seen': 3}, {}, set()

            def build_family(self, family, responses):
                self.calls += 1
                if self.fail_on_call == self.calls:
                    raise RuntimeError('simulated interruption')
                doi = next(iter(family.targets.values())).target.doi
                paper = {
                    'DOI': doi, 'PaperTitle': f'Paper {doi}', 'Authors': ['A'],
                    'Source': 'PREreview', 'Venue': 'Preprints.org', 'Year': '2024',
                    'PeerReview': [{'Round': 1, 'Target_DOI': doi, 'Comments': [
                        {'Reviewer_ID': f'10.5281/zenodo.{self.calls}', 'Comment': 'Review'}
                    ], 'Response': []}], 'Field': '',
                }
                audit = {
                    'family_key': family.key, 'output_doi': doi, 'rounds': [{
                        'round': 1, 'target_identifier': doi, 'reviews': [], 'responses': []
                    }],
                    'field_level_provenance': {name: [] for name in crawler.COLUMNS},
                    'duplicate_review_records_removed': [],
                }
                return paper, audit

        first = FakeCollector(
            fail_on_call=2, state_dir=state_dir, resume=True, checkpoint_every=1,
            use_datacite=False, use_crossref=False, use_openalex=False,
            field_policy='empty', sampling_policy='hash', seed='resume-test',
        )
        with self.assertRaises(RuntimeError):
            first.collect(3, 1)
        checkpoint = json.loads((state_dir / 'collection_checkpoint.json').read_text())
        self.assertEqual(len(checkpoint['papers']), 1)
        second = FakeCollector(
            state_dir=state_dir, resume=True, checkpoint_every=1,
            use_datacite=False, use_crossref=False, use_openalex=False,
            field_policy='empty', sampling_policy='hash', seed='resume-test',
        )
        papers, _, _ = second.collect(3, 1)
        self.assertEqual(len(papers), 3)
        self.assertEqual(second.calls, 2)


if __name__ == '__main__':
    unittest.main(verbosity=2)
