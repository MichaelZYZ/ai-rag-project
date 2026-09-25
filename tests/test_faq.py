import tempfile
import unittest
from pathlib import Path
from fastapi.testclient import TestClient

from app import core
from app.main import app


class FaqExtractionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.previous_db = core.DB_PATH
        core.DB_PATH = Path(self.temp.name) / 'faq.sqlite3'
        core.init_db()
        core.add_product('cloud_box', '云盒')
        self.client = TestClient(app)

    def tearDown(self):
        core.DB_PATH = self.previous_db
        self.temp.cleanup()

    def test_explicit_and_rule_faqs_answer_from_the_correct_version(self):
        doc = ('# 常见问题\n'
               'Q: 云盒如何重置密码？\n'
               'A: 在登录页点击找回密码，然后使用注册邮箱重置。\n'
               '# 功能\n'
               '云盒 2.0 支持导出 PDF 文件。\n'
               '云盒 2.0 的附件上传限制为单个文件 25 MB。')
        added = core.add_document('cloud_box', '2.0', 'FAQ', '帮助', 'help.md', doc.encode())
        self.assertGreaterEqual(added['faqs'], 3)
        listed = self.client.get('/api/faqs', params={'product_id': 'cloud_box', 'version': '2.0'}).json()
        self.assertTrue(any(f['kind'] == 'explicit' and '重置密码' in f['question'] for f in listed))
        self.assertEqual(core.ask('云盒如何重置密码？', product_id='cloud_box', version='2.0')['answer'],
                         '在登录页点击找回密码,然后使用注册邮箱重置。')
        supported = core.ask('云盒支持导出 PDF 吗？', product_id='cloud_box', version='2.0')
        self.assertEqual(supported['answer'], '云盒 2.0 支持导出 PDF 文件。')
        self.assertEqual(supported['sources'][0]['source'], 'help.md')
        self.assertEqual(core.ask('云盒支持导出 Word 吗？', product_id='cloud_box', version='2.0')['status'],
                         'no_answer')

    def test_conflicting_explicit_answers_go_to_human(self):
        for answer, filename in [('最多 10 个项目。', 'one.md'), ('最多 20 个项目。', 'two.md')]:
            core.add_document('cloud_box', '1.0', 'FAQ', filename, filename,
                              ('# 常见问题\nQ: 云盒最多支持多少个项目？\nA: ' + answer).encode())
        result = core.ask('云盒最多支持多少个项目？', product_id='cloud_box', version='1.0')
        self.assertEqual(result['status'], 'handoff')
        self.assertIn('冲突', result['answer'])
        self.assertIsNotNone(result['handoff_id'])

    def test_inline_faq_keeps_answer_separate_from_following_fact(self):
        doc = ('# FAQ\nQ: 云盒支持离线使用吗？A: 云盒支持离线查看已下载的文件。\n'
               '云盒的附件上传限制为单个文件 30 MB。')
        added = core.add_document('cloud_box', '3.0', 'FAQ', '说明', 'inline.md', doc.encode())
        self.assertGreaterEqual(added['chunks'], 2)
        result = core.ask('云盒支持离线使用吗？', product_id='cloud_box', version='3.0')
        self.assertEqual(result['answer'], '云盒支持离线查看已下载的文件。')
        self.assertNotIn('30 MB', result['answer'])

    def test_common_warranty_question_uses_documented_fact(self):
        core.add_document('cloud_box', '4.0', '售后', '售后说明', 'warranty.md',
                          '云盒 4.0 的保修期为 12 个月。'.encode())
        result = core.ask('云盒保修期是多久？', product_id='cloud_box', version='4.0')
        self.assertEqual(result['status'], 'answered')
        self.assertIn('12 个月', result['answer'])


if __name__ == '__main__':
    unittest.main()
