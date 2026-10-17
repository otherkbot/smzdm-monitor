import hashlib
import os
import tempfile
import unittest
from contextlib import ExitStack
from datetime import datetime, timedelta
from unittest.mock import Mock, patch

import requests

from smzdm import CONFIG, SmzdmScraper


class PushKeywordCooldownTests(unittest.TestCase):
    """旧 WXPusher 冷却状态保持兼容：企业微信不会写入新冷却，但旧缓存行仍被尊重直至过期。"""

    def setUp(self):
        self.patches = ExitStack()
        self.addCleanup(self.patches.close)
        self.patches.enter_context(patch('smzdm.DB_PATH', ':memory:'))
        self.patches.enter_context(patch('smzdm.WECOM_CORPID', 'ww_private_corp'))
        self.patches.enter_context(patch('smzdm.WECOM_AGENTID', '1000003'))
        self.patches.enter_context(patch('smzdm.WECOM_SECRET', 'SECRET_private_value'))
        self.patches.enter_context(patch('smzdm.WECOM_TOUSER', '@all'))
        self.patches.enter_context(patch('smzdm.DRY_RUN', False))
        self.post = self.patches.enter_context(patch('smzdm.requests.post'))
        self.post.return_value.status_code = 200
        get = self.patches.enter_context(patch('smzdm.requests.get'))
        get.return_value.status_code = 200
        get.return_value.json.return_value = {'errcode': 0, 'access_token': 'TOKEN_private'}
        self.scraper = SmzdmScraper()
        self.scraper.keyword_rules = []
        self.addCleanup(self.scraper._cleanup)
        self.scraper._build_notification_summary = Mock(return_value='19.9元 测试商品')
        self.data = {
            'id': '183743621', 'title': '极空间 NAS 测试商品',
            'fingerprint': 'nas-model', 'mall': '京东',
            'price': '19.9元', 'price_value': 19.9,
            'worthy': 14, 'unworthy': 0, 'collection': 5, 'comments': 6,
            'age_hours': 2, 'quality_path': '早期好价',
        }

    @staticmethod
    def expected_recipient_key(touser='@all'):
        return hashlib.sha256(f'ww_private_corp:1000003:{touser}'.encode('utf-8')).hexdigest()

    def test_wecom_failures_never_create_cooldown(self):
        """企业微信没有 WXPusher 式的接收人关键词拒绝；任何失败都只计为推送失败。"""
        for result in (
            {'errcode': 81013, 'errmsg': 'user & party & tag all invalid'},
            {'errcode': 45009, 'errmsg': 'api freq out of limit'},
            {'errcode': 0, 'errmsg': 'ok', 'invaliduser': 'someone'},
        ):
            with self.subTest(result=result):
                self.post.return_value.json.return_value = result
                self.scraper._send_notification(dict(self.data))
                self.assertFalse(self.scraper._is_push_keyword_cooling_down(self.data['id']))
        self.post.side_effect = requests.ReadTimeout('unknown POST outcome')
        self.assertFalse(self.scraper._send_notification(self.data))
        self.assertEqual(self.scraper.stats['total_push_unknown'], 1)
        self.assertEqual(self.scraper.stats['total_push_keyword_filtered'], 0)
        self.assertEqual(self.scraper.conn.execute(
            'SELECT COUNT(*) FROM push_keyword_cooldowns').fetchone()[0], 0)

    def test_legacy_cooldown_row_still_blocks_send_and_stores_only_hash(self):
        self.scraper._save_push_keyword_cooldown(self.data['id'])
        row = self.scraper.conn.execute(
            'SELECT recipient_key, article_id FROM push_keyword_cooldowns').fetchone()
        self.assertEqual(row, (self.expected_recipient_key(), self.data['id']))
        with self.assertLogs(level='INFO') as logs:
            self.assertFalse(self.scraper._send_notification(self.data))
        self.post.assert_not_called()
        self.assertIn('关键词冷却跳过', '\n'.join(logs.output))
        self.assertEqual(self.scraper.stats['total_push_failed'], 0)

    def test_cooldown_is_scoped_to_recipient_and_article_and_expiry_allows_retry(self):
        self.scraper._save_push_keyword_cooldown(self.data['id'])
        self.assertTrue(self.scraper._is_push_keyword_cooling_down(self.data['id']))
        self.assertFalse(self.scraper._is_push_keyword_cooling_down('different-article'))
        with patch('smzdm.WECOM_TOUSER', 'another_user'):
            self.assertFalse(self.scraper._is_push_keyword_cooling_down(self.data['id']))
        self.scraper.conn.execute(
            'UPDATE push_keyword_cooldowns SET expires_at = ?',
            (datetime.utcnow().isoformat(timespec='seconds'),),
        )
        self.assertFalse(self.scraper._is_push_keyword_cooling_down(self.data['id']))
        self.post.return_value.json.return_value = {'errcode': 0, 'errmsg': 'ok', 'msgid': 'MSG1'}
        self.assertTrue(self.scraper._send_notification(self.data))
        self.post.assert_called_once()
        self.assertEqual(self.scraper.stats['total_push_keyword_cooldown_skipped'], 0)

    def test_cooling_list_candidate_keeps_snapshot_without_enrichment_or_comment_requests(self):
        self.scraper._save_push_keyword_cooldown(self.data['id'])
        self.scraper._filter_stage1 = Mock(side_effect=AssertionError('cooling item should stop early'))
        candidates = []
        self.scraper._consider_stage1_candidate(self.data, candidates)
        self.scraper._commit_candidate_snapshots()
        self.assertEqual(candidates, [])
        rows = self.scraper.conn.execute(
            'SELECT article_id, worthy, comments FROM candidate_snapshots'
        ).fetchall()
        self.assertEqual(rows, [(self.data['id'], 14, 6)])
        self.scraper._filter_stage1.assert_not_called()
        self.post.assert_not_called()
        self.assertEqual(self.scraper.stats['total_candidate_snapshots_saved'], 1)
        self.assertEqual(self.scraper.stats['total_push_keyword_cooldown_skipped'], 1)

    def test_run_skips_external_checks_for_a_cooling_candidate(self):
        self.scraper._save_push_keyword_cooldown(self.data['id'])
        with ExitStack() as mocks:
            mocks.enter_context(patch.object(
                self.scraper, '_scan_and_filter_stage1', return_value=[self.data]))
            mocks.enter_context(patch.object(self.scraper, '_search_keyword_deals'))
            mocks.enter_context(patch.object(self.scraper, '_enrich_candidates_from_channel_apis'))
            mocks.enter_context(patch.object(self.scraper, '_cleanup'))
            mocks.enter_context(patch.object(self.scraper, '_print_statistics'))
            comment_check = mocks.enter_context(patch.object(self.scraper, '_check_comment_user_levels'))
            self.scraper.run()
        comment_check.assert_not_called()
        self.post.assert_not_called()
        self.assertEqual(self.scraper.stats['total_push_keyword_cooldown_skipped'], 1)
        self.assertEqual(self.scraper.stats['total_push_failed'], 0)

    def test_late_recheck_excludes_cooldown_before_limit_and_backfills_request_budget(self):
        captured_at = (datetime.utcnow() - timedelta(hours=2)).isoformat(timespec='seconds')
        for index in range(20):
            article_id = str(183700000 + index)
            self.scraper.conn.execute(
                '''INSERT INTO candidate_snapshots
                   (item_key, article_id, title, worthy, collection, comments,
                    composite_score, age_hours, captured_at)
                   VALUES (?, ?, ?, 10, 8, 8, 50, 2, ?)''',
                (f'id:{article_id}', article_id, f'正常商品 {index}', captured_at),
            )
            if index < 18:
                self.scraper._save_push_keyword_cooldown(article_id)
        rows = self.scraper._select_late_recheck_rows(set(), 2)
        self.assertEqual([row['article_id'] for row in rows], ['183700018', '183700019'])
        self.assertEqual(self.scraper.late_recheck_eligible_count, 2)
        with patch.object(self.scraper, '_fetch_late_recheck_detail', return_value=None) as fetch:
            self.scraper._append_late_recheck_candidates(set(), [])
        self.assertEqual([call.args[0] for call in fetch.call_args_list], ['183700018', '183700019'])
        self.assertEqual(self.scraper.stats['total_late_recheck_selected'], 2)
        self.assertEqual(self.scraper.conn.execute('SELECT COUNT(*) FROM history').fetchone()[0], 0)

    def test_expired_state_is_cleaned_without_deleting_active_recipient_state(self):
        self.scraper._save_push_keyword_cooldown('active')
        self.scraper._save_push_keyword_cooldown('expired')
        self.scraper.conn.execute(
            'UPDATE push_keyword_cooldowns SET expires_at = ? WHERE article_id = ?',
            ((datetime.utcnow() - timedelta(seconds=1)).isoformat(timespec='seconds'), 'expired'),
        )
        self.scraper._clean_old_records()
        self.assertEqual(self.scraper.conn.execute(
            'SELECT article_id FROM push_keyword_cooldowns').fetchall(), [('active',)])

    def test_new_state_survives_reopening_the_database_and_migration_is_idempotent(self):
        with tempfile.TemporaryDirectory() as directory, patch(
                'smzdm.DB_PATH', os.path.join(directory, 'monitor.db')):
            first = SmzdmScraper()
            try:
                first._save_push_keyword_cooldown(self.data['id'])
            finally:
                first._cleanup()
            second = SmzdmScraper()
            try:
                self.assertTrue(second._is_push_keyword_cooling_down(self.data['id']))
                self.assertEqual(second.conn.execute('SELECT COUNT(*) FROM history').fetchone()[0], 0)
            finally:
                second._cleanup()


if __name__ == '__main__':
    unittest.main()
