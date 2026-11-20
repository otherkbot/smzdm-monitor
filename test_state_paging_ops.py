import logging
import os
import sys
import unittest
from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import requests

from smzdm import (
    CONFIG,
    OPS_ALERT_MARKER,
    SmzdmScraper,
    _keyword_rule,
    format_utc_iso,
    format_utc_sql,
    main,
    parse_utc,
    send_workflow_failure_alert,
    utc_now,
)


def _super_counts():
    return {'worthy': 25, 'comments': 30, 'collection': 15}


class StatePagingAndOpsTests(unittest.TestCase):
    def setUp(self):
        self.patches = ExitStack()
        self.addCleanup(self.patches.close)
        self.patches.enter_context(patch('smzdm.DB_PATH', ':memory:'))
        self.patches.enter_context(patch('smzdm.APP_TOKEN', 'AT_private_token'))
        self.patches.enter_context(patch('smzdm.UID', 'UID_test_recipient'))
        self.patches.enter_context(patch('smzdm.time.sleep'))
        self.scraper = SmzdmScraper()
        self.addCleanup(self.scraper._cleanup)
        self.scraper._fetch_rank_rows = lambda: []
        self.scraper._append_late_recheck_candidates = lambda discovered, candidates: None

    def raw(self, article_id, age_hours, worthy=0, comments=0, collection=0, title=None):
        published = int(utc_now().timestamp() - age_hours * 3600)
        return {
            'article_id': str(article_id),
            'article_channel_type': 'youhui',
            'article_title': title or f'红豆 男士纯棉短袖T恤 {article_id}',
            'article_price': '99元',
            'article_url': f'https://www.smzdm.com/p/{article_id}/',
            'publish_date_lt': str(published),
            'article_mall': '京东',
            'tongji_hudong': f'评论_{comments},收藏_{collection},值_{worthy},不值_0',
        }

    def remember(self, article_id):
        self.scraper.conn.execute(
            'INSERT INTO history (id, title, last_seen) VALUES (?, ?, ?)',
            (str(article_id), '已见', format_utc_sql()),
        )
        self.scraper.conn.commit()

    def scan(self, pages, **config):
        merged = {
            'incremental_paging_enabled': True,
            'incremental_full_scan_every': 1000,
            'incremental_min_pages': 4,
            'incremental_min_age_hours': 3,
            'incremental_known_page_streak': 2,
            'incremental_known_ratio': 0.9,
            'request_delay': (0, 0),
        }
        merged.update(config)
        calls = []

        def fetch(page):
            calls.append(page)
            if page > len(pages):
                return []
            return pages[page - 1]

        self.scraper._fetch_page = fetch
        with patch.dict(CONFIG, merged):
            candidates = self.scraper._scan_and_filter_stage1()
        return calls, candidates

    def feed_pages(self):
        hot = _super_counts()
        pages = [
            [self.raw('hot-new', 0.2, title='小米 智能插座3 白色', **hot), self.raw('k1', 0.3)],
            [self.raw('k2', 0.8), self.raw('k3', 0.9)],
            [self.raw('k4', 1.5), self.raw('k5', 1.6)],
            [self.raw('hot-mid', 2.6, title='九阳 豆浆机 1.2L', **hot), self.raw('k6', 2.7)],
            [self.raw('k7', 3.4), self.raw('k8', 3.5)],
            [self.raw('k9', 3.8), self.raw('k10', 3.9)],
            [self.raw('hot-deep', 5.0, title='雅漾 祛痘舒缓小黑膜面膜5片', **hot)],
            [self.raw('too-old', 7.0, title='老板 天空之境系列洗碗机', **hot)],
        ]
        for key in [f'k{i}' for i in range(1, 11)]:
            self.remember(key)
        return pages

    def paths(self, candidates):
        return {item['id']: item.get('quality_path') for item in candidates}

    def test_stage1_decisions_match_the_recorded_contract(self):
        """同一批互动数据的放行路径必须和改分页之前的门槛一致。"""
        warming = self.scraper._empty_trend_metrics()
        warming.update({
            'snapshot_count': 2,
            'recent_minutes': 30,
            'elapsed_minutes': 60,
            'recent_growth_score': 13,
            'growth_score': 13,
            'recent_signal_growth_score': 5,
            'signal_growth_score': 5,
            'recent_delta_worthy': 1,
            'delta_worthy': 1,
            'recent_growth_per_hour': 26,
            'growth_per_hour': 13,
        })
        no_worthy_growth = dict(warming)
        no_worthy_growth.update({
            'recent_delta_worthy': 0,
            'delta_worthy': 0,
            'recent_signal_growth_score': 4,
            'signal_growth_score': 4,
        })
        cases = [
            ('超级好价', True, {'comments': 30, 'worthy': 25, 'collection': 15, 'unworthy': 0}),
            ('高讨论', True, {'comments': 16, 'worthy': 10, 'collection': 16, 'unworthy': 0}),
            ('均衡热度', True, {'comments': 10, 'worthy': 12, 'collection': 12, 'unworthy': 0}),
            ('早期好价', True, {'comments': 7, 'worthy': 6, 'collection': 5, 'unworthy': 0}),
            ('早期强信号', True, {'comments': 2, 'worthy': 6, 'collection': 8, 'unworthy': 0}),
            ('升温好价', True, {
                'comments': 6, 'worthy': 4, 'collection': 5, 'unworthy': 0,
                'trend_metrics': warming,
            }),
            (None, False, {
                'comments': 6, 'worthy': 4, 'collection': 5, 'unworthy': 0,
                'trend_metrics': no_worthy_growth,
            }),
            (None, False, {
                'comments': 30, 'worthy': 25, 'collection': 15, 'unworthy': 0,
                'is_sold_out': True,
            }),
            (None, False, {'comments': 20, 'worthy': 6, 'collection': 20, 'unworthy': 2}),
            ('早期好价', True, {'comments': 8, 'worthy': 6, 'collection': 5, 'unworthy': 1}),
            (None, False, {'comments': 1, 'worthy': 1, 'collection': 10, 'unworthy': 0}),
        ]
        for expected_path, expected_pass, fields in cases:
            parsed = {
                'id': 'contract',
                'title': '合同商品',
                'mall': '京东',
                'is_sold_out': False,
                'is_timeout': False,
                'status_text': '',
                'trend_metrics': self.scraper._empty_trend_metrics(),
            }
            parsed.update(fields)
            with self.subTest(expected_path=expected_path):
                self.assertEqual(self.scraper._filter_stage1(parsed), expected_pass)
                if expected_pass:
                    self.assertEqual(parsed['quality_path'], expected_path)

    def _reset_scan_memory(self):
        self.scraper.seen_ids.difference_update({'hot-new', 'hot-mid', 'hot-deep', 'too-old'})
        self.scraper.snapshot_keys_seen_this_run.clear()
        self.scraper.conn.execute('DELETE FROM candidate_snapshots')
        self.scraper.conn.execute('DELETE FROM scan_state')
        self.scraper.conn.commit()

    def test_full_scan_and_incremental_scan_agree_inside_the_warming_window(self):
        pages = self.feed_pages()
        early_calls, early_candidates = self.scan(pages)
        early_paths = self.paths(early_candidates)
        self.assertEqual(early_calls, list(range(1, 7)))
        self.assertEqual(early_paths['hot-new'], '超级好价')
        self.assertEqual(early_paths['hot-mid'], '超级好价')
        self.assertNotIn('hot-deep', early_paths)
        self.assertEqual(self.scraper.stats['total_paging_early_stops'], 1)

        self._reset_scan_memory()
        self.scraper.stats['total_paging_early_stops'] = 0
        full_calls, full_candidates = self.scan(pages, incremental_paging_enabled=False)
        full_paths = self.paths(full_candidates)
        self.assertEqual(full_calls, list(range(1, 9)))
        self.assertEqual(full_paths['hot-new'], early_paths['hot-new'])
        self.assertEqual(full_paths['hot-mid'], early_paths['hot-mid'])
        self.assertEqual(full_paths['hot-deep'], '超级好价')
        self.assertNotIn('too-old', full_paths)
        self.assertEqual(self.scraper.stats['total_paging_early_stops'], 0)
        self.assertLess(
            SmzdmScraper.estimate_list_fetch_seconds(len(early_calls)),
            SmzdmScraper.estimate_list_fetch_seconds(CONFIG['max_pages']),
        )

    def test_stale_tail_and_periodic_full_scan_still_reach_the_deep_deal(self):
        pages = self.feed_pages()
        self.scraper.conn.execute(
            '''INSERT INTO candidate_snapshots
               (item_key, article_id, title, worthy, collection, comments, age_hours, captured_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)''',
            (
                'id:stale-tail', 'stale-tail', '仍在决策窗内的未推送商品',
                6, 8, 4, 4.0, format_utc_iso(utc_now() - timedelta(minutes=40)),
            ),
        )
        self.scraper.conn.commit()
        stale_calls, stale_candidates = self.scan(pages)
        self.assertIn('hot-deep', self.paths(stale_candidates))
        self.assertEqual(stale_calls, list(range(1, 9)))
        self.assertEqual(self.scraper.stats['total_paging_early_stops'], 0)

        self._reset_scan_memory()
        self.scraper.stats['total_paging_early_stops'] = 0
        self.scraper._scan_state_set('scan_generation', '1')
        full_calls, full_candidates = self.scan(pages, incremental_full_scan_every=2)
        self.assertEqual(full_calls, list(range(1, 9)))
        self.assertEqual(self.paths(full_candidates)['hot-deep'], '超级好价')
        self.assertEqual(self.scraper.stats['total_paging_early_stops'], 0)

    def test_young_known_pages_do_not_stop_before_the_three_hour_floor(self):
        pages = []
        for index in range(CONFIG['max_pages']):
            article_id = f'peak-{index}'
            self.remember(article_id)
            pages.append([self.raw(article_id, 0.4)])
        calls, candidates = self.scan(pages)
        self.assertEqual(calls, list(range(1, CONFIG['max_pages'] + 1)))
        self.assertEqual(candidates, [])
        self.assertEqual(self.scraper.stats['total_paging_early_stops'], 0)
        self.assertEqual(
            SmzdmScraper.estimate_list_fetch_seconds(CONFIG['max_pages']),
            76.5,
        )

    def test_new_engagement_on_a_known_page_keeps_paging(self):
        pages = self.feed_pages()
        pages[5] = [
            self.raw('k9', 3.8),
            self.raw('just-engaged', 3.85, worthy=1, title='巴布豆 儿童运动鞋'),
        ]
        calls, candidates = self.scan(pages)
        self.assertIn(7, calls)
        self.assertIn('hot-deep', self.paths(candidates))

    def test_price_drop_dedup_decision_is_unchanged(self):
        self.scraper.seen_fingerprints.add('same-product')
        self.scraper.fingerprint_min_prices['same-product'] = 100.0
        dropped = {'id': '1', 'title': '降价商品', 'fingerprint': 'same-product', 'price_value': 90.0}
        unchanged = {'id': '2', 'title': '原价商品', 'fingerprint': 'same-product', 'price_value': 96.0}
        self.assertFalse(self.scraper._is_fingerprint_duplicate(dropped))
        self.assertTrue(self.scraper._is_fingerprint_duplicate(unchanged))
        self.assertIn('较历史', dropped['price_drop_note'])

    def test_safe_mode_records_history_without_posting(self):
        candidate = {
            'id': 'safe-1',
            'title': '小米 智能插座3',
            'fingerprint': 'safe-socket',
            'mall': '京东',
            'price': '19.9元',
            'price_value': 19.9,
            'worthy': 25,
            'unworthy': 0,
            'collection': 15,
            'comments': 30,
            'quality_path': '超级好价',
        }
        with patch.dict(os.environ, {'SMZDM_STATE_RESTORED': '0'}), \
                patch('smzdm.WECOM_WEBHOOK_URL', 'https://example.invalid/wecom-hook'), \
                patch('smzdm.requests.post') as post, \
                patch.object(self.scraper, '_scan_and_filter_stage1', return_value=[candidate]), \
                patch.object(self.scraper, '_search_keyword_deals'), \
                patch.object(self.scraper, '_enrich_candidates_from_channel_apis'), \
                patch.object(self.scraper, '_cleanup'), \
                patch.object(self.scraper, '_print_statistics'), \
                patch.object(self.scraper, '_prioritize_candidates', side_effect=lambda rows: rows), \
                patch.object(self.scraper, '_should_defer_for_comment_unavailable', return_value=False), \
                patch.object(self.scraper, '_is_slow_growth_candidate', return_value=False), \
                patch.object(self.scraper, '_should_wait_for_trend_confirmation', return_value=False), \
                patch.object(self.scraper, '_is_low_growth_candidate', return_value=False), \
                patch.dict(CONFIG, {
                    'jd_self_filter_enabled': False,
                    'comment_level_check_enabled': False,
                    'shill_detection_enabled': False,
                }):
            self.scraper.run()
        post.assert_not_called()
        self.assertEqual(self.scraper.stats['total_sent'], 0)
        self.assertEqual(self.scraper.stats['total_safe_mode_suppressed'], 1)
        row = self.scraper.conn.execute(
            'SELECT id, price_value, send_record_id FROM history'
        ).fetchone()
        self.assertEqual(row[0], 'safe-1')
        self.assertEqual(row[1], 19.9)
        self.assertFalse(row[2])

    def test_safe_mode_run_counts_keyword_and_quality_hits_separately(self):
        # 同一轮冷启动里，热度候选和关键词候选应各计一次跳过，且都不发推送。
        # 关键词搜索必须打桩，避免单测依赖外部列表接口。
        quality = {
            'id': 'safe-1',
            'title': '小米 智能插座3',
            'fingerprint': 'safe-socket',
            'mall': '京东',
            'price': '19.9元',
            'price_value': 19.9,
            'worthy': 25,
            'unworthy': 0,
            'collection': 15,
            'comments': 30,
            'quality_path': '超级好价',
        }
        rule = _keyword_rule('好奇号', 155)
        keyword = {
            'id': 'kw-safe-2',
            'title': '《好奇号》杂志 1年12期',
            'fingerprint': 'haoqihao-safe',
            'mall': '当当',
            'price': '149元',
            'price_value': 149,
            'keyword_rule': rule,
        }

        def fake_search():
            self.scraper.keyword_candidates[keyword['id']] = dict(keyword)

        with patch.dict(os.environ, {'SMZDM_STATE_RESTORED': '0'}), \
                patch('smzdm.WECOM_WEBHOOK_URL', 'https://example.invalid/wecom-hook'), \
                patch('smzdm.requests.post') as post, \
                patch.object(self.scraper, '_scan_and_filter_stage1', return_value=[quality]), \
                patch.object(self.scraper, '_search_keyword_deals', side_effect=fake_search), \
                patch.object(self.scraper, '_enrich_candidates_from_channel_apis'), \
                patch.object(self.scraper, '_cleanup'), \
                patch.object(self.scraper, '_print_statistics'), \
                patch.object(self.scraper, '_prioritize_candidates', side_effect=lambda rows: rows), \
                patch.object(self.scraper, '_should_defer_for_comment_unavailable', return_value=False), \
                patch.object(self.scraper, '_is_slow_growth_candidate', return_value=False), \
                patch.object(self.scraper, '_should_wait_for_trend_confirmation', return_value=False), \
                patch.object(self.scraper, '_is_low_growth_candidate', return_value=False), \
                patch.dict(CONFIG, {
                    'jd_self_filter_enabled': False,
                    'comment_level_check_enabled': False,
                    'shill_detection_enabled': False,
                }):
            self.scraper.run()
        post.assert_not_called()
        self.assertEqual(self.scraper.stats['total_sent'], 0)
        self.assertEqual(self.scraper.stats['total_keyword_sent'], 0)
        self.assertEqual(self.scraper.stats['total_safe_mode_suppressed'], 2)
        rows = self.scraper.conn.execute(
            'SELECT id, price_value, send_record_id FROM history ORDER BY id'
        ).fetchall()
        self.assertEqual([(row[0], row[1], bool(row[2])) for row in rows], [
            ('kw-safe-2', 149, False),
            ('safe-1', 19.9, False),
        ])

    def test_daily_heartbeat_is_once_per_china_day_and_backup_is_optional(self):
        accepted = {'success': True, 'code': 1000, 'data': []}
        morning = datetime(2026, 10, 29, 2, 0, tzinfo=timezone.utc)
        next_day = datetime(2026, 10, 29, 16, 30, tzinfo=timezone.utc)
        with patch('smzdm.requests.post') as post:
            post.return_value.status_code = 200
            post.return_value.json.return_value = accepted
            self.assertTrue(self.scraper.maybe_send_daily_heartbeat(now=morning))
            self.assertFalse(self.scraper.maybe_send_daily_heartbeat(now=morning))
            self.assertEqual(post.call_count, 1)
            summary = post.call_args.kwargs['json']['summary']
            self.assertLessEqual(len(summary), 100)
            self.assertNotIn('AT_private_token', summary)
            self.assertNotIn('UID_test_recipient', summary)
            self.assertIn('未配置', post.call_args.kwargs['json']['content'])
            self.assertTrue(self.scraper.maybe_send_daily_heartbeat(now=next_day))
            self.assertEqual(post.call_count, 2)

    def test_backup_channel_does_not_change_primary_history(self):
        data = {
            'id': 'backup-1', 'title': '九阳 豆浆机 1.2L', 'fingerprint': 'joyoung',
            'mall': '京东', 'price': '99元', 'price_value': 99,
            'worthy': 25, 'unworthy': 0, 'collection': 15, 'comments': 30,
            'quality_path': '超级好价', 'link': 'https://www.smzdm.com/p/backup-1/',
        }
        wecom_ok = {'errcode': 0, 'errmsg': 'ok', 'msgid': '77'}
        wecom_ctx = [
            patch('smzdm.WECOM_CORPID', 'ww_test'),
            patch('smzdm.WECOM_AGENTID', '1000003'),
            patch('smzdm.WECOM_SECRET', 'SECRET_test'),
            patch('smzdm.WECOM_WEBHOOK_URL', ''),
            patch('smzdm.WECOM_TOUSER', '@all'),
        ]
        for item in wecom_ctx:
            item.start()
        self.addCleanup(lambda: [item.stop() for item in reversed(wecom_ctx)])

        with patch('smzdm.requests.get') as get, patch('smzdm.requests.post') as post:
            get.return_value.status_code = 200
            get.return_value.json.return_value = {
                'errcode': 0, 'access_token': 'TOKEN_test', 'expires_in': 7200,
            }
            post.return_value.status_code = 200
            post.return_value.json.return_value = wecom_ok
            with patch.dict(os.environ, {'WXPUSHER_BACKUP_UID': 'UID_test_recipient'}):
                self.assertTrue(self.scraper._send_notification(dict(data)))
            # 备用 UID 与主 WXPusher UID 相同会被忽略，只发企业微信主通道。
            self.assertEqual(post.call_count, 1)

        def side_effect(url, *args, **kwargs):
            response = requests.Response()
            response.status_code = 200
            if 'wxpusher.zjiecode.com' in str(url):
                uid = (kwargs.get('json') or {}).get('uids', [None])[0]
                response.json = lambda: {'success': True, 'code': 1000, 'data': []}
                if uid == 'UID_backup_recipient':
                    response.status_code = 503
            else:
                response.json = lambda: wecom_ok
            return response

        with patch('smzdm.requests.get') as get, patch('smzdm.requests.post', side_effect=side_effect) as post:
            get.return_value.status_code = 200
            get.return_value.json.return_value = {
                'errcode': 0, 'access_token': 'TOKEN_test', 'expires_in': 7200,
            }
            with patch.dict(os.environ, {'WXPUSHER_BACKUP_UID': 'UID_backup_recipient'}):
                fresh = dict(data)
                self.assertTrue(self.scraper._send_notification(fresh))
        self.assertEqual(fresh['push_send_record_id'], '77')
        self.assertEqual(self.scraper.stats['total_backup_push_failed'], 1)
        wx_uids = [
            call.kwargs['json']['uids']
            for call in post.call_args_list
            if 'wxpusher.zjiecode.com' in str(call.args[0] if call.args else call.kwargs.get('url', ''))
        ]
        self.assertEqual(wx_uids, [['UID_backup_recipient']])

    def test_safe_mode_also_suppresses_keyword_alerts(self):
        self.scraper.safe_mode = True
        self.scraper.keyword_rules = [{'keyword': '好奇号', 'terms': ['好奇号'], 'max_price': 155}]
        parsed = {
            'id': 'kw-safe-1',
            'title': '《好奇号》杂志 1年12期',
            'fingerprint': 'haoqihao',
            'mall': '当当',
            'price': '149元',
            'price_value': 149,
            'keyword_rule': self.scraper.keyword_rules[0],
        }
        self.scraper.keyword_candidates = {parsed['id']: parsed}
        with patch('smzdm.requests.post') as post:
            self.scraper._process_keyword_candidates()
        post.assert_not_called()
        self.assertEqual(self.scraper.stats['total_safe_mode_suppressed'], 1)
        self.assertEqual(self.scraper.stats['total_keyword_sent'], 0)
        self.assertEqual(self.scraper.stats['total_sent'], 0)
        row = self.scraper.conn.execute('SELECT id, price_value FROM history').fetchone()
        self.assertEqual(row[0], 'kw-safe-1')
        self.assertEqual(row[1], 149)

    def test_main_logs_the_traceback_and_sends_one_error_alert(self):
        with patch('smzdm.SmzdmScraper') as cls:
            cls.return_value.run.side_effect = RuntimeError('boom')
            cls.return_value.send_error_alert.return_value = True
            with self.assertLogs(level='ERROR') as logs:
                with self.assertRaises(SystemExit) as raised:
                    main()
        self.assertEqual(raised.exception.code, 1)
        text = '\n'.join(logs.output)
        self.assertIn('运行异常', text)
        self.assertIn('RuntimeError', text)
        self.assertIn('Traceback', text)
        cls.return_value.send_error_alert.assert_called_once()
        if os.path.exists(OPS_ALERT_MARKER):
            os.remove(OPS_ALERT_MARKER)

    def test_workflow_failure_alert_is_skipped_after_the_script_already_alerted(self):
        with open(OPS_ALERT_MARKER, 'w', encoding='utf-8') as handle:
            handle.write('1\n')
        self.addCleanup(lambda: os.path.exists(OPS_ALERT_MARKER) and os.remove(OPS_ALERT_MARKER))
        with patch('smzdm.requests.post') as post:
            self.assertFalse(send_workflow_failure_alert('already sent'))
        post.assert_not_called()

    def test_datetimes_are_timezone_aware_utc_without_changing_stored_text(self):
        parsed = parse_utc('2026-10-29T01:02:03')
        self.assertEqual(parsed.tzinfo, timezone.utc)
        self.assertEqual(format_utc_iso(parsed), '2026-10-29T01:02:03')
        self.assertEqual(format_utc_sql(parsed), '2026-10-29 01:02:03')
        aware = datetime(2026, 10, 29, 1, 2, 3, tzinfo=timezone.utc)
        self.assertEqual(
            SmzdmScraper._minutes_between(aware, aware + timedelta(minutes=15)),
            15,
        )
        self.assertEqual(SmzdmScraper._parse_snapshot_time('2026-10-29 01:02:03').tzinfo, timezone.utc)

    def test_unittest_logging_does_not_write_expected_errors_to_stdout(self):
        root = logging.getLogger()
        self.assertGreaterEqual(root.level, logging.ERROR)
        self.assertFalse(any(
            isinstance(handler, logging.StreamHandler)
            and getattr(handler, 'stream', None) is sys.stdout
            for handler in root.handlers
        ))


if __name__ == '__main__':
    unittest.main()
