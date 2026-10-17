import json
import os
import tempfile
import unittest
from contextlib import ExitStack
from unittest.mock import Mock, patch

from smzdm import (
    CONFIG, SmzdmScraper, load_keyword_rules, normalize_keyword_text, parse_keyword_rules,
)


def api_row(article_id, title, price, **extra):
    row = {
        'article_id': article_id,
        'article_channel_type': 'youhui',
        'article_title': title,
        'article_price': price,
        'article_url': f'https://www.smzdm.com/p/{article_id}/',
        'article_pic': f'http://y.zdmimg.com/{article_id}.jpg_d320.jpg',
        'article_mall': '京东',
        'article_format_date': '10:21',
        'publish_date_lt': '0',
        'article_worthy': '0',
        'article_unworthy': '0',
        'article_comment': '0',
        'tongji_hudong': '评论_0,收藏_0,值_0,不值_0',
    }
    row.update(extra)
    return row


class KeywordRuleParsingTests(unittest.TestCase):
    def test_env_spec_parses_thresholds_aliases_and_fullwidth_separators(self):
        rules = parse_keyword_rules('好奇号:155，京东plus会员|京东PLUS年卡：80元, 坏配置, 无价格:abc')
        self.assertEqual([rule['keyword'] for rule in rules], ['好奇号', '京东plus会员'])
        self.assertEqual(rules[0]['max_price'], 155.0)
        self.assertEqual(rules[1]['terms'], ['京东plus会员', '京东PLUS年卡'])
        self.assertEqual(rules[1]['normalized_terms'], ['京东plus会员', '京东plus年卡'])
        self.assertEqual(rules[1]['max_price'], 80.0)

    def test_normalization_ignores_case_spaces_width_and_punctuation(self):
        for text in ('京东PLUS会员', '京东 PLUS 会员', '京东Ｐｌｕｓ会员', '京东·plus·会员'):
            with self.subTest(text=text):
                self.assertIn('京东plus会员', normalize_keyword_text(text))
        self.assertIn('好奇号', normalize_keyword_text('《好奇号》（2026年10月-2027年9月）'))

    def test_env_overrides_file_and_file_supports_list_and_mapping(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, 'keywords.json')
            with open(path, 'w', encoding='utf-8') as handle:
                json.dump({'keywords': [{'keyword': '好奇号', 'max_price': 155,
                                         'aliases': ['好奇号杂志']}]}, handle)
            rules = load_keyword_rules('', path)
            self.assertEqual(rules[0]['terms'], ['好奇号', '好奇号杂志'])
            self.assertEqual(load_keyword_rules('京东plus会员:80', path)[0]['keyword'], '京东plus会员')
            with open(path, 'w', encoding='utf-8') as handle:
                json.dump({'好奇号': 155}, handle)
            self.assertEqual(load_keyword_rules('', path)[0]['max_price'], 155.0)
            with open(path, 'w', encoding='utf-8') as handle:
                handle.write('{broken')
            self.assertEqual(load_keyword_rules('', path), [])
        self.assertEqual(load_keyword_rules('', '/nonexistent/keywords.json'), [])

    def test_repository_keywords_file_is_valid(self):
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'keywords.json')
        rules = load_keyword_rules('', path)
        self.assertEqual({rule['keyword']: rule['max_price'] for rule in rules},
                         {'好奇号': 155.0, '京东plus会员': 80.0})


