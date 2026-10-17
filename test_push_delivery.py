import sqlite3
import unittest
from contextlib import ExitStack
from unittest.mock import Mock, patch

import requests

from smzdm import CONFIG, SmzdmScraper


def token_response(token='TOKEN_private_access'):
    response = Mock(status_code=200)
    response.json.return_value = {
        'errcode': 0, 'errmsg': 'ok', 'access_token': token, 'expires_in': 7200,
    }
    return response


def send_response(result, status_code=200):
    response = Mock(status_code=status_code)
    response.json.return_value = result
    return response


class PushDeliveryTests(unittest.TestCase):
    def setUp(self):
        self.patches = ExitStack()
        self.addCleanup(self.patches.close)
        self.patches.enter_context(patch('smzdm.DB_PATH', ':memory:'))
        self.patches.enter_context(patch('smzdm.WECOM_CORPID', 'ww_private_corp'))
        self.patches.enter_context(patch('smzdm.WECOM_AGENTID', '1000003'))
        self.patches.enter_context(patch('smzdm.WECOM_SECRET', 'SECRET_private_value'))
        self.patches.enter_context(patch('smzdm.WECOM_TOUSER', '@all'))
        self.patches.enter_context(patch('smzdm.DRY_RUN', False))
        self.get = self.patches.enter_context(patch('smzdm.requests.get'))
        self.get.return_value = token_response()
        self.scraper = SmzdmScraper()
        self.scraper.keyword_rules = []
        self.addCleanup(self.scraper._cleanup)
        self.scraper._build_notification_summary = Mock(return_value='19.9元｜测试商品｜互动升温')
        self.data = {
            'id': '178999999',
            'title': '测试商品',
            'fingerprint': 'test-product-fingerprint',
            'product_key': 'jd:123456',
            'mall': '京东',
            'quality_path': '升温好价',
            'price': '19.9元',
            'price_value': 19.9,
            'comments': 6,
            'collection': 8,
            'worthy': 10,
            'unworthy': 1,
        }

    @staticmethod
    def accepted_result():
        return {'errcode': 0, 'errmsg': 'ok', 'msgid': 'MSGID_abc123'}

    def run_one_candidate(self):
        """保留真实发送和历史落库，用一个已经通过筛选的候选验证边界。"""
        with ExitStack() as mocks:
            mocks.enter_context(patch.dict(CONFIG, {
                'jd_self_filter_enabled': False,
                'comment_level_check_enabled': False,
                'shill_detection_enabled': False,
            }))
            mocks.enter_context(patch.object(
                self.scraper, '_scan_and_filter_stage1', return_value=[self.data]))
            mocks.enter_context(patch.object(self.scraper, '_search_keyword_deals'))
            mocks.enter_context(patch.object(self.scraper, '_enrich_candidates_from_channel_apis'))
            mocks.enter_context(patch.object(self.scraper, '_cleanup'))
            mocks.enter_context(patch.object(self.scraper, '_print_statistics'))
            mocks.enter_context(patch.object(
                self.scraper, '_prioritize_candidates', side_effect=lambda candidates: candidates))
            for method in (
                '_should_defer_for_comment_unavailable', '_is_slow_growth_candidate',
                '_should_wait_for_trend_confirmation', '_is_low_growth_candidate',
            ):
                mocks.enter_context(patch.object(self.scraper, method, return_value=False))
            mocks.enter_context(patch('smzdm.time.sleep'))
            self.scraper.run()

    def assert_no_history(self):
        self.assertEqual(self.scraper.conn.execute('SELECT COUNT(*) FROM history').fetchone()[0], 0)
        self.assertNotIn(self.data['id'], self.scraper.seen_ids)
        self.assertEqual(self.scraper.stats['total_sent'], 0)
        self.assertEqual(self.scraper.stats['total_push_failed'], 1)
        self.assertNotIn('push_send_record_id', self.data)

    @patch('smzdm.requests.post')
    def test_errcode_zero_saves_history_with_msgid_and_news_payload(self, post):
        post.return_value = send_response(self.accepted_result())
        self.data['link'] = 'https://www.smzdm.com/p/178999999/'
        self.data['pic'] = 'http://y.zdmimg.com/202610/17/abc.jpg_d320.jpg'
        with self.assertLogs(level='INFO') as logs:
            self.run_one_candidate()
        self.assertEqual(self.scraper.stats['total_sent'], 1)
        self.assertEqual(self.scraper.stats['total_push_failed'], 0)
        row = self.scraper.conn.execute(
            'SELECT id, send_record_id, price_value FROM history').fetchone()
        self.assertEqual(row, ('178999999', 'MSGID_abc123', 19.9))
        self.assertEqual(post.call_args.kwargs['params'], {'access_token': 'TOKEN_private_access'})
        self.assertTrue(post.call_args.args[0].endswith('/cgi-bin/message/send'))
        payload = post.call_args.kwargs['json']
        self.assertEqual(payload['msgtype'], 'news')
        self.assertEqual(payload['touser'], '@all')
        self.assertEqual(payload['agentid'], 1000003)
        article = payload['news']['articles'][0]
        self.assertEqual(article['url'], 'https://www.smzdm.com/p/178999999/')
        self.assertEqual(article['picurl'], 'http://y.zdmimg.com/202610/17/abc.jpg_d320.jpg')
        self.assertTrue(article['title'].startswith('19.9元 测试商品'))
        self.assertIn('19.9元｜测试商品｜互动升温', article['description'])
        get_params = self.get.call_args.kwargs['params']
        self.assertEqual(get_params, {'corpid': 'ww_private_corp', 'corpsecret': 'SECRET_private_value'})
        message = '\n'.join(logs.output)
        self.assertIn('errcode:0', message)
        self.assertNotIn('SECRET_private_value', message)
        self.assertNotIn('TOKEN_private_access', message)

    @patch('smzdm.requests.post')
    def test_access_token_is_cached_within_a_run(self, post):
        post.return_value = send_response(self.accepted_result())
        self.assertTrue(self.scraper._send_notification(dict(self.data)))
        self.assertTrue(self.scraper._send_notification(dict(self.data, id='178999998')))
        self.get.assert_called_once()
        self.assertEqual(post.call_count, 2)

    @patch('smzdm.requests.post')
    def test_expired_token_is_refreshed_once_then_sent(self, post):
        for code in (40014, 42001):
            with self.subTest(code=code):
                post.reset_mock()
                self.get.reset_mock()
                self.scraper.wecom_access_token = ''
                self.get.side_effect = [token_response('TOKEN_old'), token_response('TOKEN_new')]
                post.side_effect = [
                    send_response({'errcode': code, 'errmsg': 'access_token expired'}),
                    send_response(self.accepted_result()),
                ]
                self.assertTrue(self.scraper._send_notification(dict(self.data)))
                self.assertEqual(self.get.call_count, 2)
                self.assertEqual(post.call_count, 2)
                self.assertEqual(post.call_args.kwargs['params'], {'access_token': 'TOKEN_new'})

    @patch('smzdm.requests.post')
    def test_repeated_token_error_is_not_retried_forever(self, post):
        self.get.side_effect = [token_response('TOKEN_a'), token_response('TOKEN_b')]
        post.return_value = send_response({'errcode': 42001, 'errmsg': 'expired'})
        self.assertFalse(self.scraper._send_notification(dict(self.data)))
        self.assertEqual(post.call_count, 2)
        self.assertEqual(self.scraper.stats['total_push_failed'], 1)

    @patch('smzdm.requests.post')
    def test_nonzero_errcode_and_malformed_results_never_enter_history(self, post):
        cases = [
            {'errcode': 81013, 'errmsg': 'user & party & tag all invalid'},
            {'errcode': 60020, 'errmsg': 'not allow to access from your ip'},
            {'errcode': '0', 'errmsg': 'ok'},
            {'errcode': False, 'errmsg': 'ok'},
            {'errmsg': 'ok'},
            ['unexpected-json-array'],
            None,
        ]
        for result in cases:
            with self.subTest(result=result):
                self.scraper.stats['total_push_failed'] = 0
                post.return_value = send_response(result)
                self.run_one_candidate()
                self.assert_no_history()

    @patch('smzdm.requests.post')
    def test_token_failure_does_not_submit_message(self, post):
        self.get.return_value = Mock(status_code=200)
        self.get.return_value.json.return_value = {'errcode': 40001, 'errmsg': 'invalid credential SECRET_private_value'}
        with self.assertLogs(level='ERROR') as logs:
            self.run_one_candidate()
        self.assert_no_history()
        post.assert_not_called()
        self.assertNotIn('SECRET_private_value', '\n'.join(logs.output))

    @patch('smzdm.requests.post')
    def test_http_error_and_invalid_json_never_enter_history(self, post):
        post.return_value = send_response({}, status_code=503)
        self.run_one_candidate()
        self.assert_no_history()
        post.return_value.json.assert_not_called()
        self.scraper.stats['total_push_failed'] = 0
        post.return_value = Mock(status_code=200)
        post.return_value.json.side_effect = ValueError('body contains TOKEN_private_access')
        with self.assertLogs(level='ERROR') as logs:
            self.run_one_candidate()
        self.assert_no_history()
        self.assertNotIn('TOKEN_private_access', '\n'.join(logs.output))

    @patch('smzdm.requests.post')
    def test_post_timeout_is_unknown_and_is_not_retried(self, post):
        post.side_effect = requests.ReadTimeout('request included TOKEN_private_access')
        with self.assertLogs(level='ERROR') as logs:
            self.run_one_candidate()
        self.assert_no_history()
        self.assertEqual(self.scraper.stats['total_push_unknown'], 1)
        post.assert_called_once()
        self.assertNotIn('TOKEN_private_access', '\n'.join(logs.output))

    @patch('smzdm.requests.post')
    def test_errmsg_redacts_credentials_and_control_characters(self, post):
        self.scraper.wecom_access_token = 'TOKEN_private_access'
        post.return_value = send_response({
            'errcode': 40003, 'errmsg': 'SECRET_private_value\nTOKEN_private_access\t失败'})
        with self.assertLogs(level='ERROR') as logs:
            self.assertFalse(self.scraper._send_notification(self.data))
        message = '\n'.join(logs.output)
        self.assertNotIn('SECRET_private_value', message)
        self.assertNotIn('TOKEN_private_access', message)
        self.assertNotIn('\t', message)
        self.assertIn('[已隐藏]', message)

    @patch('smzdm.requests.post')
    def test_invalid_recipient_configuration_does_not_submit_a_post(self, post):
        for touser in ('', ' user', 'user\n', 'a,b', 'a\x00', None):
            with self.subTest(touser=touser), patch('smzdm.WECOM_TOUSER', touser):
                self.assertFalse(self.scraper._send_notification(self.data))
        post.assert_not_called()

    @patch('smzdm.requests.post')
    def test_dry_run_logs_payload_without_requests_or_history(self, post):
        self.data['link'] = 'https://www.smzdm.com/p/178999999/'
        with patch('smzdm.DRY_RUN', True), self.assertLogs(level='INFO') as logs:
            self.run_one_candidate()
        post.assert_not_called()
        self.get.assert_not_called()
        self.assertEqual(self.scraper.conn.execute('SELECT COUNT(*) FROM history').fetchone()[0], 0)
        self.assertEqual(self.scraper.stats['total_push_dry_run'], 1)
        self.assertEqual(self.scraper.stats['total_push_failed'], 0)
        message = '\n'.join(logs.output)
        self.assertIn('DRY_RUN 推送载荷', message)
        self.assertIn('"msgtype": "news"', message)
        self.assertNotIn('SECRET_private_value', message)

    def test_get_retries_never_sleep_for_a_rate_limit_retry_after(self):
        retry = self.scraper.session.get_adapter('https://').max_retries
        self.assertFalse(retry.respect_retry_after_header)
        self.assertFalse(retry.is_retry('GET', 429, has_retry_after=True))
        self.assertFalse(retry.is_retry('GET', 403, has_retry_after=True))
        self.assertFalse(retry.is_retry('GET', 202, has_retry_after=True))
        for status in (500, 502, 503, 504):
            self.assertTrue(retry.is_retry('GET', status, has_retry_after=True))
        self.assertFalse(retry.is_retry('POST', 503, has_retry_after=True))
        response = Mock(headers={'Retry-After': '3600'})
        with patch('urllib3.util.retry.time.sleep') as sleep:
            retry.sleep(response)
        sleep.assert_not_called()

    def test_history_migration_preserves_old_rows_and_adds_record_column(self):
        legacy = SmzdmScraper.__new__(SmzdmScraper)
        legacy.conn = sqlite3.connect(':memory:')
        self.addCleanup(legacy.conn.close)
        legacy.conn.execute('''CREATE TABLE history (
            id TEXT PRIMARY KEY, title TEXT, last_seen DATETIME DEFAULT CURRENT_TIMESTAMP
        )''')
        legacy.conn.execute('INSERT INTO history (id, title) VALUES (?, ?)', ('old-id', '旧商品'))
        legacy._ensure_history_columns()
        legacy._ensure_history_columns()
        columns = {row[1] for row in legacy.conn.execute('PRAGMA table_info(history)')}
        self.assertIn('send_record_id', columns)
        self.assertEqual(
            legacy.conn.execute('SELECT id, title, send_record_id FROM history').fetchone(),
            ('old-id', '旧商品', None),
        )


if __name__ == '__main__':
    unittest.main()
