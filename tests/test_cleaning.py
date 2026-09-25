import tempfile
import unittest
from pathlib import Path

from app import core
from app.cleaning import clean_document


class CleaningFlowTests(unittest.TestCase):
    def test_noise_removed_without_changing_product_limits(self):
        raw = ('＃ 产品参数\r\n\u200b\r\n第 1 页 / 共 2 页\r\n'
               '最大标签数：  20  个\r\n最大标签数：  20  个\r\n'
               '附件上限：\r\n25 MB\r\nPage 2 of 2')
        cleaned, report = clean_document(raw)
        self.assertNotIn('第 1 页', cleaned)
        self.assertNotIn('Page 2', cleaned)
        self.assertEqual(cleaned.count('最大标签数: 20 个'), 1)
        self.assertIn('25 MB', cleaned)
        self.assertEqual(report['page_markers_removed'], 2)
        self.assertEqual(report['adjacent_duplicates_removed'], 1)
        chunks = core.split_text(cleaned)
        self.assertTrue(any('附件上限: 25 MB' in c for c in chunks))
        self.assertTrue(all('产品参数' in c for c in chunks))

    def test_import_reports_cleaning_and_preserves_answer(self):
        original_db = core.DB_PATH
        with tempfile.TemporaryDirectory() as temp:
            core.DB_PATH = Path(temp) / 'test.sqlite3'
            try:
                core.init_db()
                core.add_product('test_product', '测试产品')
                result = core.add_document('test_product', '1.0', '参数', '测试', 'test.md',
                                           '# 参数\n\n最大标签数：20 个\nPage 1 of 1'.encode())
                self.assertEqual(result['cleaning']['page_markers_removed'], 1)
                with core.connect() as db:
                    row = db.execute('SELECT cleaning_report FROM documents').fetchone()
                    self.assertIn('page_markers_removed', row['cleaning_report'])
                hits = core.retrieve('最大标签数是多少', 'test_product', '1.0')
                self.assertIn('20 个', hits[0]['text'])
            finally:
                core.DB_PATH = original_db


if __name__ == '__main__':
    unittest.main()