class KeywordAlertTests(unittest.TestCase):
    def setUp(self):
        self.patches = ExitStack()
        self.addCleanup(self.patches.close)
        self.patches.enter_context(patch('smzdm.DB_PATH', ':memory:'))
        self.patches.enter_context(patch('smzdm.WECOM_CORPID', 'ww_private_corp'))
        self.patches.enter_context(patch('smzdm.WECOM_AGENTID', '1000003'))
        self.patches.enter_context(patch('smzdm.WECOM_SECRET', 'SECRET_private_value'))
        self.patches.enter_context(patch('smzdm.WECOM_WEBHOOK_URL', ''))
        self.patches.enter_context(patch('smzdm.WECOM_TOUSER', '@all'))
        self.patches.enter_context(patch('smzdm.DRY_RUN', False))
        self.patches.enter_context(patch('smzdm.time.sleep'))
        self.scraper = SmzdmScraper()
        self.addCleanup(self.scraper._cleanup)
        self.scraper.keyword_rules = parse_keyword_rules('好奇号:155,京东plus会员|京东plus年卡:80')
        self.sent = []

        def fake_send(data):
            self.sent.append(data)
            data['push_send_record_id'] = f"msg{len(self.sent)}"
            return True
        self.scraper._send_notification = Mock(side_effect=fake_send)

    def observe(self, *rows):
        for row in rows:
            self.scraper._observe_keyword_match(self.scraper._parse_item(row))

    def test_match_and_price_threshold(self):
        self.observe(
            api_row('1', 'PLUS会员、今日必买：《好奇号》（2026年10月-2027年9月、全年共12期）', '152.99元（需用券）'),
            api_row('2', '好奇号杂志订阅 2026年 1年共12期', '268元（需用券）'),
            api_row('3', '好奇 铂金装小桃裤拉拉裤L76片', '68.54元'),  # 只含“好奇”，不命中“好奇号”
            api_row('4', '京东 PLUS 会员 年卡 12个月', '79元'),
            api_row('5', '京东PLUS会员 领专属十周年拾光皮肤', ''),  # 无价格
            api_row('6', '芒果TV 会员12个月年卡+京东Plus年卡', '108元（需用券）'),
            api_row('7', '京东 PLUS京典年卡 京东年卡1年12个月', '99元'),  # 不含关键词或别名
            api_row('8', '京东plus年卡 12个月', '80元'),  # 别名命中，价格等于阈值
        )
        self.assertEqual(sorted(self.scraper.keyword_candidates), ['1', '2', '4', '5', '6', '8'])
        self.scraper._process_keyword_candidates()
        self.assertEqual(sorted(row['id'] for row in self.sent), ['1', '4', '8'])
        self.assertEqual(self.scraper.stats['total_keyword_over_price'], 2)
        self.assertEqual(self.scraper.stats['total_keyword_no_price'], 1)
        self.assertEqual(self.scraper.stats['total_keyword_sent'], 3)
        sent_ids = {row[0] for row in self.scraper.conn.execute('SELECT id FROM history')}
        self.assertEqual(sent_ids, {'1', '4', '8'})
        self.assertEqual(self.scraper.keyword_match_counts, {'好奇号': 2, '京东plus会员': 4})

    def test_keyword_path_bypasses_quality_filters_without_engagement(self):
        self.scraper._filter_stage1 = Mock(side_effect=AssertionError('keyword path must not use stage1'))
        self.scraper._check_comment_user_levels = Mock(side_effect=AssertionError('no comment check'))
        self.observe(api_row('11', '《好奇号》杂志 1年12期', '149元'))
        self.scraper._process_keyword_candidates()
        self.assertEqual([row['id'] for row in self.sent], ['11'])
        self.assertEqual(self.sent[0]['quality_path'], '关键词:好奇号')

    def test_history_dedupes_across_runs_and_inactive_or_old_items_are_skipped(self):
        self.scraper.conn.execute("INSERT INTO history (id, title) VALUES ('21', '旧推送')")
        self.observe(
            api_row('21', '《好奇号》杂志 1年12期', '149元'),
            api_row('22', '《好奇号》杂志 1年12期 售罄', '149元', article_is_sold_out='1'),
            api_row('23', '《好奇号》杂志 1年12期 已过期', '149元', article_is_timeout='1'),
        )
        old = self.scraper._parse_item(api_row('24', '《好奇号》杂志 2025', '99元'))
        old['age_hours'] = CONFIG['keyword_max_age_hours'] + 1
        self.scraper._observe_keyword_match(old)
        self.scraper._process_keyword_candidates()
        self.assertEqual(self.sent, [])
        self.assertEqual(self.scraper.stats['total_keyword_duplicates'], 1)
        self.assertEqual(self.scraper.stats['total_keyword_inactive'], 2)

    def test_same_article_seen_twice_is_pushed_once(self):
        row = api_row('31', '《好奇号》杂志 1年12期', '149元')
        self.observe(row, row)
        self.scraper._process_keyword_candidates()
        self.scraper._process_keyword_candidates()
        self.assertEqual(len(self.sent), 1)

    def test_same_keyword_same_price_reposts_push_once_per_run(self):
        self.observe(
            api_row('35', '好奇号杂志订阅 2026年 1年共12期', '149元'),
            api_row('36', '杂志铺 好奇号杂志 2026年全年订阅 中英双语', '149元'),
            api_row('37', '《好奇号》 2026 全年', '139元'),
        )
        self.scraper._process_keyword_candidates()
        self.assertEqual(sorted(row['id'] for row in self.sent), ['35', '37'])

    def test_hot_candidate_in_same_run_does_not_block_keyword_push(self):
        parsed = self.scraper._parse_item(api_row('41', '《好奇号》杂志 1年12期', '149元'))
        self.scraper._observe_keyword_match(parsed)
        self.scraper.seen_ids.add('41')  # 热度候选也会进入 seen_ids，但尚未推送
        self.scraper._process_keyword_candidates()
        self.assertEqual([row['id'] for row in self.sent], ['41'])

    def test_search_queries_each_term_with_keyword_param_and_age_limit(self):
        response = Mock(status_code=200)
        response.json.return_value = {'error_code': '0', 'data': {'rows': [
            api_row('51', '《好奇号》杂志 1年12期', '149元'),
            api_row('52', '好奇 心钻装 拉拉裤', '99元'),
            api_row('53', '好奇号原创文章', '', article_channel_type='yuanchuang'),
        ]}}
        with patch.object(self.scraper.session, 'get', return_value=response) as get:
            self.scraper._search_keyword_deals()
        terms = [call.kwargs['params']['keyword'] for call in get.call_args_list]
        self.assertEqual(terms, ['好奇号', '京东plus会员', '京东plus年卡'])
        self.assertEqual(get.call_args.args[0], 'https://api.smzdm.com/v1/list')
        self.assertEqual(sorted(self.scraper.keyword_candidates), ['51'])
        self.assertEqual(self.scraper.stats['total_keyword_searches'], 3)

    def test_search_failure_and_budget_degrade_without_raising(self):
        with patch.dict(CONFIG, {'max_keyword_searches_per_run': 1}), patch.object(
                self.scraper.session, 'get', side_effect=RuntimeError('boom')) as get:
            self.scraper._search_keyword_deals()
        self.assertEqual(get.call_count, 1)
        self.assertEqual(self.scraper.stats['total_keyword_search_failed'], 1)

    def test_run_pushes_keyword_deal_and_hot_loop_skips_it(self):
        hot = self.scraper._parse_item(api_row('61', '《好奇号》杂志 1年12期', '149元'))
        hot.update({'quality_path': '升温好价', 'worthy': 10, 'comments': 6, 'collection': 8})

        def fake_scan():
            self.scraper._observe_keyword_match(hot)
            return [hot]
        with ExitStack() as mocks:
            mocks.enter_context(patch.dict(CONFIG, {
                'comment_level_check_enabled': False, 'shill_detection_enabled': False}))
            mocks.enter_context(patch.object(self.scraper, '_scan_and_filter_stage1', side_effect=fake_scan))
            mocks.enter_context(patch.object(self.scraper, '_search_keyword_deals'))
            mocks.enter_context(patch.object(self.scraper, '_enrich_candidates_from_channel_apis'))
            mocks.enter_context(patch.object(self.scraper, '_cleanup'))
            mocks.enter_context(patch.object(self.scraper, '_print_statistics'))
            for method in ('_should_defer_for_comment_unavailable', '_is_slow_growth_candidate',
                           '_should_wait_for_trend_confirmation', '_is_low_growth_candidate'):
                mocks.enter_context(patch.object(self.scraper, method, return_value=False))
            self.scraper.run()
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.sent[0]['quality_path'], '关键词:好奇号')
        self.assertEqual(self.scraper.stats['total_sent'], 1)


