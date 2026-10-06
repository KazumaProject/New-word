import unittest
from unittest.mock import patch

from test_collect_new_words import collector, candidate, evidence, issue


class CandidateQualityTests(unittest.TestCase):
    def test_actual_non_words_are_rejected(self):
        for text in ('普通のメガネに見えるスマートグラス',
                     'Samsung Galaxyに決めた12億人の理由', '回避のすべはない', '未来を創る'):
            with self.subTest(text=text):
                self.assertIsNone(collector.clean_candidate(text))
                self.assertEqual(collector.extract_candidates(f'新製品「{text}」を発表'), set())

    def test_announcing_company_is_not_a_product(self):
        self.assertEqual(collector.extract_candidates('TDK、新製品「メガネビュー」を発表'), {'メガネビュー'})
        self.assertEqual(collector.extract_candidates('Samsung Galaxyに決めた12億人の理由'), set())

    def test_complete_name_survives_without_ascii_fragments(self):
        self.assertEqual(collector.extract_candidates('新サービス「ゆめNEOBANK」を発表'), {'ゆめNEOBANK'})
        self.assertEqual(collector.extract_candidates('新モデル「GPT-6.1 Sol」を発表'), {'GPT-6.1 Sol'})
        self.assertEqual(collector.extract_candidates('FactoryLensを発売'), {'FactoryLens'})

    def test_acronym_estimate_is_not_exported(self):
        with patch.object(collector, 'public_reading_document', return_value=''):
            result = collector.ReadingResolver().resolve('TDK', evidence('TDK'))
        self.assertEqual(result['reading_estimate'], 'てぃーでぃーけー')
        self.assertEqual(result['reading'], '要確認')
        rendered = collector.render_rows([{**candidate('TDK'), **result}])
        self.assertIn('推定読み: てぃーでぃーけー', rendered)
        self.assertNotIn('てぃーでぃーけー\tTDK', rendered)

    def test_estimate_refresh_preserves_manual_notes(self):
        original = issue(1, [candidate('TDK')], '\n手書きメモ')
        with patch.object(collector, 'public_reading_document', return_value=''):
            result = collector.ReadingResolver().resolve('TDK', evidence('TDK'))
        body = collector.replace_issue_readings(original, {'tdk': result})
        self.assertIn('推定読み: てぃーでぃーけー', body)
        self.assertIn('手書きメモ', body)
        self.assertEqual(collector.issue_rows({**original, 'body': body})[0]['reading'], '要確認')

    def test_conflicting_sources_are_not_hidden_by_an_estimate(self):
        with patch.object(collector, 'public_reading_document', side_effect=['TDK（テーデーケー）', 'TDK（ティーディーケー）']):
            result = collector.ReadingResolver().resolve('TDK', evidence('TDK'))
        self.assertEqual(result['reading_status'], 'conflict')
        self.assertNotIn('reading_estimate', result)

    def test_kanji_estimation_when_dependency_installed(self):
        try:
            import pykakasi
        except ImportError:
            self.skipTest('Install requirements.txt for kanji estimation')
        self.assertEqual(collector.ReadingResolver().estimate('未来技術', []), 'みらいぎじゅつ')

    def test_unknown_english_name_is_not_spelled_as_a_word(self):
        self.assertIsNone(collector.ReadingResolver().estimate('HomeThing', []))


if __name__ == '__main__':
    unittest.main()
