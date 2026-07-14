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
            'author_orcids': ['0000-0001-2345-6789'],
            'year': '2024',
            'venue': crawler.normalize_venue([], target) or 'Preprints.org',
            'sources': [source],
            'field_candidates': [
                {'value': value, 'source': source} for value in (fields or [])
            ],
            'provenance': {
                'title': [source], 'authors': [source], 'author_orcids': [source], 'year': [source],
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
            '10.1590/scielopreprints.8406': 'SciELO Preprints',
            '10.36227/techrxiv.173014403.30709587/v1': 'TechRxiv',
            '10.32942/x2cg64': 'EcoEvoRxiv',
        }
        for doi, venue in expected.items():
            self.assertEqual(crawler.normalize_venue([], self.make_target(doi)), venue)

    def test_explicit_zenodo_preprint_target_is_not_confused_with_review_doi(self):
        record = {
            'metadata': {
                'related_identifiers': [{
                    'relation': 'reviews',
                    'identifier': '10.5281/zenodo.16813375',
                    'resource_type': 'Preprint',
                }],
            },
        }
        target, reason = crawler.explicit_target(record)
        self.assertEqual(reason, 'ok')
        self.assertEqual(target.doi, '10.5281/zenodo.16813375')
        self.assertEqual(crawler.normalize_venue([], target), 'Zenodo')

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
        self.assertEqual(set(strict_round), {'Round', 'Comments', 'Response', 'Discussion', 'Timeline'})
        self.assertIn('Target_DOI', extended_round)

    def test_discussion_comment_is_preserved_with_role_and_timeline(self):
        family = self.make_family(
            '10.31234/osf.io/example_v1',
            [('10.5281/zenodo.1', 'Review text')],
        )
        discussion = crawler.DiscussionComment(
            comment_id='10.5281/zenodo.2',
            record_id='2',
            target_review_id='10.5281/zenodo.1',
            family_key=family.key,
            content='Thank you for the review. We have addressed the main concern.',
            comment_date='2024-02-01',
            record_url='https://zenodo.org/records/2',
            creators=['Alice Example'],
            body_source='description',
            target_relation_verified=True,
        )
        collector = crawler.Collector(
            state_dir=Path(tempfile.mkdtemp()), resume=False,
            use_datacite=False, use_crossref=False, field_policy='broad',
        )
        collector.resolve = lambda target: self.fake_metadata(target)
        paper, audit = collector.build_family(
            family,
            {},
            {'10.5281/zenodo.1': [discussion]},
        )
        round_data = paper['PeerReview'][0]
        self.assertEqual(round_data['Response'], [])
        self.assertEqual(len(round_data['Discussion']), 1)
        self.assertEqual(round_data['Discussion'][0]['Commenter_Role'], 'author')
        self.assertEqual(round_data['Discussion'][0]['Comment_Type'], 'author_response')
        self.assertEqual(round_data['Discussion'][0]['In_Reply_To_Reviewer_ID'], '10.5281/zenodo.1')
        self.assertEqual([event['Event_Type'] for event in round_data['Timeline']], ['review', 'author_response'])
        self.assertTrue(audit['rounds'][0]['discussion'][0]['target_relation_verified'])

    def test_explicit_response_text_repairs_missing_author_name_match(self):
        discussion = crawler.DiscussionComment(
            comment_id='10.5281/zenodo.2', record_id='2', target_review_id='10.5281/zenodo.1',
            family_key='doi:10.1234/example', content='Response to Reviewers\nWe sincerely thank the reviewers.',
            comment_date='2025-01-01', record_url='https://zenodo.org/records/2', creators=['Unmatched Name'],
        )
        role, evidence = crawler.discussion_participant_role(discussion, ['Paper Author'], ['Reviewer'])
        self.assertEqual(role, 'author')
        self.assertIn('author-response pattern', evidence)

    def test_exact_duplicate_discussions_are_merged_and_audited(self):
        family = self.make_family('10.31234/osf.io/example_v1', [('10.5281/zenodo.1', 'Review text')])
        discussions = [
            crawler.DiscussionComment(
                comment_id=f'10.5281/zenodo.{record_id}', record_id=str(record_id),
                target_review_id='10.5281/zenodo.1', family_key=family.key,
                content='Same follow-up comment.', comment_date='2025-01-01',
                record_url=f'https://zenodo.org/records/{record_id}', creators=['Same Person'],
            )
            for record_id in (2, 3)
        ]
        collector = crawler.Collector(state_dir=Path(tempfile.mkdtemp()), resume=False, field_policy='broad')
        collector.resolve = lambda target: self.fake_metadata(target)
        paper, audit = collector.build_family(family, {}, {'10.5281/zenodo.1': discussions})
        self.assertEqual(len(paper['PeerReview'][0]['Discussion']), 1)
        self.assertEqual(audit['duplicate_discussion_records_removed'][0]['removed_comment_id'], '10.5281/zenodo.3')

    def test_orcid_role_matching_precedes_names(self):
        discussion = crawler.DiscussionComment(
            comment_id='10.5281/zenodo.2', record_id='2', target_review_id='10.5281/zenodo.1',
            family_key='doi:10.1234/example', content='Thank you.', comment_date='2025-01-01',
            record_url='https://zenodo.org/records/2', creators=['Pseudonymous Display'],
            creator_orcids=['0000-0001-2345-6789'],
        )
        role, evidence = crawler.discussion_participant_role(
            discussion, ['Paper Author'], ['Reviewer'], ['0000-0001-2345-6789'], []
        )
        self.assertEqual((role, evidence), ('author', 'commenter ORCID matches resolved paper author'))

    def test_comment_html_is_canonical_body(self):
        collector = crawler.Collector(state_dir=Path(tempfile.mkdtemp()), resume=False)
        collector.cached_text_request = lambda *args, **kwargs: '<p>Canonical HTML response.</p>'
        record = {
            'id': 2,
            'metadata': {'description': '<p>Stale description.</p>'},
            'files': [{'key': 'comment.html', 'links': {'self': 'https://example.test/comment.html'}}],
        }
        self.assertEqual(collector.discussion_body(record), ('Canonical HTML response.', 'html_attachment'))

    def test_pagination_deduplicates_and_rejects_drifting_snapshot(self):
        collector = crawler.Collector(state_dir=Path(tempfile.mkdtemp()), resume=False)
        collector.cached_json_request = lambda *args, **kwargs: {
            'hits': {'hits': [{'id': 1}], 'total': 2},
        }
        with self.assertRaisesRegex(RuntimeError, 'changed during pagination'):
            list(collector.iter_zenodo_records(max_pages=2, page_size=1))
        self.assertEqual(collector.zenodo_duplicate_records, 1)
        partial = crawler.Collector(
            state_dir=Path(tempfile.mkdtemp()), resume=False, allow_partial_scan=True,
        )
        partial.cached_json_request = collector.cached_json_request
        self.assertEqual(len(list(partial.iter_zenodo_records(max_pages=2, page_size=1))), 1)
        self.assertFalse(partial.zenodo_scan_complete)

    def test_pagination_without_reported_total_requires_exhaustion(self):
        full_page = crawler.Collector(state_dir=Path(tempfile.mkdtemp()), resume=False)
        full_page.cached_json_request = lambda *args, **kwargs: {
            'hits': {'hits': [{'id': 1}]},
        }
        with self.assertRaisesRegex(RuntimeError, 'incomplete'):
            list(full_page.iter_zenodo_records(max_pages=1, page_size=1))
        self.assertFalse(full_page.zenodo_scan_complete)

        exhausted = crawler.Collector(state_dir=Path(tempfile.mkdtemp()), resume=False)
        exhausted.cached_json_request = lambda *args, **kwargs: {
            'hits': {'hits': [{'id': 1}]},
        }
        self.assertEqual(len(list(exhausted.iter_zenodo_records(max_pages=1, page_size=2))), 1)
        self.assertTrue(exhausted.zenodo_scan_complete)

    def test_explicit_target_title_change_is_audited_not_dropped(self):
        family = self.make_family('10.1101/2024.08.02.606416', [('10.5281/zenodo.1', 'Review text')])
        collector = crawler.Collector(state_dir=Path(tempfile.mkdtemp()), resume=False, field_policy='broad')
        metadata = self.fake_metadata(next(iter(family.targets.values())).target)
        metadata['title'] = 'A completely renamed version of the manuscript'
        collector.resolve = lambda target: metadata
        paper, audit = collector.build_family(family, {})
        self.assertIsNotNone(paper)
        self.assertEqual(audit['metadata_warnings'][0]['warning'], 'title_mismatch')

    def test_scan_links_new_comment_records_after_reviews_in_two_passes(self):
        target_doi = '10.31234/osf.io/example_v1'
        review_doi = '10.5281/zenodo.100'
        comment_record = {
            'id': 200,
            'doi': '10.5281/zenodo.200',
            'metadata': {
                'title': 'Comment on a PREreview of "A test paper"',
                'doi': '10.5281/zenodo.200',
                'publication_date': '2024-02-02',
                'description': '<p>This Zenodo record is a permanently preserved version of a comment on a PREreview.</p><p>Author response body.</p>',
                'creators': [{'name': 'Alice Example'}],
                'related_identifiers': [
                    {'relation': 'references', 'identifier': target_doi},
                    {'relation': 'references', 'identifier': review_doi},
                ],
            },
        }
        review_record = {
            'id': 100,
            'doi': review_doi,
            'metadata': {
                'title': 'PREreview of "A test paper"',
                'doi': review_doi,
                'publication_date': '2024-02-01',
                'description': '<p>Review body.</p>',
                'creators': [{'name': 'Reviewer One'}],
                'related_identifiers': [{
                    'relation': 'reviews',
                    'identifier': target_doi,
                    'resource_type': 'Preprint',
                }],
            },
        }
        collector = crawler.Collector(
            state_dir=Path(tempfile.mkdtemp()), resume=False,
            use_datacite=False, use_crossref=False, use_openalex=False,
        )
        collector.iter_zenodo_records = lambda max_pages: iter([comment_record, review_record])
        families, stats, responses, discussions, interaction_keys = collector.scan(1)
        self.assertEqual(len(families), 1)
        self.assertEqual(responses, {})
        self.assertEqual(stats['discussion_comments_accepted'], 1)
        self.assertEqual(discussions[review_doi][0].content, 'Author response body.')
        self.assertTrue(discussions[review_doi][0].target_relation_verified)
        self.assertEqual(interaction_keys, {next(iter(families))})

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

    def test_arxiv_doi_uses_registry_before_arxiv_api(self):
        collector = crawler.Collector(
            state_dir=Path(tempfile.mkdtemp()), resume=False,
            use_datacite=True, use_crossref=False, use_openalex=False,
        )
        calls = []
        collector.datacite_metadata = lambda doi: calls.append('datacite') or {
            **self.fake_metadata(self.make_target(doi)),
            'venue_candidates': ['arXiv'],
            'source': 'DataCite',
        }
        collector.arxiv_metadata = lambda arxiv_id: calls.append('arxiv') or {}
        metadata = collector.resolve(self.make_target('10.48550/arxiv.2603.10669'))
        self.assertEqual(calls, ['datacite'])
        self.assertEqual(metadata['venue'], 'arXiv')

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
                return families, {'records_seen': 3}, {}, {}, set()

            def build_family(self, family, responses, discussions=None):
                self.calls += 1
                if self.fail_on_call == self.calls:
                    raise RuntimeError('simulated interruption')
                doi = next(iter(family.targets.values())).target.doi
                paper = {
                    'DOI': doi, 'PaperTitle': f'Paper {doi}', 'Authors': ['A'],
                    'Source': 'PREreview', 'Venue': 'Preprints.org', 'Year': '2024',
                    'PeerReview': [{'Round': 1, 'Target_DOI': doi, 'Comments': [
                        {'Reviewer_ID': f'10.5281/zenodo.{self.calls}', 'Comment': 'Review'}
                    ], 'Response': [], 'Discussion': []}], 'Field': '',
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