class WecomPayloadTests(unittest.TestCase):
    def setUp(self):
        self.patches = ExitStack()
        self.addCleanup(self.patches.close)
        self.patches.enter_context(patch('smzdm.DB_PATH', ':memory:'))
        self.patches.enter_context(patch('smzdm.WECOM_AGENTID', '1000003'))
        self.patches.enter_context(patch('smzdm.WECOM_TOUSER', '@all'))
        self.scraper = SmzdmScraper()
        self.addCleanup(self.scraper._cleanup)
        self.scraper.keyword_rules = parse_keyword_rules('好奇号:155')

    def test_keyword_payload_has_marked_title_link_and_picture(self):
        data = self.scraper._parse_item(api_row(
            '184101178', 'PLUS会员、今日必买：《好奇号》（2026年10月-2027年9月、全年共12期）', '149元（需用券）'))
        self.scraper._observe_keyword_match(data)
        data = self.scraper.keyword_candidates['184101178']
        payload = self.scraper._build_wecom_payload(data)
        self.assertEqual(payload['msgtype'], 'news')
        self.assertEqual(payload['agentid'], 1000003)
        self.assertEqual(payload['touser'], '@all')
        article = payload['news']['articles'][0]
        self.assertTrue(article['title'].startswith('[关键词:好奇号] ¥149 '))
        self.assertIn('需用券', article['description'])
        self.assertIn('阈值≤155元', article['description'])
        self.assertIn('未经热度筛选', article['description'])
        self.assertEqual(article['url'], 'https://www.smzdm.com/p/184101178/')
        self.assertEqual(article['picurl'], 'http://y.zdmimg.com/184101178.jpg_d320.jpg')

    def test_hot_payload_respects_wecom_byte_limits(self):
        data = {
            'id': '184000001', 'title': '超长商品名' * 80, 'price': '1431.74元（需用券）',
            'price_value': 1431.74, 'mall': '京东' * 100, 'pub_time': '10:00',
            'worthy': 10, 'unworthy': 0, 'comments': 6, 'collection': 8,
            'quality_path': '均衡热度', 'link': 'https://www.smzdm.com/p/184000001/',
        }
        article = self.scraper._build_wecom_payload(data)['news']['articles'][0]
        self.assertLessEqual(len(article['title'].encode('utf-8')), 128)
        self.assertLessEqual(len(article['description'].encode('utf-8')), 512)
        self.assertTrue(article['title'].startswith('1431.74元'))
        self.assertTrue(article['title'].endswith('…'))
        self.assertTrue(article['description'].startswith(self.scraper._build_notification_summary(data)))
        self.assertNotIn('picurl', article)

    def test_invalid_link_falls_back_to_article_page_and_bad_pic_is_dropped(self):
        data = {'id': '184000002', 'title': '测试', 'price': '9.9元', 'price_value': 9.9,
                'link': 'javascript:alert(1)', 'pic': 'jd://image'}
        article = self.scraper._build_wecom_payload(data)['news']['articles'][0]
        self.assertEqual(article['url'], 'https://www.smzdm.com/p/184000002/')
        self.assertNotIn('picurl', article)

    def test_utf8_clip_never_splits_characters(self):
        clipped = SmzdmScraper._clip_utf8('好' * 100, 128)
        self.assertLessEqual(len(clipped.encode('utf-8')), 128)
        clipped.encode('utf-8').decode('utf-8')
        self.assertEqual(SmzdmScraper._clip_utf8('short', 128), 'short')


if __name__ == '__main__':
    unittest.main()
