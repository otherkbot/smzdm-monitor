import unittest
from contextlib import ExitStack
from unittest.mock import Mock, patch

import requests

from smzdm import CONFIG, SmzdmScraper
from test_push_delivery import PushDeliveryTests, send_response

WEBHOOK = 'https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=KEY_private_webhook_123'


class WebhookDeliveryTests(unittest.TestCase):
    """群机器人 Webhook：无需 token，errcode=0 才写历史，节流与 45009 重试一次。"""

    run_one_candidate = PushDeliveryTests.run_one_candidate
    assert_no_history = PushDeliveryTests.assert_no_history

    def setUp(self):
        PushDeliveryTests.setUp(self)
        self.patches.enter_context(patch('smzdm.WECOM_WEBHOOK_URL', WEBHOOK))
        self.sleep = self.patches.enter_context(patch('smzdm.time.sleep'))

    @patch('smzdm.requests.post')
    def test_webhook_success_saves_history_without_token(self, post):
        post.return_value = send_response({'errcode': 0, 'errmsg': 'ok'})
        self.data['link'] = 'https://www.smzdm.com/p/178999999/'
        self.data['pic'] = 'http://y.zdmimg.com/202610/17/abc.jpg_d320.jpg'
        with self.assertLogs(level='INFO') as logs:
            self.run_one_candidate()
        self.assertEqual(self.scraper.stats['total_sent'], 1)
        self.assertEqual(self.scraper.stats['total_push_failed'], 0)
        self.get.assert_not_called()
        self.assertEqual(post.call_args.args[0], WEBHOOK)
        self.assertNotIn('params', post.call_args.kwargs)
        payload = post.call_args.kwargs['json']
        self.assertEqual(set(payload), {'msgtype', 'news'})
        self.assertEqual(payload['msgtype'], 'news')
        article = payload['news']['articles'][0]
        self.assertEqual(set(article), {'title', 'description', 'url', 'picurl'})
        self.assertEqual(article['url'], 'https://www.smzdm.com/p/178999999/')
        self.assertTrue(article['title'].startswith('19.9元 测试商品'))
        row = self.scraper.conn.execute('SELECT id, send_record_id FROM history').fetchone()
        self.assertEqual(row, ('178999999', ''))
        message = '\n'.join(logs.output)
        self.assertIn('群机器人', message)
        self.assertNotIn('KEY_private_webhook_123', message)
        self.assertIn('key=KEY_***', message)

    @patch('smzdm.requests.post')
    def test_webhook_payload_respects_byte_limits(self, post):
        post.return_value = send_response({'errcode': 0, 'errmsg': 'ok'})
        data = dict(self.data, title='超长商品名' * 80)
        self.scraper._build_notification_summary = Mock(return_value='摘要' * 400)
        self.assertTrue(self.scraper._send_notification(data))
        article = post.call_args.kwargs['json']['news']['articles'][0]
        self.assertLessEqual(len(article['title'].encode('utf-8')), CONFIG['wecom_title_max_bytes'])
        self.assertLessEqual(len(article['description'].encode('utf-8')), CONFIG['wecom_description_max_bytes'])

    @patch('smzdm.requests.post')
    def test_webhook_nonzero_errcode_never_enters_history(self, post):
        for result in ({'errcode': 93000, 'errmsg': 'invalid webhook url KEY_private_webhook_123'},
                       {'errcode': '0'}, {'errmsg': 'ok'}, ['x'], None):
            with self.subTest(result=result):
                self.scraper.stats['total_push_failed'] = 0
                post.return_value = send_response(result)
                with self.assertLogs(level='ERROR') as logs:
                    self.run_one_candidate()
                self.assert_no_history()
                self.assertNotIn('KEY_private_webhook_123', '\n'.join(logs.output))
        self.get.assert_not_called()

    @patch('smzdm.requests.post')
    def test_webhook_http_error_and_timeout(self, post):
        post.return_value = send_response({}, status_code=502)
        self.run_one_candidate()
        self.assert_no_history()
        self.scraper.stats['total_push_failed'] = 0
        post.reset_mock()
        post.return_value = None
        post.side_effect = requests.ReadTimeout(WEBHOOK)
        with self.assertLogs(level='ERROR') as logs:
            self.run_one_candidate()
        self.assert_no_history()
        self.assertEqual(post.call_count, 1)
        self.assertEqual(self.scraper.stats['total_push_unknown'], 1)
        self.assertNotIn('KEY_private_webhook_123', '\n'.join(logs.output))

    @patch('smzdm.requests.post')
    def test_rate_limit_waits_and_retries_once(self, post):
        post.side_effect = [
            send_response({'errcode': 45009, 'errmsg': 'api freq out of limit'}),
            send_response({'errcode': 0, 'errmsg': 'ok'}),
        ]
        self.assertTrue(self.scraper._send_notification(dict(self.data)))
        self.assertEqual(post.call_count, 2)
        self.assertIn(CONFIG['wecom_webhook_rate_limit_wait'], [c.args[0] for c in self.sleep.call_args_list])

    @patch('smzdm.requests.post')
    def test_repeated_rate_limit_fails_after_one_retry(self, post):
        post.return_value = send_response({'errcode': 45009, 'errmsg': 'api freq out of limit'})
        self.assertFalse(self.scraper._send_notification(dict(self.data)))
        self.assertEqual(post.call_count, 2)
        self.assertEqual(self.scraper.stats['total_push_failed'], 1)

    @patch('smzdm.requests.post')
    def test_consecutive_sends_are_paced(self, post):
        post.return_value = send_response({'errcode': 0, 'errmsg': 'ok'})
        with patch('smzdm.time.monotonic', side_effect=[100.0, 101.0, 104.2]):
            self.assertTrue(self.scraper._send_notification(dict(self.data)))
            self.assertTrue(self.scraper._send_notification(dict(self.data, id='178999998')))
        waits = [c.args[0] for c in self.sleep.call_args_list]
        self.assertEqual(len(waits), 1)
        self.assertAlmostEqual(waits[0], CONFIG['wecom_webhook_min_interval'] - 1.0)

    @patch('smzdm.requests.post')
    def test_dry_run_does_not_post_webhook(self, post):
        with patch('smzdm.DRY_RUN', True):
            self.assertFalse(self.scraper._send_notification(dict(self.data)))
        post.assert_not_called()
        self.assertEqual(self.scraper.stats['total_push_dry_run'], 1)

    def test_mask_webhook_url(self):
        masked = SmzdmScraper._mask_webhook_url(WEBHOOK)
        self.assertNotIn('KEY_private_webhook_123', masked)
        self.assertTrue(masked.endswith('?key=KEY_***'))

    @patch('smzdm.requests.post')
    def test_app_path_unchanged_when_webhook_empty(self, post):
        post.return_value = send_response({'errcode': 0, 'errmsg': 'ok', 'msgid': 'MSGID_x'})
        with patch('smzdm.WECOM_WEBHOOK_URL', ''):
            self.assertTrue(self.scraper._send_notification(dict(self.data)))
        self.assertTrue(post.call_args.args[0].endswith('/cgi-bin/message/send'))
        self.assertEqual(post.call_args.kwargs['json']['agentid'], 1000003)
        self.get.assert_called_once()


if __name__ == '__main__':
    unittest.main()
