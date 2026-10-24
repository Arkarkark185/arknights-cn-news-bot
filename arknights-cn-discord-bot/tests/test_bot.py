import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import bot


class TestParsing(unittest.TestCase):
    def test_official(self):
        html = '''<a href="/news/0799">新闻2026 // 10 / 17《明日方舟》制作组通讯#70期</a>
                  <a href="/news/0799">duplicate</a>
                  <a href="/news/3020">公告2026 // 10 / 16停机维护公告</a>'''
        result = bot.parse_official_page(html)
        self.assertEqual([p.id for p in result], ['site:0799', 'site:3020'])
        self.assertIn('制作组通讯', result[0].text)

    def test_weibo_sina(self):
        html = '''<a href="/news/detail/5356126868869739.html">明日方舟Arknights 2026-10-20 16:07来自 微博网页版 #明日方舟# 新干员公开</a>'''
        result = bot.parse_sina_page(html)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].id, 'sina:5356126868869739')
        self.assertIn('新干员公开', result[0].text)

    def test_bilibili(self):
        data = {'code': 0, 'data': {'items': [{
            'id_str': '1234',
            'modules': {'module_dynamic': {
                'desc': {'text': '新干员登场'},
                'major': {'archive': {'bvid': 'BV1testing', 'title': '宣传PV',
                                      'cover': 'https://i0.hdslb.com/test.jpg'}}}
            }}]}}
        result = bot.parse_bilibili_api(data)
        self.assertEqual(result[0].id, 'bilibili:1234')
        self.assertEqual(result[0].url, 'https://www.bilibili.com/video/BV1testing')
        self.assertIn('宣传PV', result[0].text)

    def test_first_run_no_spam_and_next_run_sends_only_new(self):
        state = {'sources': {}}
        old = bot.Post('Official site', 'site:1', 'Older notice', 'https://ak.hypergryph.com/news/1')
        new = bot.Post('Official site', 'site:2', 'Brand new notice', 'https://ak.hypergryph.com/news/2')
        with patch.object(bot, 'send_to_discord') as sender:
            self.assertEqual(bot.process_source('official', [old], state, 'unused'), (0, 0))
            self.assertEqual(sender.call_count, 0)
            self.assertEqual(bot.process_source('official', [new, old], state, 'unused'), (1, 1))
            self.assertEqual(sender.call_count, 1)
            self.assertEqual(bot.process_source('official', [new, old], state, 'unused'), (0, 0))
            self.assertEqual(sender.call_count, 1)

    def test_state_persists(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'state.json'
            bot.save_state(path, {'sources': {'official': ['site:1']}})
            self.assertEqual(bot.load_state(path)['sources']['official'], ['site:1'])
            self.assertEqual(json.loads(path.read_text())['sources']['official'], ['site:1'])

    def test_empty_or_blocked_feed_never_initializes_source(self):
        state = {'sources': {}}
        with self.assertRaises(ValueError):
            bot.process_source('bilibili', [], state, 'unused')
        self.assertNotIn('bilibili', state['sources'])


if __name__ == '__main__':
    unittest.main()
