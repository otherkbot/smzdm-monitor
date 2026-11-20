import unittest
from datetime import timedelta
from unittest.mock import patch

from smzdm import SmzdmScraper, format_utc_iso, utc_now


class ArticleTrendAndTitleTests(unittest.TestCase):
    def setUp(self):
        self.scraper = SmzdmScraper.__new__(SmzdmScraper)
        with patch("smzdm.DB_PATH", ":memory:"):
            self.scraper._init_database()
        self.addCleanup(self.scraper.conn.close)
        self.scraper.stats = {
            "total_trend_candidates": 0,
            "total_candidate_snapshots_saved": 0,
        }
        self.scraper.quality_path_pass_counts = {}
        self.scraper.snapshot_keys_seen_this_run = set()
        self.scraper.pending_snapshot_writes = 0

    def article(self, article_id="article_B", **overrides):
        title = "测试品牌 固定型号 500g 产品装"
        parsed = {
            "id": article_id,
            "title": title,
            "fingerprint": self.scraper._build_title_fingerprint(title),
            "product_key": "183:123456",
            "mall": "京东",
            "price": "19.9元",
            "comments": 5,
            "collection": 6,
            "worthy": 5,
            "unworthy": 0,
        }
        parsed.update(overrides)
        return parsed

    def insert_snapshot(self, parsed, minutes_ago=30, item_key=None):
        captured_at = format_utc_iso(utc_now() - timedelta(minutes=minutes_ago))
        self.scraper.conn.execute(
            """
            INSERT INTO candidate_snapshots (
                item_key, article_id, captured_at, comments, collection, worthy, unworthy
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                item_key or self.scraper._candidate_snapshot_key(parsed),
                parsed["id"],
                captured_at,
                parsed["comments"],
                parsed["collection"],
                parsed["worthy"],
                parsed["unworthy"],
            ),
        )
        return captured_at

    def test_actions_product_titles_with_store_suffix_are_allowed(self):
        # These 15 titles were incorrectly rejected in the 100-run log review.
        titles = [
            "云南昭通新鲜天麻小草坝乌天麻正宗天麻正品中药材正品官方旗舰店",
            "周黑鸭 锁鲜鸭脖鸭锁骨鸭翅鸭货卤味解馋零食官方旗舰店",
            "德芙 巧克力散装结婚喜糖4.5g婚庆糖果伴手礼零食官方旗舰店",
            "汉斯格雅 水龙头暗装面盆龙头入墙式面盆出水冷热水龙头官方旗舰店",
            "汪氏 蜂王浆冻干粉片4.0正品蜂皇浆便携官方旗舰店",
            "海尔 空调3匹AI舒适风Pro家用一级能效柜机E2旗舰店",
            "海尔 空调小2匹挂机壁挂式新一级能效变频客厅家用官方旗舰店",
            "移动端：芙清 修复乳医用促愈功能性敷料乳液面霜换季官方旗舰店",
            "移动端：蔓迪 泡沫剂二代米诺地尔男女士防脱生发液官方旗舰店",
            "立邦 水性金属漆防盗门入户门改色阳台栏杆翻新油漆官方旗舰店",
            "立邦 聚脲美缝剂室内外卫生间瓷砖防霉防水勾缝胶彩砂官方旗舰店",
            "美体小铺 茶树精油祛痘淡化痘印面部绿茶精油旗舰店",
            "舒肤佳 沐浴露柠檬清爽清香型男女士沐浴乳液旗舰店",
            "诺布琪 水杨酸果酸精华液身体护理精华保湿水改善粗糙官方旗舰店",
            "野生川麦冬无硫麦门冬茶黄芪干货正品旗舰店",
        ]
        for title in titles:
            with self.subTest(title=title):
                self.assertFalse(
                    self.scraper._is_title_blocked({"title": title, "mall": "京东"})
                )

    def test_confirmed_store_only_titles_remain_blocked(self):
        for title in (
            "养生堂京东自营旗舰店",
            "元气森林京东自营旗舰店",
            "周六福官方旗舰店",
            "京东 养生堂京东自营旗舰店",
        ):
            with self.subTest(title=title):
                self.assertTrue(
                    self.scraper._is_title_blocked({"title": title, "mall": "京东"})
                )

    def test_known_store_names_do_not_block_product_descriptions(self):
        for title in (
            "养生堂 维生素C咀嚼片90片 京东自营旗舰店",
            "养生堂维生素C咀嚼片90片京东自营旗舰店",
            "元气森林 气泡水480ml*12瓶京东自营旗舰店",
            "元气森林气泡水京东自营旗舰店",
            "周六福 足金项链4.5g官方旗舰店",
            "维生素C 养生堂京东自营旗舰店",
        ):
            with self.subTest(title=title):
                self.assertFalse(
                    self.scraper._is_title_blocked({"title": title, "mall": "京东"})
                )

    def test_store_reward_tasks_still_block_with_decorated_digits(self):
        for title in (
            "海尔官方旗舰店 入会５０京豆",
            "元气森林京东自营旗舰店 加购 如图",
            "周六福官方旗舰店 签⑤",
            "养生堂京东自营旗舰店 有10豆",
        ):
            with self.subTest(title=title):
                self.assertTrue(
                    self.scraper._is_title_blocked({"title": title, "mall": "京东"})
                )

    def test_different_article_same_fingerprint_cannot_borrow_growth(self):
        previous = self.article("article_A", comments=1, collection=4, worthy=4)
        self.insert_snapshot(previous, item_key="fp:" + previous["fingerprint"])
        current = self.article(product_key="")

        self.scraper._attach_trend_metrics(current)

        trend = current["trend_metrics"]
        self.assertEqual(trend["article_id"], "article_B")
        self.assertEqual(trend["snapshot_count"], 0)
        self.assertEqual(trend["growth_score"], 0)
        self.assertEqual(trend["delta_worthy"], 0)
        self.assertFalse(self.scraper._filter_stage1(current))

    def test_different_article_same_sku_cannot_borrow_growth(self):
        previous = self.article("article_A", comments=1, collection=4, worthy=4)
        self.insert_snapshot(previous, item_key="sku:" + previous["product_key"])
        current = self.article()

        self.scraper._attach_trend_metrics(current)

        self.assertEqual(current["trend_metrics"]["snapshot_count"], 0)
        self.assertFalse(self.scraper._filter_stage1(current))

    def test_same_article_old_fingerprint_history_keeps_true_warming_growth(self):
        previous = self.article(comments=1, collection=4, worthy=4)
        captured_at = self.insert_snapshot(previous, item_key="fp:" + previous["fingerprint"])
        current = self.article()

        self.scraper._attach_trend_metrics(current)

        trend = current["trend_metrics"]
        self.assertEqual(trend["snapshot_count"], 1)
        self.assertEqual(trend["item_key"], "id:article_B")
        self.assertEqual(trend["article_id"], "article_B")
        self.assertEqual(trend["first_captured_at"], captured_at)
        self.assertEqual(trend["previous_captured_at"], captured_at)
        self.assertEqual(trend["growth_score"], 23)
        self.assertEqual(trend["delta_worthy"], 1)
        self.assertTrue(self.scraper._filter_stage1(current))
        self.assertEqual(current["quality_path"], "升温好价")

    def test_same_article_history_survives_sku_enrichment_and_new_keys(self):
        previous = self.article(comments=1, collection=4, worthy=4)
        first_at = self.insert_snapshot(previous, minutes_ago=60, item_key="sku:183:old")
        second = self.article(comments=3, collection=5, worthy=4)
        previous_at = self.insert_snapshot(second, minutes_ago=30)
        unrelated = self.article("article_A", comments=0, collection=0, worthy=0)
        self.insert_snapshot(unrelated, minutes_ago=90, item_key="sku:183:old")
        current = self.article(product_key="183:new", fingerprint="changed_title")

        self.scraper._attach_trend_metrics(current)

        trend = current["trend_metrics"]
        self.assertEqual(trend["snapshot_count"], 2)
        self.assertEqual(trend["first_captured_at"], first_at)
        self.assertEqual(trend["previous_captured_at"], previous_at)
        self.assertEqual(trend["growth_score"], 23)
        self.assertEqual(trend["recent_growth_score"], 13)
        self.assertTrue(self.scraper._filter_stage1(current))
        self.assertEqual(current["quality_path"], "升温好价")

    def test_two_articles_same_sku_each_save_one_snapshot_per_run(self):
        first = self.article("article_A")
        second = self.article("article_B")

        self.scraper._save_candidate_snapshot(first)
        self.scraper._save_candidate_snapshot(second)
        # Later channel enrichment must not create a second observation in this run.
        self.scraper._save_candidate_snapshot(self.article("article_A", product_key="183:new"))
        self.scraper._commit_candidate_snapshots()

        rows = self.scraper.conn.execute(
            "SELECT item_key, article_id, product_key, fingerprint FROM candidate_snapshots ORDER BY id"
        ).fetchall()
        self.assertEqual([row[:2] for row in rows], [("id:article_A", "article_A"), ("id:article_B", "article_B")])
        self.assertEqual(rows[0][2:], rows[1][2:])
        self.assertEqual(self.scraper.stats["total_candidate_snapshots_saved"], 2)

    def test_missing_article_id_cannot_seed_product_level_trend(self):
        parsed = self.article(None)

        self.scraper._save_candidate_snapshot(parsed)
        self.scraper._attach_trend_metrics(parsed)

        self.assertEqual(self.scraper.conn.execute("SELECT COUNT(*) FROM candidate_snapshots").fetchone()[0], 0)
        self.assertEqual(parsed["trend_metrics"]["snapshot_count"], 0)


if __name__ == "__main__":
    unittest.main()
