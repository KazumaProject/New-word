import copy
import datetime as dt
import io
import json
import tempfile
import unittest
import urllib.error
import urllib.parse
from pathlib import Path
from unittest.mock import patch

from test_collect_new_words import collector, FakeGitHub, candidate, evidence, issue, NOW


class ReadingTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.readings_path = Path(temp.name) / 'readings.json'
        self.seen_path = Path(temp.name) / 'seen.tsv'
        for patcher in (
            patch.object(collector, 'READINGS_PATH', self.readings_path),
            patch.object(collector, 'SEEN_PATH', self.seen_path),
            patch.object(collector.urllib.request, 'urlopen', side_effect=AssertionError('unexpected network')),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_explicit_readings_and_ruby_are_normalized(self):
        documents = [
            '新製品「FactoryLens（読み方：ファクトリーレンズ）」を発表。',
            'FactoryLens (ファクトリー・レンズ)',
            '「ファクトリーレンズ（FactoryLens）」を発表。',
            '<p>FactoryLensの読み方は「ファクトリーレンズ」。</p>',
            '<ruby>FactoryLens<rp>（</rp><rt>ファクトリーレンズ</rt><rp>）</rp></ruby>',
        ]
        for document in documents:
            with self.subTest(document=document):
                self.assertEqual(collector.extract_readings('FactoryLens', document), {'ふぁくとりーれんず'})
        self.assertEqual(collector.reading_hint('ゆめネオバンク'), 'ゆめねおばんく')
        self.assertEqual(collector.reading_hint('ﾌｧｸﾄﾘｰ･ﾚﾝｽﾞ'), 'ふぁくとりーれんず')

    def test_substrings_scripts_partial_ruby_and_numbers_are_not_guessed(self):
        for document in (
            'SuperFactoryLens（スーパーファクトリーレンズ）',
            'FactoryLens Pro（ファクトリーレンズプロ）',
            '<script>FactoryLens（ウソ）</script><style>FactoryLens（ニセ）</style>',
            '<ruby>Factory<rt>ファクトリー</rt></ruby>Lens',
            'FactoryLens（ファクトリーレンズ2026）',
        ):
            self.assertEqual(collector.extract_readings('FactoryLens', document), set())
        self.assertEqual(collector.reading_hint('灰色の魔女'), '要確認')

    def test_resolver_keeps_direct_source_and_caches_shared_articles(self):
        sources = evidence('FactoryLens')
        for source in sources:
            source['link'] = 'https://publisher.example/article'
        with patch.object(collector, 'public_reading_document', return_value='FactoryLens（ファクトリーレンズ）') as fetch:
            resolver = collector.ReadingResolver()
            result = resolver.resolve('FactoryLens', sources)
            self.assertEqual(result['reading'], 'ふぁくとりーれんず')
            self.assertEqual(result['reading_sources'][0]['link'], sources[0]['link'])
            resolver.resolve('FactoryLens', sources)
            self.assertEqual(fetch.call_count, 1)

    def test_conflicting_sources_stay_out_of_import_tsv(self):
        with patch.object(collector, 'public_reading_document', side_effect=['Name（ネーム）', 'Name（ナメ）']):
            result = collector.ReadingResolver().resolve('Name', evidence('Name'))
        self.assertEqual(result['reading_status'], 'conflict')
        rendered = collector.render_rows([{**candidate('Name'), **result}])
        self.assertNotIn('要確認\tName', rendered)
        self.assertIn('出典間で読みが異なる', rendered)

    def test_unavailable_sources_and_page_budget_are_best_effort(self):
        with patch.object(collector, 'public_reading_document', side_effect=urllib.error.URLError('offline')) as fetch:
            resolver = collector.ReadingResolver(page_limit=1)
            result = resolver.resolve('Name', evidence('Name'))
            resolver.resolve('Other', evidence('Other'))
            self.assertEqual(result['reading_status'], 'source_unavailable')
            self.assertEqual(fetch.call_count, 1)

    def test_reviewed_homonym_requires_matching_context(self):
        self.readings_path.write_text(json.dumps([{
            'word': 'dots', 'reading': 'どっつ', 'context': ['OpenAI'],
            'sources': [{'source': 'review', 'link': 'https://publisher.example/review'}],
        }]), encoding='utf-8')
        with patch.object(collector, 'public_reading_document', return_value=''):
            resolver = collector.ReadingResolver()
            self.assertEqual(resolver.resolve('dots', evidence('dots'))['reading'], '要確認')
            sources = evidence('dots')
            sources[0]['title'] = 'OpenAI の dots 発表'
            self.assertEqual(resolver.resolve('dots', sources)['reading'], 'どっつ')

    def test_google_news_resolution_uses_signed_public_response(self):
        wrapper = '<div data-n-a-sg="signature" data-n-a-ts="123"></div>'
        response = ")]}'\n\n" + json.dumps([['wrb.fr', 'Fbv4je', json.dumps(['garturlres', 'https://publisher.example/article', 1])]])
        with patch.object(collector, 'public_reading_document', return_value=wrapper), patch.object(collector, 'request', return_value=response.encode()) as request:
            self.assertEqual(collector.publisher_url('https://news.google.com/rss/articles/ABC?oc=5'), 'https://publisher.example/article')
            payload = urllib.parse.parse_qs(request.call_args.kwargs['body'].decode())['f.req'][0]
            inner = json.loads(json.loads(payload)[0][0][1])
            self.assertEqual(inner[-3:], ['ABC', 123, 'signature'])

    def test_refresh_updates_table_tsv_metadata_seen_and_preserves_manual_text(self):
        old = candidate('FactoryLens')
        rows = [old, candidate('Unresolved')]
        github = FakeGitHub([issue(1, rows, '\n手書きメモは保持')])
        github.issues[0]['body'] = github.issues[0]['body'].replace(old['reading_note'] + ' |', old['reading_note'] + ' / 担当者メモ |', 1)
        collector.append_seen(rows)
        with patch.object(collector, 'github_api', side_effect=github), patch.object(collector, 'public_reading_document', return_value='FactoryLens（ファクトリーレンズ）'):
            self.assertEqual(collector.refresh_issue_readings(github.issues, limit=None), 1)
        updated = github.issues[0]
        self.assertIn('手書きメモは保持', updated['body'])
        self.assertIn('担当者メモ', updated['body'])
        self.assertIn('ふぁくとりーれんず\tFactoryLens', updated['body'])
        self.assertNotIn('要確認\tUnresolved', updated['body'])
        self.assertEqual(collector.issue_rows(updated)[0]['reading'], 'ふぁくとりーれんず')
        self.assertIn('ふぁくとりーれんず\tFactoryLens', self.seen_path.read_text())
        self.assertFalse(any(method == 'POST' for method, _, _ in github.calls))

    def test_refresh_handles_multiple_sections_without_duplicate_tsv_rows(self):
        a, b = candidate('Alpha'), candidate('Beta')
        original = issue(1, [a])
        original['body'] += '\n\n## 追加候補\n\n' + collector.render_rows([b])
        updates = {'alpha': {'reading': 'あるふぁ'}, 'beta': {'reading': 'べーた'}}
        body = collector.replace_issue_readings(original, updates)
        self.assertEqual(body.count('あるふぁ\tAlpha'), 1)
        self.assertEqual(body.count('べーた\tBeta'), 1)
        self.assertEqual([row['reading'] for row in collector.issue_rows({**original, 'body': body})], ['あるふぁ', 'べーた'])

    def test_refresh_does_not_overwrite_manual_readings(self):
        original = issue(1, [candidate('Alpha')])
        original['body'] = original['body'].replace('| 要確認 | Alpha |', '| あるふぁ | Alpha |')
        self.assertEqual(collector.issue_rows(original)[0]['reading'], 'あるふぁ')
        self.assertEqual(collector.replace_issue_readings(original, {'alpha': {'reading': 'あーるふぁ'}}), original['body'])

    def test_legacy_unresolved_rows_remain_in_metadata_after_tsv_exclusion(self):
        original = {'number': 1, 'title': '新語候補 2026-10-02', 'body':
                    '手書きメモ\n```tsv\n読み\t表記\t左ID\t右ID\t品詞\n要確認\tAlpha\t1920\t1920\t名詞\n```'}
        body = collector.replace_issue_readings(original, {'alpha': {'reading': '要確認'}})
        self.assertEqual(collector.issue_rows({**original, 'body': body})[0]['word'], 'Alpha')
        self.assertNotIn('要確認\tAlpha', body)
        self.assertIn('手書きメモ', body)

    def test_daily_backfill_rotates_beyond_first_ten_unresolved_words(self):
        github = FakeGitHub([issue(1, [candidate(f'Name{i}') for i in range(25)])])
        checked = []
        resolver = collector.ReadingResolver()
        def resolve(word, sources):
            checked.append(word)
            return {'reading': '要確認'}
        with patch.object(collector, 'github_api', side_effect=github), patch.object(resolver, 'resolve', side_effect=resolve):
            collector.refresh_issue_readings(github.issues, resolver=resolver, now=NOW)
            first = set(checked)
            checked.clear()
            collector.refresh_issue_readings(github.issues, resolver=resolver, now=NOW + dt.timedelta(days=1))
        self.assertEqual(len(first), 10)
        self.assertEqual(len(set(checked)), 10)
        self.assertNotEqual(first, set(checked))

    def test_published_readings_recover_seen_sync_without_second_issue_write(self):
        original = candidate('Alpha')
        updated = {**original, 'reading': 'あるふぁ'}
        collector.append_seen([original])
        github = FakeGitHub([issue(1, [updated])])
        with patch.object(collector, 'github_api', side_effect=github):
            self.assertEqual(collector.refresh_issue_readings(github.issues), 0)
        self.assertIn('あるふぁ\tAlpha', self.seen_path.read_text())
        self.assertEqual(github.calls, [])

    def test_legacy_unknown_note_is_removed_when_reading_is_resolved(self):
        row = candidate('Alpha')
        original = issue(1, [row])
        original['body'] = original['body'].replace(row['reading_note'], '読みは自動推測せず要確認 / 手書き注記', 1)
        body = collector.replace_issue_readings(original, {'alpha': {'reading': 'あるふぁ', 'reading_method': 'source', 'reading_status': 'confirmed', 'reading_note': ''}})
        self.assertNotIn('読みは自動推測せず要確認', body)
        self.assertIn('手書き注記', body)

    def test_malformed_publisher_resolution_remains_best_effort(self):
        sources = evidence('Alpha')
        sources[0]['link'] = 'https://news.google.com/rss/articles/ABC'
        response = ")]}'\n\n" + json.dumps([['wrb.fr', 'Fbv4je', '[]']])
        wrapper = '<div data-n-a-sg="signature" data-n-a-ts="123"></div>'
        with patch.object(collector, 'public_reading_document', return_value=wrapper), patch.object(collector, 'request', return_value=response.encode()):
            self.assertEqual(collector.ReadingResolver().resolve('Alpha', sources)['reading'], '要確認')
