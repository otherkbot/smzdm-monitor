# -*- coding: utf-8 -*-
"""SMZDM 好价监控推送脚本 - GitHub Actions 单次执行模式"""

import os
import sys
import io
import sqlite3
import requests
import time
import random
import logging
import re
import html
import json
import hashlib
import unicodedata
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

# ===== 配置 =====
# 企业微信自建应用推送（图文 news 消息，点击直接打开 SMZDM 原文）
WECOM_CORPID = os.environ.get("WECOM_CORPID", "").strip()
WECOM_AGENTID = os.environ.get("WECOM_AGENTID", "").strip()
WECOM_SECRET = os.environ.get("WECOM_SECRET", "").strip()
WECOM_TOUSER = os.environ.get("WECOM_TOUSER", "").strip() or "@all"
WECOM_API_BASE = "https://qyapi.weixin.qq.com/cgi-bin"
# 企业微信群机器人（消息推送）Webhook；配置后优先走群机器人，不再需要 corpid/secret。
WECOM_WEBHOOK_URL = os.environ.get("WECOM_WEBHOOK_URL", "").strip()
# 仅记录将要发送的载荷，不请求企业微信、不写入推送历史
DRY_RUN = os.environ.get("DRY_RUN", "").strip().lower() in ("1", "true", "yes", "on")
# 关键词低价提醒：环境变量优先，其次仓库内 keywords.json
SMZDM_KEYWORDS = os.environ.get("SMZDM_KEYWORDS", "")
KEYWORDS_FILE = os.environ.get(
    "SMZDM_KEYWORDS_FILE",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "keywords.json"),
)
DB_PATH = os.environ.get("SMZDM_DB_PATH", "smzdm.db")
SMZDM_DETAIL_SIGN_KEY = "apr1$AwP!wRRT$gJ/q.X24poeBInlUJC"

CONFIG = {
    # 扫描参数
    "max_pages": 51,                    # 接口当前最多提供 5100 条；高峰期 4000 条仅覆盖约 1.5 小时
    "max_history_hours": 6,             # 最多扫描过去多少小时内的数据
    "whitelist_channel_types": ["faxian", "youhui"],  # 只保留好价相关频道
    "items_per_page": 100,              # 实测接口上限为 100，减少实时分页漂移和请求次数
    "max_consecutive_page_failures": 3, # 主列表连续失败时结束本轮，交给下一次定时任务重试
    "rank_source_enabled": True,       # 总榜补充会重新浮出超过 6 小时但近期升温的商品
    "rank_source_url": "https://m.smzdm.com/sou/category_rank",
    "rank_source_hours": [1, 3, 12],   # 配合 15 分钟触发轮换短期、升温和成熟榜单
    "rank_source_slot_seconds": 900,
    "rank_source_limit": 50,           # 接口单次最多返回 50 条，仍只请求一次总榜
    "late_recheck_enabled": True,       # 主列表消失后，用按文章 ID 的详情 JSON 补查审核延迟的互动
    "late_recheck_url": "https://haojia-api.smzdm.com/detail/{article_id}",
    "late_recheck_min_per_run": 4,
    "late_recheck_candidate_ratio": 0.04,
    "max_late_rechecks_per_run": 16,    # 积压时动态扩容，仍保留防反爬硬上限
    "late_recheck_interval_minutes": 45,
    "late_recheck_max_age_hours": 18,
    "late_recheck_min_worthy": 4,
    "late_recheck_min_collection": 4,
    "late_recheck_min_comments": 8,
    "detail_api_version": "10.4.26",
    "detail_api_revision": "866",
    "max_consecutive_detail_failures": 2,
    "push_keyword_cooldown_minutes": 45,  # 旧 WXPusher 关键词过滤冷却；企业微信无此拒绝语义，保留表结构但不再写入
    "wecom_title_max_bytes": 128,       # 企业微信 news 标题上限
    "wecom_description_max_bytes": 512, # 企业微信 news 描述上限
    "wecom_url_max_bytes": 2048,
    "wecom_token_error_codes": [40001, 40014, 42001],  # 令牌无效/过期时刷新一次
    "wecom_webhook_min_interval": 3.2,  # 群机器人每分钟最多 20 条，两次发送至少间隔 3.2 秒
    "wecom_webhook_rate_limit_codes": [45009],  # 超频：等待后只重试一次
    "wecom_webhook_rate_limit_wait": 60,
    "keyword_search_enabled": True,     # 每轮按关键词主动查询主列表接口的 keyword 参数
    "keyword_search_limit": 50,
    "max_keyword_searches_per_run": 10, # 关键词及别名的搜索请求硬上限
    "keyword_max_age_hours": 12,        # 关键词提醒只推近期爆料，避免首次运行推送大量旧帖

    # 第一阶段：综合评分筛选
    "score_weights": {
        "comments": 3,                  # 评论体现讨论热度，不能证明最难刷或已购买
        "collection": 2,                # 收藏权重中等
        "worthy": 1,                    # 值票是社区支持信号，不等于实际购买
    },
    "min_total_engagement": 25,         # 均衡路径需要成熟、分布较完整的互动
    "min_composite_score": 65,
    "min_score_rate": 90,
    "min_balanced_comments": 6,
    "min_balanced_collection": 5,
    "min_balanced_worthy": 8,
    "min_score_rate_relaxed": 85,       # 高讨论路径仍要求较高好评率
    "min_signal_worthy": 2,              # 至少有值票或评论信号，避免纯收藏/纯券活动
    "min_signal_comments": 2,
    "discussion_min_comments": 12,
    "discussion_min_collection": 5,
    "discussion_min_worthy": 8,
    "discussion_min_total_engagement": 30,
    "discussion_min_composite_score": 90,
    "emerging_min_worthy": 4,            # 早期好价必须有评论、收藏和值票三类信号
    "emerging_min_comments": 6,
    "emerging_min_collection": 4,
    "emerging_min_total_engagement": 16,
    "emerging_min_composite_score": 35,
    "emerging_min_score_rate": 95,
    "early_signal_min_worthy": 6,        # 评论审核延迟时，要求更强的收藏/值票组合
    "early_signal_min_collection": 8,
    "early_signal_min_total_engagement": 16,
    "early_signal_min_composite_score": 28,
    "early_signal_min_score_rate": 95,
    "single_unworthy_tolerance_min_worthy": 6,  # 小样本只出现 1 个不值时，不让 95% 门槛一票否决
    "single_unworthy_tolerance_max_unworthy": 1,
    "single_unworthy_tolerance_min_score_rate": 85,
    "super_deal_min_comments": 20,       # 超级好价：高值率、高评论、高收藏和值票
    "super_deal_min_worthy": 20,
    "super_deal_min_collection": 10,
    "super_deal_min_composite_score": 120,
    "super_deal_min_score_rate": 90,
    "warming_min_growth_score": 10,      # 升温好价：依赖多轮扫描中的真实增长
    "warming_min_worthy": 4,
    "warming_min_collection": 4,
    "warming_min_composite_score": 32,
    "warming_min_total_engagement": 15,
    "warming_min_score_rate": 95,
    "warming_recent_min_growth_score": 8,
    "warming_recent_max_minutes": 120,
    "warming_cumulative_max_minutes": 180,
    "warming_min_growth_per_hour": 6,
    "warming_min_signal_growth_score": 5,  # 升温必须包含值票/收藏增长，不能只靠评论
    "warming_min_worthy_growth": 1,       # 收藏增长可刷，至少还要出现真实值票增长
    "excluded_status_keywords": [
        "售罄",
        "过期",
        "已结束",
    ],

    # 第二阶段：京东自营校验
    # GitHub Actions 访问京东商品页经常只拿到无商品字段的云端空壳页；
    # 默认关闭自营强过滤，京东商品与其他平台一样走评论等级/集中度/互动异常水军判断。
    "jd_self_filter_enabled": False,
    "jd_link_lookup_pages": 12,          # 当前列表接口不带 article_link，按频道接口最多回查页数
    "jd_link_lookup_page_size": 50,
    "jd_reject_when_unverified": True,   # 京东商品无法确认自营时拒绝，避免放过非自营
    "jd_self_check_min_per_run": 5,       # 京东候选少时的基础校验量
    "jd_self_check_candidate_ratio": 0.3, # 京东候选多时按比例增加校验量
    "max_jd_self_checks_per_run": 12,     # 硬上限，控制 go.smzdm/jd 外部链路请求量
    "jd_self_title_keywords": [           # 京东页面在云端不可用时，仅对确定的京东自营体系做兜底
        "京东自营",
        "自营旗舰店",
        "1号会员店",
        "京东京造",
        "京东超市",
    ],

    # 第三阶段：评论等级特征与集中度检查（haojia.m.smzdm.com 真实 JSON 的 vip_level）
    "comment_level_check_enabled": True,
    "comment_level_low_max": 5,          # Lv5 及以下为低等级特征，不等于虚假账号
    "comment_level_high_min": 6,         # Lv6 及以上为高等级特征，不等于真实购买认证
    "comment_level_min_comments": 3,     # 列表达到此值才请求样本，独立于等级否决证据门槛
    "comment_level_min_known_samples": 6, # 等级比例硬拒前至少有 6 条已知等级的非作者样本
    "comment_level_min_known_ratio": 0.8, # 已知等级至少覆盖 80% 非作者样本
    "comment_level_max_low_ratio": 0.35, # 证据充足时，低等级占已知等级超过 35% 则过滤
    "comment_concentration_min_comments": 4,  # 评论样本达到此数量才检查集中度
    "comment_concentration_min_users": 3,     # 独立评论用户过少则可疑
    "comment_concentration_max_user_ratio": 0.5, # 单个用户评论占比过高则可疑
    "comment_module_min_coverage_ratio": 0.8, # 评论模块返回样本覆盖列表评论数约 80% 才代表整体评论区
    "comment_module_coverage_tolerance": 2,   # 或返回评论数与列表评论数差距不超过 2 条
    "comment_module_undercovered_skip_min_comments": 10, # 总评论已较多但模块只给热评时跳过等级判断
    "large_thread_representative_min_samples": 5, # 大评论区热评样本少时不把样本当完整代表
    "comment_level_check_min_per_run": 8,
    "comment_level_check_candidate_ratio": 1.0, # 常规情况下校验全部可校验候选，避免预算导致半小时级延迟
    "max_comment_level_checks_per_run": 80,     # 保留硬上限，避免候选异常暴增时连续撞外部接口
    "defer_comment_unavailable": False,          # 评论等级不可用只做诊断，不再阻断互动/趋势评分通过的商品
    "defer_emerging_when_comment_unavailable": True,
    "pending_review_fallback_runs": 2,
    "pending_review_keep_days": 2,
    "fallback_allowed_reasons": ["sample", "external"],
    "partial_sample_min_samples": 2,     # 少样本放行：至少取到 2 条评论
    "partial_sample_min_unique_users": 2,
    "partial_sample_min_score_rate": 85,
    "partial_sample_min_comments": 10,
    "partial_sample_min_score": 80,
    "large_thread_min_total": 50,        # 模块声明评论很多但热评样本偏少时的受限放行
    "large_thread_min_samples": 2,
    "large_thread_min_unique_users": 2,
    "large_thread_min_score_rate": 95,
    "large_thread_min_comments": 15,
    "large_thread_min_score": 50,
    "large_thread_max_low_samples": 1,
    "large_thread_min_high_samples": 1,
    "fallback_min_score_rate": 85,       # 样本长期不可用兜底：只放行成熟高信号商品
    "fallback_min_comments": 15,
    "fallback_min_score": 90,
    "budget_strong_pass_min_score": 120,
    "budget_strong_pass_min_comments": 20,

    # 第四阶段：互动比例异常观察，不能仅靠缺少不值票/评论认定水军
    "shill_detection_enabled": True,
    "shill_min_votes_for_check": 30,        # 总投票数少于此值时跳过水军检测
    "shill_max_worthy_unworthy_ratio": 20,  # 值/不值比超过此值则可疑指标+1
    "shill_min_comment_worthy_ratio": 0.2,  # 评论数/值票数低于此值则可疑指标+1
    "shill_min_flags": 2,                   # 至少两个比例异常才记录，仍不是账号身份判断
    # 复用已有评论请求的正文和时间；未经标注校准，只观察，不新增硬拒绝。
    "comment_anomaly_enabled": True,
    "comment_anomaly_min_samples": 6,
    "comment_anomaly_min_text_length": 24,
    "comment_anomaly_min_duplicate_users": 4,
    "comment_anomaly_min_duplicate_ratio": 0.6,
    "comment_anomaly_burst_seconds": 180,
    "trend_stale_filter_enabled": True,      # 初始有票但多轮不增长时降噪
    "trend_stale_min_age_minutes": 60,
    "trend_stale_max_growth_score": 3,
    "trend_stale_max_comments": 5,
    "trend_confirmation_enabled": True,       # 低信心早期商品先等一轮趋势确认
    "trend_confirmation_paths": ["早期好价", "早期强信号"],
    "trend_confirmation_min_comments": 10,
    "trend_confirmation_min_score": 65,
    "trend_confirmation_min_worthy": 6,
    "trend_confirmation_min_collection": 8,
    "trend_confirmation_discussion_min_comments": 20,
    "trend_confirmation_discussion_min_score": 100,
    "trend_confirmation_discussion_min_worthy": 4,
    "trend_confirmation_discussion_min_collection": 8,
    "trend_confirmation_min_growth_score": 8,
    "trend_confirmation_min_signal_growth_score": 5,
    "trend_confirmation_min_worthy_growth": 1,
    "trend_confirmation_min_growth_per_hour": 6,
    "trend_low_growth_filter_enabled": True,  # 有历史快照但增长很慢时过滤
    "trend_low_growth_min_age_minutes": 25,
    "trend_low_growth_min_growth_score": 6,
    "trend_low_growth_min_recent_score": 4,
    "trend_low_growth_max_comments": 8,
    "trend_low_growth_exempt_paths": ["超级好价", "高讨论", "升温好价"],
    "trend_slow_filter_enabled": True,       # 长时间窗口里慢慢涨一点，不再视为升温
    "trend_slow_min_age_minutes": 240,
    "trend_slow_max_growth_per_hour": 2,
    "trend_slow_max_recent_score": 3,
    "trend_slow_max_comments": 8,
    "trend_slow_exempt_paths": ["超级好价", "高讨论"],
    "trend_display_recent_after_minutes": 240,

    # 标题正则过滤：补齐 WXPusher 只能关键词屏蔽、无法表达“入会%京豆”的限制
    "title_block_patterns": [
        r"(?:关注|入会|加入(?:店铺)?会员|签到|打卡|抽奖|大转盘|竞猜|瓜分|逛店|浏览任务).{0,40}(?:京豆|豆(?![\u4e00-\u9fff])|逗(?![\u4e00-\u9fff])|[dD](?![a-z])|红包|优惠券|积分|现金奖励|实测|大概率|必中)",
        r"(?:评论|晒单|发布评论).{0,16}(?:有奖|赢|奖励|现金)",
        r"(?:会员)?积分.{0,16}(?:兑换|换购).{0,16}(?:实物|礼品|商品)",
        r"(?:店铺|旗舰店|直播间?|会员).{0,20}(?:领|抽|得|送|返|奖励|福利).{0,20}(?:京豆|豆(?![\u4e00-\u9fff])|逗(?![\u4e00-\u9fff])|红包|优惠券|积分|[dD](?![a-z]))",
        r"(?:^|\s)(?:店铺|直播间?).{0,8}(?:领|抽|得|送|返).{0,4}\d+\s*$",
        r"(?:店铺|直播间?).{0,20}\d+\s*(?:京豆|豆(?![\u4e00-\u9fff])|逗(?![\u4e00-\u9fff]))",
        r"(?:领|送|返|抽|(?:^|[\s:：，,、元])得).{0,20}(?:\d+|[零一二两三四五六七八九十百]+).{0,6}(?:京豆|豆(?![\u4e00-\u9fff\d])|逗(?![\u4e00-\u9fff\d]))",
        r"入会.{0,40}京豆",
        r"入会.{0,30}\d+\s*(?:京豆|豆(?![\u4e00-\u9fff]))",
        r"关注.{0,30}\d+\s*(?:京豆|豆(?![\u4e00-\u9fff]))",
        r"关注.{0,20}(?:领|领取|得).{0,20}京豆",
        r"签到.{0,30}京豆",
        r"签到.{0,40}(?:有奖|可领|领券|券|超市卡)",
        r"(?:签到|抽奖|大转盘|实测).{0,30}\d+\s*(?:京豆|豆(?![\u4e00-\u9fff]))",
        r"(?:领|送|返|抽|(?:^|[\s:：，,、元])得).{0,30}\d+\s*京豆",
        r"(?:竞猜|瓜分).{0,30}\d+\s*万?\s*京豆",
        r"\d+\s*万?\s*京豆",
        # 店铺名后只有操作、奖励或含糊数量，不是可购买商品标题。
        r"(?:旗舰店|专营店|专卖店|店铺).{0,24}(?:加购(?:\s*如图)?|如图)\s*$",
        r"(?:旗舰店|专营店|专卖店|店铺).{0,24}共计.{0,12}(?:个?豆|京豆)",
        r"(?:旗舰店|专营店|专卖店|店铺).{0,12}有\s*(?:\d[\d\s]*|[零一二两三四五六七八九十百]+)\s*$",
        r"(?:^|\s)来来来.{0,12}(?:一百|100)的?\s*$",
        # 店铺运营任务不一定写明“京豆”，但标题结尾只有动作或奖励数值。
        r"积分.{0,12}(?:兑|兑换).{0,12}(?:\d[\d\s]*|[零一二两三四五六七八九十百千万]+)\s*(?:京豆|豆(?![\u4e00-\u9fff]))?\s*$",
        r"(?:旗舰店|专营店|专卖店|店铺|店铺会员).{0,24}(?:入会|关注|加购).{0,8}(?:\d[\d\s]*|[零一二两三四五六七八九十百千万]+)\s*(?:京豆|豆(?![\u4e00-\u9fff]))?\s*$",
        r"店铺活动.{0,40}(?:关注|加购|抽奖|抽好礼|领)",
        r"京自.{0,8}(?:加|领(?:京豆)?)\s*$",
        r"(?:京东自营|京自|旗舰店|专营店|专卖店|店铺|专区).{0,12}领京豆\s*$",
        r"(?:旗舰店|专营店|专卖店|店铺).{0,12}\d+\s*元?\s*(?:红包|优惠券)\s*$",
        r"(?:官旗|旗舰店).{0,12}加\s*g[o0]\s*l[o0]\s*$",
        # 实际日志中的中文数字、专区、经营部和反向奖励语序变体。
        r"加购.{0,24}(?:\d[\d\s]*|[零一二两三四五六七八九十百千万]+)\s*[）)]?\s*个?\s*(?:京豆|豆(?![\u4e00-\u9fff]))\s*$",
        r"(?:旗舰店|专营店|专卖店|店铺|专区|经营部|京东自营).{0,20}(?:有|共计|一共|共|等级礼包).{0,12}(?:\d[\d\s]*|[零一二两三四五六七八九十百千万]+)\s*(?:京豆|个?豆(?![\u4e00-\u9fff]))\s*$",
        r"(?:\d[\d\s]*|[零一二两三四五六七八九十百千万]+)\s*元?\s*e卡.{0,8}抽奖\s*$",
        r"进入.{0,8}弹窗.{0,20}(?:领|领取)",
        r"必看促销[:：].{0,50}\d+\s*个?.{0,8}品牌参与",
        r"(?:分享|玩游戏).{0,30}得.{0,20}(?:\d[\d\s]*|[零一二两三四五六七八九十百千万]+)\s*(?:京豆|豆(?![\u4e00-\u9fff]))",
        r"(?:真(?:五|5)折|促销|特卖).{0,30}专区\s*$",
        # 近期实际漏网：互动很高的店铺任务、会场和抽奖福袋仍不是商品。
        r"加购.{0,12}(?:需|须)入会\s*[）)]?\s*$",
        r"弹窗.{0,12}可领.{0,20}(?:红包|优惠券|超市卡|随机金额)",
        r"积分.{0,12}(?:兑|兑换).{0,12}京豆",
        r"(?:官旗|旗舰店).{0,12}签\s*(?:\d+|[零一二两三四五六七八九十百千万]+)\s*$",
        r"入会\s*(?:\d[\d\s]*|[零一二两三四五六七八九十百千万]+)\s*$",
        r"(?:礼品店|旗舰店|店铺).{0,12}关\s*(?:\d[\d\s]*|[零一二两三四五六七八九十百千万]+)\s*豆",
        r"(?:官旗|旗舰店).{0,12}每日\s*(?:\d[\d\s]*|[零一二两三四五六七八九十百千万]+)\s*(?:京豆|豆|逗)\s*$",
        r"福袋.{0,30}(?:活动|抽奖).{0,20}(?:非必中|概率)",
        r"关注有礼.{0,12}(?:\d[\d\s]*|[零一二两三四五六七八九十百千万]+)\s*[）)]?\s*个?\s*豆子?",
        r"深夜游乐场",
        # 2026-08-01 Actions 实际漏网：标题只有店铺任务、奖励或直播会场。
        r"(?:旗舰店|自营店|京东自营).{0,20}入会.{0,8}抽奖\s*$",
        r"(?:^|[\s，,、:：|])(?:\d+月)?等级礼包.{0,12}(?:\d+|[零一二两三四五六七八九十百千万]+)\s*豆\s*$",
        r"\d+\s*天.{0,20}(?:抢|领|得)?\s*(?:\d+|[零一二两三四五六七八九十百千万]+)\s*(?:京豆|豆|元?\s*[eE]卡)",
        r"积分.{0,12}(?:兑换|兑|换).{0,8}(?:京豆|豆)\s*$",
        r"(?:手机馆|会员页|圈子).{0,12}京豆\s*$",
        r"(?:^|京东\s+)出发.{0,8}(?:\d+|[零一二两三四五六七八九十百千万]+)\s*豆",
        r"京东直播.{0,30}直播间\s*$",
        r"(?:今日好券|好券速领|领\d+元支付券|整点抢\d+元.{0,8}券)",
        r"京东.{0,24}(?:有|送|领)(?:\d+|[零一二两三四五六七八九十百千万]+)\s*豆\s*$",
        # 仅拦已确认只有店铺名称的标题；旗舰店后缀不能证明前面没有商品。
        r"^(?:(?:京东|天猫(?:超市)?|淘宝)\s+){0,2}(?:养生堂|元气森林|周六福)(?:官方)?(?:京东自营)?旗舰店\s*$",
    ],

    # 去重参数
    "fingerprint_dedupe_days": 30,      # 同商品 30 天内不重复；真实降价仍可再次推送
    "fingerprint_min_len": 8,
    "price_drop_min_percent": 5,        # 同商品降价超过 5% 允许再次推送
    "price_drop_min_amount": 5,         # 或至少便宜 5 元允许再次推送

    # 候选快照和趋势评分
    "snapshot_keep_days": 2,
    "snapshot_min_collection": 2,
    "trend_weights": {
        "worthy": 3,
        "collection": 2,
        "comments": 4,
        "unworthy": -2,
    },

    # 请求参数
    "request_delay": (0.5, 1.5),        # 随机延迟范围（秒）
    "external_request_delay": (1.5, 3.5), # 详情/跳转类请求更慢，降低反爬风险
    "timeout": (8, 20),                # 连接/读取超时；失败尽快交给下一轮扫描重试
    "max_consecutive_comment_failures": 2, # 评论接口连续失败时熔断本轮外部校验
    "waf_status_codes": [202, 403, 429],
    "waf_markers": ["probe.js", "tcaptcha", "验证码", "captcha", "访问过于频繁"],
}

# ===== 日志 =====
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[logging.StreamHandler(stream=sys.stdout)]
)


def normalize_keyword_text(value):
    """关键词匹配归一化：全半角统一、忽略大小写、空格和标点（如 京东 PLUS 会员 / 《好奇号》）。"""
    text = unicodedata.normalize('NFKC', html.unescape(str(value or ''))).lower()
    text = re.sub(r'<[^>]*>', '', text)
    return re.sub(r'[\W_]+', '', text)


def _keyword_rule(keyword, max_price, aliases=()):
    terms = []
    for term in [keyword, *aliases]:
        term = str(term or '').strip()
        if term and normalize_keyword_text(term) and term not in terms:
            terms.append(term)
    if not terms:
        return None
    try:
        price = float(max_price)
    except (TypeError, ValueError):
        return None
    if price <= 0:
        return None
    return {
        'keyword': terms[0],
        'terms': terms,
        'normalized_terms': [normalize_keyword_text(term) for term in terms],
        'max_price': price,
    }


def parse_keyword_rules(spec):
    """解析 `好奇号:155,京东plus会员|京东plus年卡:80`；`|` 后为同一规则的别名。"""
    rules = []
    text = unicodedata.normalize('NFKC', str(spec or ''))
    for entry in re.split(r'[,;\n]+', text):
        entry = entry.strip()
        if not entry or ':' not in entry:
            if entry:
                logging.warning(f"关键词配置缺少价格上限，已忽略: {entry[:40]}")
            continue
        names, _, price = entry.rpartition(':')
        terms = [name.strip() for name in names.split('|')]
        rule = _keyword_rule(terms[0], price.strip().rstrip('元'), terms[1:])
        if rule:
            rules.append(rule)
        else:
            logging.warning(f"关键词配置无效，已忽略: {entry[:40]}")
    return rules


def load_keyword_rules(env_spec=None, file_path=None):
    """环境变量 SMZDM_KEYWORDS 非空时优先；否则读取 keywords.json。"""
    env_spec = SMZDM_KEYWORDS if env_spec is None else env_spec
    if str(env_spec or '').strip():
        return parse_keyword_rules(env_spec)
    file_path = KEYWORDS_FILE if file_path is None else file_path
    if not file_path or not os.path.exists(file_path):
        return []
    try:
        with open(file_path, encoding='utf-8') as handle:
            payload = json.load(handle)
    except (OSError, ValueError) as e:
        logging.warning(f"关键词文件读取失败，跳过关键词提醒: {type(e).__name__}")
        return []
    if isinstance(payload, dict):
        payload = payload.get('keywords', payload)
    rules = []
    if isinstance(payload, dict):
        for keyword, price in payload.items():
            rule = _keyword_rule(keyword, price)
            if rule:
                rules.append(rule)
    elif isinstance(payload, list):
        for item in payload:
            if not isinstance(item, dict):
                continue
            aliases = item.get('aliases') or []
            if not isinstance(aliases, list):
                aliases = []
            rule = _keyword_rule(item.get('keyword'), item.get('max_price'), aliases)
            if rule:
                rules.append(rule)
    return rules


class SmzdmScraper:
    def __init__(self):
        self.session = requests.Session()
        self._init_session()
        self._init_database()
        self.seen_ids = set()
        self.seen_fingerprints = set()
        self.seen_product_keys = set()
        self.fingerprint_min_prices = {}
        self.product_key_min_prices = {}
        self._load_existing_ids()

        self.stats = {
            'total_fetched': 0,
            'total_rank_fetched': 0,
            'total_rank_merged': 0,
            'total_rank_candidates': 0,
            'total_rank_unavailable': 0,
            'total_rank_duplicates': 0,
            'total_rank_filtered_title': 0,
            'total_rank_filtered_stage1': 0,
            'total_late_recheck_selected': 0,
            'total_late_recheck_eligible': 0,
            'total_late_recheck_attempted': 0,
            'total_late_recheck_fetched': 0,
            'total_late_recheck_candidates': 0,
            'total_late_recheck_unavailable': 0,
            'total_late_recheck_filtered_stage1': 0,
            'total_sent': 0,
            'total_push_failed': 0,
            'total_push_unknown': 0,
            'total_push_keyword_filtered': 0,
            'total_push_keyword_cooldown_skipped': 0,
            'total_push_dry_run': 0,
            'total_keyword_searches': 0,
            'total_keyword_search_failed': 0,
            'total_keyword_matched': 0,
            'total_keyword_over_price': 0,
            'total_keyword_no_price': 0,
            'total_keyword_duplicates': 0,
            'total_keyword_inactive': 0,
            'total_keyword_sent': 0,
            'total_duplicates': 0,
            'total_fingerprint_duplicates': 0,
            'total_product_key_duplicates': 0,
            'total_channel_metadata_enriched': 0,
            'total_comment_count_upgraded': 0,
            'total_candidate_snapshots_saved': 0,
            'total_trend_candidates': 0,
            'total_filtered_trend_stale': 0,
            'total_filtered_trend_low_growth': 0,
            'total_filtered_trend_slow': 0,
            'total_deferred_trend_confirmation': 0,
            'total_filtered_title_pattern': 0,
            'total_filtered_stage1': 0,
            'total_page_request_failures': 0,
            'total_main_scan_aborted': 0,
            'total_filtered_jd_self': 0,
            'total_filtered_comment_level': 0,
            'total_filtered_shill': 0,
            'total_comment_level_unavailable': 0,
            'total_comment_level_unavailable_budget': 0,
            'total_comment_level_unavailable_sample': 0,
            'total_comment_level_unavailable_low_comments': 0,
            'total_comment_level_unavailable_external': 0,
            'total_comment_level_deferred': 0,
            'total_comment_level_fallback_allowed': 0,
            'total_comment_level_large_thread_allowed': 0,
            'total_comment_level_undercovered_skipped': 0,
            'total_comment_level_insufficient_evidence': 0,
            'total_comment_anomaly_observed': 0,
            'total_comment_anomaly_combined': 0,
            'total_engagement_anomaly_observed': 0,
            'total_comment_level_budget_strong_allowed': 0,
            'total_external_checks_suspended': 0,
        }
        self.article_link_cache = {}
        self.channel_article_link_cache = {}
        self.channel_article_data_cache = {}
        self.article_data_cache = {}
        self.channel_link_pages_loaded = {}
        self.channel_link_exhausted = set()
        self.jd_self_checks = 0
        self.jd_self_check_limit = CONFIG['jd_self_check_min_per_run']
        self.jd_fetch_debug = []
        self.jd_page_checks_unavailable = False
        self.quality_path_pass_counts = {}
        self.quality_path_sent_counts = {}
        self.comment_level_checks = 0
        self.comment_level_check_limit = CONFIG['comment_level_check_min_per_run']
        self.external_checks_suspended = False
        self.consecutive_comment_request_failures = 0
        self.consecutive_detail_request_failures = 0
        self.late_recheck_eligible_count = 0
        self.snapshot_keys_seen_this_run = set()
        self.pending_snapshot_writes = 0
        self.keyword_rules = load_keyword_rules()
        self.keyword_candidates = {}
        self.keyword_match_counts = {}
        self.keyword_pushed_ids = set()
        self.wecom_access_token = ''
        self.wecom_webhook_last_send = None

    def _init_session(self):
        retry = Retry(
            total=2,
            connect=2,
            read=1,
            status=2,
            backoff_factor=1,
            status_forcelist=[500, 502, 503, 504],
            allowed_methods=frozenset({'GET'}),
            raise_on_status=False,
            # 429 必须立即交给本轮熔断，避免 Retry-After 将单次任务挂起数小时。
            respect_retry_after_header=False,
        )
        adapter = HTTPAdapter(max_retries=retry, pool_connections=20)
        self.session.mount('https://', adapter)
        self.session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/139.0.0.0 Safari/537.36',
            'Accept': 'application/json, text/plain, */*',
            'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8',
            'Accept-Encoding': 'gzip, deflate, br',
            'Connection': 'keep-alive',
            'Referer': 'https://www.smzdm.com/',
        })

    def _init_database(self):
        self.conn = sqlite3.connect(DB_PATH)
        self.conn.execute('''
            CREATE TABLE IF NOT EXISTS history (
                id TEXT PRIMARY KEY,
                title TEXT,
                fingerprint TEXT,
                product_key TEXT,
                mall TEXT,
                price TEXT,
                price_value REAL,
                send_record_id TEXT,
                last_seen DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        self._ensure_history_columns()
        self.conn.execute('CREATE INDEX IF NOT EXISTS idx_history_fingerprint ON history (fingerprint, last_seen)')
        self.conn.execute('CREATE INDEX IF NOT EXISTS idx_history_product_key ON history (product_key, last_seen)')
        self.conn.execute('''
            CREATE TABLE IF NOT EXISTS pending_reviews (
                review_key TEXT PRIMARY KEY,
                article_id TEXT,
                title TEXT,
                fingerprint TEXT,
                mall TEXT,
                unavailable_count INTEGER DEFAULT 0,
                first_seen DATETIME DEFAULT CURRENT_TIMESTAMP,
                last_seen DATETIME DEFAULT CURRENT_TIMESTAMP,
                last_quality_path TEXT,
                last_reason TEXT
            )
        ''')
        self.conn.execute('''
            CREATE TABLE IF NOT EXISTS candidate_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                item_key TEXT NOT NULL,
                article_id TEXT,
                title TEXT,
                fingerprint TEXT,
                product_key TEXT,
                mall TEXT,
                price TEXT,
                price_value REAL,
                comments INTEGER,
                collection INTEGER,
                worthy INTEGER,
                unworthy INTEGER,
                score_rate REAL,
                composite_score INTEGER,
                age_hours REAL,
                quality_path TEXT,
                captured_at TEXT NOT NULL
            )
        ''')
        self.conn.execute(
            'CREATE INDEX IF NOT EXISTS idx_candidate_snapshots_key_time '
            'ON candidate_snapshots (item_key, captured_at)'
        )
        self.conn.execute(
            'CREATE INDEX IF NOT EXISTS idx_candidate_snapshots_article_time '
            'ON candidate_snapshots (article_id, captured_at)'
        )
        self.conn.execute('''
            CREATE TABLE IF NOT EXISTS late_recheck_state (
                article_id TEXT PRIMARY KEY,
                last_checked TEXT NOT NULL,
                retired INTEGER DEFAULT 0
            )
        ''')
        self.conn.execute('''
            CREATE TABLE IF NOT EXISTS push_keyword_cooldowns (
                recipient_key TEXT NOT NULL,
                article_id TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                PRIMARY KEY (recipient_key, article_id)
            )
        ''')
        self.conn.execute(
            'CREATE INDEX IF NOT EXISTS idx_push_keyword_cooldowns_expiry '
            'ON push_keyword_cooldowns (expires_at)'
        )
        self.conn.commit()

    def _ensure_history_columns(self):
        cursor = self.conn.execute("PRAGMA table_info(history)")
        existing_columns = {row[1] for row in cursor.fetchall()}
        required_columns = {
            'fingerprint': 'TEXT',
            'product_key': 'TEXT',
            'mall': 'TEXT',
            'price': 'TEXT',
            'price_value': 'REAL',
            'send_record_id': 'TEXT',
        }
        for column, column_type in required_columns.items():
            if column not in existing_columns:
                self.conn.execute(f'ALTER TABLE history ADD COLUMN {column} {column_type}')
        self._backfill_history_price_values()

    def _backfill_history_price_values(self):
        cursor = self.conn.execute(
            "SELECT id, price, price_value FROM history WHERE price IS NOT NULL AND price != ''"
        )
        rows = cursor.fetchall()
        for article_id, price, old_value in rows:
            price_value = self._parse_price_value(price)
            # 同时修复旧版本误把返还/券面额存成售价的记录，而非只补 NULL。
            if price_value != old_value:
                self.conn.execute(
                    "UPDATE history SET price_value = ? WHERE id = ?",
                    (price_value, article_id)
                )

    def _load_existing_ids(self):
        cursor = self.conn.execute("SELECT id FROM history")
        self.seen_ids = {row[0] for row in cursor.fetchall()}

        cutoff = datetime.now() - timedelta(days=CONFIG['fingerprint_dedupe_days'])
        cursor = self.conn.execute(
            '''
            SELECT fingerprint, MIN(price_value)
            FROM history
            WHERE fingerprint IS NOT NULL AND fingerprint != '' AND last_seen >= ?
            GROUP BY fingerprint
            ''',
            (cutoff,)
        )
        for fingerprint, min_price in cursor.fetchall():
            self.seen_fingerprints.add(fingerprint)
            if min_price is not None:
                self.fingerprint_min_prices[fingerprint] = float(min_price)

        cursor = self.conn.execute(
            '''
            SELECT product_key, MIN(price_value)
            FROM history
            WHERE product_key IS NOT NULL AND product_key != '' AND last_seen >= ?
            GROUP BY product_key
            ''',
            (cutoff,)
        )
        for product_key, min_price in cursor.fetchall():
            self.seen_product_keys.add(product_key)
            if min_price is not None:
                self.product_key_min_prices[product_key] = float(min_price)
        logging.info(
            f"已加载 {len(self.seen_ids)} 条历史记录，"
            f"{len(self.seen_fingerprints)} 个近期商品指纹，"
            f"{len(self.seen_product_keys)} 个近期商品 SKU"
        )

    # ==================== 扫描 ====================

    def run(self):
        """单次执行入口：扫描 -> 三层筛选 -> 推送 -> 退出"""
        if WECOM_WEBHOOK_URL:
            logging.info(f"推送通道：企业微信群机器人 Webhook（{self._mask_webhook_url(WECOM_WEBHOOK_URL)}）")
        elif not DRY_RUN and not (WECOM_CORPID and WECOM_AGENTID and WECOM_SECRET):
            logging.error("缺少 WECOM_WEBHOOK_URL，或 WECOM_CORPID、WECOM_AGENTID、WECOM_SECRET 环境变量")
            sys.exit(1)
        else:
            logging.info("推送通道：企业微信自建应用消息")
        if DRY_RUN:
            logging.info("DRY_RUN 已开启：只记录推送载荷，不请求企业微信，也不写入推送历史")

        logging.info("开始扫描...")
        self._clean_old_records()
        if self.keyword_rules:
            logging.info("关键词低价提醒: " + ", ".join(
                f"{'|'.join(rule['terms'])}≤{rule['max_price']:g}元" for rule in self.keyword_rules
            ))

        # 第一轮：API 扫描 + 综合评分筛选，收集候选商品
        candidates = self._scan_and_filter_stage1()
        # 关键词低价提醒：主列表命中 + 按关键词主动查询；绕过热度筛选，仍按历史去重。
        self._search_keyword_deals()
        self._process_keyword_candidates()
        self._enrich_candidates_from_channel_apis(candidates)
        candidates = self._prioritize_candidates(candidates)
        self.jd_self_check_limit = self._calculate_jd_self_check_limit(candidates)
        self.comment_level_check_limit = self._calculate_comment_level_check_limit(candidates)

        logging.info(f"综合评分筛选后候选商品: {len(candidates)} 条")
        if CONFIG['jd_self_filter_enabled']:
            logging.info(f"京东自营动态校验预算: {self.jd_self_check_limit} 次")
        else:
            logging.info("京东自营过滤: 关闭，京东商品按通用水军策略判断")
        logging.info(f"评论等级动态校验预算: {self.comment_level_check_limit} 次")

        # 第二轮：可选京东自营、评论等级、互动异常检测
        for parsed in candidates:
            if parsed.get('id') in self.keyword_pushed_ids:
                continue
            if self._skip_push_keyword_cooldown(parsed):
                continue
            if CONFIG['jd_self_filter_enabled'] and not self._check_jd_self_operated(parsed):
                self.stats['total_filtered_jd_self'] += 1
                continue

            if CONFIG['comment_level_check_enabled'] and not self._check_comment_user_levels(parsed):
                self.stats['total_filtered_comment_level'] += 1
                self._clear_pending_review(parsed)
                continue

            if self._should_defer_for_comment_unavailable(parsed):
                if parsed.get('comment_level_unavailable_reason') != 'budget':
                    self._save_pending_review(parsed)
                self.stats['total_comment_level_deferred'] += 1
                continue

            if self._is_slow_growth_candidate(parsed):
                self.stats['total_filtered_trend_slow'] += 1
                trend = parsed.get('trend_metrics') or {}
                logging.warning(
                    f"[增长过慢过滤] {parsed['title'][:40]}... | {self._candidate_log_context(parsed)} {self._trend_log_context(parsed)} "
                    f"路径:{parsed.get('quality_path')} 已观察:{trend.get('elapsed_minutes', 0)}分钟 "
                    f"累计增长:{trend.get('growth_score', 0)} "
                    f"近期增长:{trend.get('recent_growth_score', 0)} "
                    f"速率:{trend.get('growth_per_hour', 0)}/h "
                    f"评论:{parsed.get('comments', 0)} 收藏:{parsed.get('collection', 0)} "
                    f"值:{parsed.get('worthy', 0)}"
                )
                continue

            if self._should_wait_for_trend_confirmation(parsed):
                self.stats['total_deferred_trend_confirmation'] += 1
                trend = parsed.get('trend_metrics') or {}
                logging.info(
                    f"[等待趋势确认] {parsed['title'][:40]}... | {self._candidate_log_context(parsed)} {self._trend_log_context(parsed)} "
                    f"路径:{parsed.get('quality_path')} 评分:{parsed.get('composite_score', 0)} "
                    f"评论:{parsed.get('comments', 0)} 收藏:{parsed.get('collection', 0)} "
                    f"值:{parsed.get('worthy', 0)} 快照:{trend.get('snapshot_count', 0)} "
                    f"互动增长:{trend.get('recent_growth_score', 0)} "
                    f"值增长:{trend.get('recent_delta_worthy', 0)} "
                    f"收藏增长:{trend.get('recent_delta_collection', 0)}"
                )
                continue

            if self._is_low_growth_candidate(parsed):
                self.stats['total_filtered_trend_low_growth'] += 1
                trend = parsed.get('trend_metrics') or {}
                logging.warning(
                    f"[增长过低过滤] {parsed['title'][:40]}... | {self._candidate_log_context(parsed)} {self._trend_log_context(parsed)} "
                    f"路径:{parsed.get('quality_path')} 已观察:{trend.get('elapsed_minutes', 0)}分钟 "
                    f"增长分:{trend.get('growth_score', 0)} "
                    f"近期增长:{trend.get('recent_growth_score', 0)} "
                    f"评论:{parsed.get('comments', 0)} 收藏:{parsed.get('collection', 0)} "
                    f"值:{parsed.get('worthy', 0)}"
                )
                continue

            if CONFIG['shill_detection_enabled'] and not self._check_shill(parsed):
                self.stats['total_filtered_shill'] += 1
                continue

            if self._is_send_duplicate(parsed):
                self.stats['total_duplicates'] += 1
                continue

            # 推送
            if self._send_notification(parsed):
                # 只有推送成功才写入历史；未达标或推送失败的商品下次运行会重新按最新互动数据评估。
                self._save_history(parsed)
                self._clear_pending_review(parsed)
                self.stats['total_sent'] += 1
                path = parsed.get('quality_path', '未知')
                self.quality_path_sent_counts[path] = self.quality_path_sent_counts.get(path, 0) + 1

        self._print_statistics()
        self._cleanup()

    def _scan_and_filter_stage1(self):
        """扫描 API 并通过综合评分筛选候选商品"""
        candidates = []
        stop_scanning = False
        rank_rows = self._fetch_rank_rows()
        rank_by_id = {
            str(row.get('article_id', '')).strip(): (position, row)
            for position, row in enumerate(rank_rows, start=1)
            if str(row.get('article_id', '')).strip()
        }
        processed_ids = set()
        consecutive_page_failures = 0

        for page in range(1, CONFIG['max_pages'] + 1):
            if stop_scanning:
                break

            items = self._fetch_page(page)
            if items is None:
                consecutive_page_failures += 1
                self.stats['total_page_request_failures'] += 1
                if consecutive_page_failures >= CONFIG['max_consecutive_page_failures']:
                    self.stats['total_main_scan_aborted'] += 1
                    logging.error(
                        f"主列表连续 {consecutive_page_failures} 页请求失败，结束本轮扫描，"
                        "等待下一次定时任务重试"
                    )
                    break
                continue
            consecutive_page_failures = 0
            if not items:
                logging.info(f"第{page}页无数据，停止扫描")
                break

            self.stats['total_fetched'] += len(items)

            for item in items:
                parsed = self._parse_item(item)
                if not parsed:
                    continue

                if parsed.get('age_hours', 0) > CONFIG['max_history_hours']:
                    logging.info(f"扫到 {parsed['age_hours']:.1f} 小时前的数据，达到时间上限 {CONFIG['max_history_hours']} 小时，停止扫描。")
                    stop_scanning = True
                    break

                processed_ids.add(parsed['id'])
                self._observe_keyword_match(parsed)
                rank_entry = rank_by_id.get(parsed['id'])
                if rank_entry:
                    self._apply_rank_metrics(parsed, rank_entry[1], rank_entry[0])
                    self.stats['total_rank_merged'] += 1
                self._consider_stage1_candidate(parsed, candidates)

            self._commit_candidate_snapshots()
            time.sleep(random.uniform(*CONFIG['request_delay']))

        discovered_ids = set(processed_ids)
        for position, row in enumerate(rank_rows, start=1):
            article_id = str(row.get('article_id', '')).strip()
            if article_id:
                discovered_ids.add(article_id)
            if not article_id or article_id in processed_ids:
                continue
            parsed = self._parse_rank_item(row, position)
            if parsed:
                self._observe_keyword_match(parsed)
                self._consider_stage1_candidate(parsed, candidates)

        self._append_late_recheck_candidates(discovered_ids, candidates)
        self._commit_candidate_snapshots()
        return candidates

    def _consider_stage1_candidate(self, parsed, candidates):
        if self._is_duplicate(parsed):
            self.stats['total_duplicates'] += 1
            if parsed.get('rank_source'):
                self.stats['total_rank_duplicates'] += 1
                logging.info(
                    f"[排行榜跳过:重复 #{parsed.get('rank_position')}] "
                    f"{parsed['title'][:40]}..."
                )
            return

        if self._is_title_blocked(parsed):
            self.stats['total_filtered_title_pattern'] += 1
            if parsed.get('rank_source'):
                self.stats['total_rank_filtered_title'] += 1
            return

        self._attach_trend_metrics(parsed)
        self._save_candidate_snapshot(parsed)
        # 保留列表观察，短期避免再次为接收人已明确屏蔽的同一文章请求评论或发送。
        if self._skip_push_keyword_cooldown(parsed):
            return

        if not self._filter_stage1(parsed):
            self.stats['total_filtered_stage1'] += 1
            if parsed.get('late_recheck_source'):
                self.stats['total_late_recheck_filtered_stage1'] += 1
            if parsed.get('rank_source'):
                self.stats['total_rank_filtered_stage1'] += 1
                metrics = self._current_interaction_metrics(parsed)
                logging.info(
                    f"[排行榜跳过:评分 #{parsed.get('rank_position')}] "
                    f"{parsed['title'][:40]}... | {self._candidate_log_context(parsed)} "
                    f"评分:{metrics['composite_score']} 值票支持率:{metrics['score_rate']}% "
                    f"评论:{parsed.get('comments', 0)} 收藏:{parsed.get('collection', 0)} "
                    f"值:{parsed.get('worthy', 0)} 不值:{parsed.get('unworthy', 0)}"
                )
            return

        candidates.append(parsed)
        self.seen_ids.add(parsed['id'])
        if parsed.get('late_recheck_source'):
            self.stats['total_late_recheck_candidates'] += 1
            logging.info(
                f"[延迟复查候选] {parsed['title'][:40]}... | {self._candidate_log_context(parsed)} "
                f"评论:{parsed.get('comments', 0)} 收藏:{parsed.get('collection', 0)} "
                f"值:{parsed.get('worthy', 0)} 不值:{parsed.get('unworthy', 0)}"
            )
        if parsed.get('rank_source'):
            self.stats['total_rank_candidates'] += 1
            logging.info(
                f"[排行榜候选 #{parsed.get('rank_position')}] {parsed['title'][:40]}... | {self._candidate_log_context(parsed)} "
                f"评论:{parsed.get('comments', 0)} 收藏:{parsed.get('collection', 0)} "
                f"值:{parsed.get('worthy', 0)} 不值:{parsed.get('unworthy', 0)}"
            )

    def _fetch_rank_rows(self):
        if not CONFIG.get('rank_source_enabled'):
            return []
        rank_hour = self._select_rank_source_hour()
        try:
            response = self.session.get(
                CONFIG['rank_source_url'],
                params={
                    'page': 1,
                    'limit': CONFIG['rank_source_limit'],
                    'hour': rank_hour,
                },
                headers={
                    'Accept': 'application/json, text/plain, */*',
                    'Referer': 'https://m.smzdm.com/top/',
                },
                timeout=CONFIG['timeout'],
            )
            if response.status_code != 200:
                self.stats['total_rank_unavailable'] += 1
                logging.warning(f"排行榜补充不可用: HTTP {response.status_code}，继续主列表扫描")
                return []
            payload = response.json()
            data = payload.get('data') if isinstance(payload, dict) else None
            rows = data.get('rows') if isinstance(data, dict) else None
            if str(payload.get('error_code')) != '0' or not isinstance(rows, list):
                self.stats['total_rank_unavailable'] += 1
                logging.warning("排行榜补充返回格式异常，继续主列表扫描")
                return []
            rows = [
                {**row, '_rank_window_hours': rank_hour}
                for row in rows[:CONFIG['rank_source_limit']]
                if isinstance(row, dict)
            ]
            self.stats['total_rank_fetched'] += len(rows)
            logging.info(f"排行榜补充获取: {len(rows)} 条（{rank_hour}小时总榜）")
            return rows
        except Exception as e:
            self.stats['total_rank_unavailable'] += 1
            logging.warning(f"排行榜补充请求异常: {e}，继续主列表扫描")
            return []

    @staticmethod
    def _select_rank_source_hour(now_ts=None):
        hours = CONFIG.get('rank_source_hours') or [12]
        slot_seconds = max(1, int(CONFIG.get('rank_source_slot_seconds') or 1800))
        slot = int((time.time() if now_ts is None else now_ts) // slot_seconds)
        return int(hours[slot % len(hours)])

    def _append_late_recheck_candidates(self, discovered_ids, candidates):
        if not CONFIG.get('late_recheck_enabled') or self.external_checks_suspended:
            return

        max_limit = max(0, int(CONFIG.get('max_late_rechecks_per_run', 0)))
        if max_limit <= 0:
            return

        # 多取一批纯数据库候选，先剔除任务标题，再用有效商品补足网络预算。
        rows = self._select_late_recheck_rows(discovered_ids, max_limit * 3)
        eligible_count = self.late_recheck_eligible_count
        limit = self._calculate_late_recheck_limit(eligible_count)
        selected_rows = []
        for row in rows:
            article_id = str(row.get('article_id') or '').strip()
            if self._is_title_blocked({
                'title': str(row.get('title') or ''),
                'mall': '',
            }):
                self.stats['total_filtered_title_pattern'] += 1
                self._mark_late_recheck(article_id, retired=True)
                logging.info(f"[延迟复查退出] article_id:{article_id} 标题已过滤")
                continue
            selected_rows.append(row)
            if len(selected_rows) >= limit:
                break
        rows = selected_rows
        self.stats['total_late_recheck_eligible'] = eligible_count
        self.stats['total_late_recheck_selected'] += len(rows)
        logging.info(
            f"延迟复查动态预算: {len(rows)}/{limit} 条 "
            f"（当前符合队列 {eligible_count} 条，硬上限 {max_limit}）"
        )
        for row in rows:
            if self.external_checks_suspended:
                break
            article_id = str(row.get('article_id') or '').strip()
            logging.info(
                f"[延迟复查请求] id:{article_id} "
                f"旧评分:{row.get('composite_score', 0)} "
                f"评论:{row.get('comments', 0)} 收藏:{row.get('collection', 0)} "
                f"值:{row.get('worthy', 0)} | {str(row.get('title') or '')[:40]}..."
            )
            self._mark_late_recheck(article_id)
            detail = self._fetch_late_recheck_detail(article_id)
            if not detail:
                continue
            parsed = self._parse_late_recheck_detail(detail)
            if not parsed:
                continue
            if self._is_inactive_deal(parsed):
                self._mark_late_recheck(article_id, retired=True)
                logging.info(f"[延迟复查退出] article_id:{article_id} 已失效")
                continue
            self._consider_stage1_candidate(parsed, candidates)

    def _select_late_recheck_rows(self, discovered_ids, limit):
        interval_minutes = max(1, int(CONFIG.get('late_recheck_interval_minutes', 45)))
        max_age_hours = max(1, float(CONFIG.get('late_recheck_max_age_hours', 18)))
        cutoff = datetime.utcnow() - timedelta(minutes=interval_minutes)
        pool_limit = max(limit * 5, limit)
        cursor = self.conn.execute(
            '''
            WITH latest AS (
                SELECT article_id, MAX(captured_at) AS captured_at
                FROM candidate_snapshots
                WHERE article_id IS NOT NULL AND article_id != ''
                GROUP BY article_id
            )
            SELECT snapshots.article_id, snapshots.title, snapshots.worthy,
                   snapshots.collection, snapshots.comments,
                   snapshots.composite_score, snapshots.age_hours,
                   snapshots.captured_at, COUNT(*) OVER() AS eligible_count
            FROM latest
            JOIN candidate_snapshots AS snapshots
              ON snapshots.article_id = latest.article_id
             AND snapshots.captured_at = latest.captured_at
            LEFT JOIN history ON history.id = snapshots.article_id
            LEFT JOIN late_recheck_state AS recheck
              ON recheck.article_id = snapshots.article_id
            LEFT JOIN push_keyword_cooldowns AS cooldown
              ON cooldown.article_id = snapshots.article_id
             AND cooldown.recipient_key = ? AND cooldown.expires_at > ?
            WHERE history.id IS NULL
              AND cooldown.article_id IS NULL
              AND snapshots.captured_at <= ?
              AND COALESCE(recheck.retired, 0) = 0
              AND (recheck.last_checked IS NULL OR recheck.last_checked <= ?)
              AND (
                    (snapshots.worthy >= ? AND snapshots.collection >= ?)
                    OR snapshots.worthy >= ?
                    OR snapshots.comments >= ?
                  )
              AND (
                    (julianday('now') - julianday(snapshots.captured_at)) * 24
                    + COALESCE(snapshots.age_hours, 0)
                  ) <= ?
            ORDER BY CASE WHEN recheck.last_checked IS NULL THEN 0 ELSE 1 END,
                     (
                       (julianday('now') - julianday(snapshots.captured_at)) * 24
                       + COALESCE(snapshots.age_hours, 0)
                     ) DESC,
                     COALESCE(recheck.last_checked, snapshots.captured_at) ASC,
                     snapshots.composite_score DESC
            LIMIT ?
            ''',
            (
                self._push_recipient_key(),
                datetime.utcnow().isoformat(timespec='seconds'),
                cutoff.isoformat(timespec='seconds'),
                cutoff.isoformat(timespec='seconds'),
                CONFIG['late_recheck_min_worthy'],
                CONFIG['late_recheck_min_collection'],
                CONFIG['early_signal_min_worthy'],
                CONFIG['late_recheck_min_comments'],
                max_age_hours,
                pool_limit,
            )
        )
        excluded = {str(value) for value in (discovered_ids or set())}
        selected = set()
        rows = []
        fetched_rows = cursor.fetchall()
        self.late_recheck_eligible_count = max(
            (self._safe_nonnegative_int(row[8]) for row in fetched_rows),
            default=0,
        )
        for row in fetched_rows:
            article_id = str(row[0] or '').strip()
            if not article_id or article_id in excluded or article_id in selected:
                continue
            selected.add(article_id)
            rows.append({
                'article_id': article_id,
                'title': row[1],
                'worthy': self._safe_nonnegative_int(row[2]),
                'collection': self._safe_nonnegative_int(row[3]),
                'comments': self._safe_nonnegative_int(row[4]),
                'composite_score': self._safe_nonnegative_int(row[5]),
                'age_hours': float(row[6] or 0),
                'captured_at': row[7],
            })
            if len(rows) >= limit:
                break
        return rows

    @staticmethod
    def _calculate_late_recheck_limit(eligible_count):
        minimum = max(0, int(CONFIG.get('late_recheck_min_per_run', 0)))
        maximum = max(minimum, int(CONFIG.get('max_late_rechecks_per_run', minimum)))
        ratio = min(
            1,
            max(0, float(CONFIG.get('late_recheck_candidate_ratio', 0))),
        )
        scaled = int(max(0, eligible_count) * ratio)
        if eligible_count * ratio > scaled:
            scaled += 1
        return min(maximum, max(minimum, scaled))

    def _mark_late_recheck(self, article_id, retired=False):
        self.conn.execute(
            '''
            INSERT INTO late_recheck_state (article_id, last_checked, retired)
            VALUES (?, ?, ?)
            ON CONFLICT(article_id) DO UPDATE SET
                last_checked = excluded.last_checked,
                retired = MAX(late_recheck_state.retired, excluded.retired)
            ''',
            (
                str(article_id),
                datetime.utcnow().isoformat(timespec='seconds'),
                1 if retired else 0,
            )
        )
        self.pending_snapshot_writes += 1

    @staticmethod
    def _build_detail_api_params(now_ms=None):
        params = {
            'weixin': 1,
            'basic_v': 0,
            'f': 'android',
            'v': CONFIG['detail_api_version'],
            'time': str(int(time.time() * 1000) if now_ms is None else int(now_ms)),
            'imgmode': 0,
            'hashcode': '',
            'h5hash': '',
        }
        sign_parts = []
        for key in sorted(params):
            if params[key] == '':
                continue
            value = re.sub(r'\s+', '', str(params[key]))
            sign_parts.append(f"{key}={value}")
        sign_data = '&'.join(sign_parts)
        params['sign'] = hashlib.md5(
            f"{sign_data}&key={SMZDM_DETAIL_SIGN_KEY}".encode('utf-8')
        ).hexdigest().upper()
        return params

    def _fetch_late_recheck_detail(self, article_id):
        if not article_id or self.external_checks_suspended:
            return None

        self.stats['total_late_recheck_attempted'] += 1
        url = CONFIG['late_recheck_url'].format(article_id=article_id)
        version = CONFIG['detail_api_version']
        revision = CONFIG['detail_api_revision']
        try:
            self._throttle_external_request()
            response = self.session.get(
                url,
                params=self._build_detail_api_params(),
                headers={
                    'Accept': '*/*',
                    'Accept-Language': 'zh-Hans-CN;q=1',
                    'request_key': ''.join(random.choice('0123456789') for _ in range(18)),
                    'User-Agent': (
                        f"smzdm_android_V{version} rv:{revision} "
                        "(Redmi Note 3;Android10.0;zh)smzdmapp"
                    ),
                    'Referer': f'https://www.smzdm.com/p/{article_id}/',
                },
                timeout=CONFIG['timeout'],
            )
            if self._is_waf_response(response):
                self.stats['total_late_recheck_unavailable'] += 1
                self._suspend_external_checks("延迟复查详情接口触发反爬/验证码")
                return None
            if response.status_code != 200:
                self._record_late_recheck_failure(f"HTTP {response.status_code}")
                return None
            payload = response.json()
            data = payload.get('data') if isinstance(payload, dict) else None
            if str(payload.get('error_code')) != '0' or not isinstance(data, dict):
                reason = payload.get('error_msg') if isinstance(payload, dict) else 'invalid JSON'
                self._record_late_recheck_failure(reason or 'invalid response')
                return None
        except Exception as e:
            self._record_late_recheck_failure(type(e).__name__)
            return None

        self.consecutive_detail_request_failures = 0
        self.stats['total_late_recheck_fetched'] += 1
        return data

    def _record_late_recheck_failure(self, reason):
        self.stats['total_late_recheck_unavailable'] += 1
        self.consecutive_detail_request_failures += 1
        logging.warning(f"延迟复查详情接口不可用: {reason}")
        limit = max(1, int(CONFIG.get('max_consecutive_detail_failures', 2)))
        if self.consecutive_detail_request_failures >= limit:
            self._suspend_external_checks(
                f"延迟复查详情接口连续 {self.consecutive_detail_request_failures} 次失败（{reason}）"
            )

    def _parse_late_recheck_detail(self, data):
        article_id = str(data.get('article_id') or '').strip()
        title = str(data.get('article_title') or '').strip()[:200]
        channel_type = str(
            data.get('channel_name') or data.get('gtm_channel_name') or ''
        ).strip()
        if not article_id or not title:
            return None
        whitelist = CONFIG.get('whitelist_channel_types')
        if whitelist and channel_type not in whitelist:
            return None

        mall = str(data.get('article_mall') or '未知').strip()
        mall_no = str(data.get('article_mall_id') or '').strip()
        article_link = str(data.get('dingyue_product_url') or '').strip()
        product_no = ''
        product_match = re.search(r'item\.jd\.com/(\d+)', article_link)
        if product_match:
            product_no = product_match.group(1)

        article_status = str(data.get('article_status') or '').strip()
        parsed = {
            'id': article_id,
            'title': title,
            'price': str(data.get('article_price') or '未知').strip(),
            'link': str(
                data.get('article_url') or f'https://www.smzdm.com/p/{article_id}/'
            ).strip(),
            'article_link': article_link,
            'channel_type': channel_type,
            'mall': mall,
            'mall_no': mall_no,
            'product_no': product_no,
            'product_key': '',
            'pub_time': str(
                data.get('article_format_date') or data.get('article_pubdate') or ''
            ).strip(),
            'comments': self._safe_nonnegative_int(data.get('article_comment')),
            'collection': self._safe_nonnegative_int(data.get('article_collection')),
            'worthy': self._safe_nonnegative_int(data.get('article_worthy')),
            'unworthy': self._safe_nonnegative_int(data.get('article_unworthy')),
            'age_hours': self._detail_age_hours(data),
            'fingerprint': self._build_title_fingerprint(title),
            'price_value': self._parse_price_value(data.get('article_price', '')),
            'is_sold_out': self._truthy_field(
                data.get('article_is_sold_out') or data.get('article_stock_status')
            ),
            'is_timeout': article_status not in ('', '0'),
            'status_text': str(data.get('article_status_name') or '').strip(),
            'late_recheck_source': True,
        }
        parsed['product_key'] = self._build_product_key(parsed)
        return parsed

    @staticmethod
    def _detail_age_hours(data):
        china_tz = timezone(timedelta(hours=8))
        for field in ('article_date', 'article_pubdate'):
            value = str(data.get(field) or '').strip()
            if not value:
                continue
            try:
                article_time = datetime.strptime(value, '%Y-%m-%d %H:%M:%S')
            except ValueError:
                continue
            article_time = article_time.replace(tzinfo=china_tz)
            return max(
                0,
                (datetime.now(timezone.utc) - article_time).total_seconds() / 3600,
            )
        return 0

    def _parse_rank_item(self, row, position):
        mapped = {
            'article_id': row.get('article_id'),
            'article_channel_type': row.get('article_channel_type') or 'youhui',
            'article_title': row.get('article_title'),
            'article_price': row.get('article_subtitle'),
            'article_url': row.get('article_url'),
            'article_format_date': row.get('article_date'),
            'publish_date_lt': row.get('article_timesort'),
            'article_mall': row.get('article_mall'),
            'article_worthy': row.get('article_worthy'),
            'article_unworthy': row.get('article_unworthy'),
            'article_comment': row.get('article_comment'),
            'article_is_sold_out': row.get('article_stock_status'),
            'article_pic': row.get('article_pic'),
            'tongji_hudong': self._rank_tongji_hudong(row),
        }
        parsed = self._parse_item(mapped)
        if parsed:
            self._apply_rank_metrics(parsed, row, position)
        return parsed

    @staticmethod
    def _rank_tongji_hudong(row):
        return ','.join([
            f"评论_{SmzdmScraper._safe_nonnegative_int(row.get('article_comment'))}",
            f"收藏_{SmzdmScraper._safe_nonnegative_int(row.get('article_collection'))}",
            f"值_{SmzdmScraper._safe_nonnegative_int(row.get('article_worthy'))}",
            f"不值_{SmzdmScraper._safe_nonnegative_int(row.get('article_unworthy'))}",
        ])

    @staticmethod
    def _safe_nonnegative_int(value):
        try:
            return max(0, int(value or 0))
        except (TypeError, ValueError):
            return 0

    @staticmethod
    def _apply_rank_metrics(parsed, row, position):
        metric_fields = {
            'comments': 'article_comment',
            'collection': 'article_collection',
            'worthy': 'article_worthy',
            'unworthy': 'article_unworthy',
        }
        for parsed_field, rank_field in metric_fields.items():
            parsed[parsed_field] = max(
                parsed.get(parsed_field, 0) or 0,
                SmzdmScraper._safe_nonnegative_int(row.get(rank_field)),
            )
        parsed['rank_source'] = True
        parsed['rank_position'] = position
        parsed['rank_window_hours'] = SmzdmScraper._safe_nonnegative_int(
            row.get('_rank_window_hours')
        ) or int((CONFIG.get('rank_source_hours') or [12])[-1])
        if not parsed.get('link'):
            parsed['link'] = str(row.get('article_url') or '').strip()

    def _fetch_page(self, page):
        """获取列表页数据"""
        offset = (page - 1) * CONFIG['items_per_page']
        try:
            response = self.session.get(
                f'https://api.smzdm.com/v1/list?limit={CONFIG["items_per_page"]}&offset={offset}',
                timeout=CONFIG['timeout']
            )
            if response.status_code != 200:
                logging.warning(f"第{page}页 HTTP {response.status_code}")
                return None

            resp_json = response.json()
            if isinstance(resp_json, dict):
                data = resp_json.get('data', {})
                if isinstance(data, dict):
                    return data.get('rows', [])
                elif isinstance(data, list):
                    return data
            elif isinstance(resp_json, list):
                return resp_json
            return []
        except Exception as e:
            logging.error(f"第{page}页请求异常: {e}")
            return None

    def _parse_item(self, item):
        """解析单个商品数据"""
        if not item or 'article_id' not in item:
            return None

        # 频道白名单过滤
        channel_type = str(item.get('article_channel_type', '')).strip()
        whitelist = CONFIG.get('whitelist_channel_types')
        if whitelist and channel_type not in whitelist:
            return None

        article_id = str(item['article_id']).strip()
        title = str(item.get('article_title', '')).strip()[:200]
        if not article_id or not title:
            return None

        # 计算发布时间
        age_hours = 0
        time_sort = str(item.get('publish_date_lt', '0')).strip()
        if time_sort.isdigit() and int(time_sort) > 0:
            ts = int(time_sort)
            if ts > 10000000000:
                ts = ts / 1000
            item_time = datetime.fromtimestamp(ts)
            age_hours = (datetime.now() - item_time).total_seconds() / 3600

        worthy = max(0, int(item.get('article_worthy', 0)))
        unworthy = max(0, int(item.get('article_unworthy', 0)))
        comments = max(0, int(item.get('article_comment', 0)))

        # 解析 tongji_hudong 获取精确数据
        tongji = self._parse_tongji_hudong(item.get('tongji_hudong', ''))
        article_link = self._extract_article_link(item)
        mall_no, product_no = self._extract_mall_client(item)

        return {
            'id': article_id,
            'title': title,
            'price': str(item.get('article_price', '未知')).strip(),
            'link': str(item.get('article_url', '')).strip(),
            'pic': str(item.get('article_pic') or '').strip(),
            'article_link': article_link,
            'channel_type': channel_type,
            'mall': str(item.get('article_mall', '未知')).strip(),
            'mall_no': mall_no,
            'product_no': product_no,
            'product_key': '',
            'pub_time': str(item.get('article_format_date', '')).strip(),
            'comments': tongji['comments'] or comments,
            'collection': tongji['collection'],
            'worthy': tongji['worthy'] or worthy,
            'unworthy': tongji['unworthy'] or unworthy,
            'age_hours': age_hours,
            'fingerprint': self._build_title_fingerprint(title),
            'price_value': self._parse_price_value(item.get('article_price', '')),
            'is_sold_out': self._truthy_field(item.get('article_is_sold_out')),
            'is_timeout': self._truthy_field(item.get('article_is_timeout')),
            'status_text': ' '.join(
                str(item.get(key, '')).strip()
                for key in ('stock_status_name', 'article_status_name')
                if str(item.get(key, '')).strip()
            ),
        }

    @staticmethod
    def _extract_mall_client(item):
        mall_client = item.get('article_mall_client') or {}
        if isinstance(mall_client, str):
            try:
                mall_client = json.loads(mall_client)
            except json.JSONDecodeError:
                mall_client = {}
        if not isinstance(mall_client, dict):
            return '', ''
        return (
            str(mall_client.get('mall_no') or '').strip(),
            str(mall_client.get('product_no') or '').strip(),
        )

    @staticmethod
    def _truthy_field(value):
        text = str(value or '').strip().lower()
        return text not in ('', '0', 'false', 'none', 'null')

    def _extract_article_link(self, item):
        article_link = str(item.get('article_link') or '').strip()
        if article_link:
            return article_link

        redirect_data = item.get('redirect_data') or {}
        if isinstance(redirect_data, str):
            try:
                redirect_data = json.loads(redirect_data)
            except json.JSONDecodeError:
                redirect_data = {}
        if not isinstance(redirect_data, dict):
            return ''

        md5_url = str(redirect_data.get('md5_url') or '').strip()
        if not md5_url:
            return ''
        if md5_url.startswith(('http://', 'https://')):
            return md5_url
        # 列表接口常见的 md5_url 只有 32 位哈希，直接拼 go.smzdm.com 会 404；
        # 只有拿到完整路径片段时才作为 article_link 回退。
        if '/' not in md5_url:
            return ''
        return f"https://go.smzdm.com/{md5_url.lstrip('/')}"

    def _parse_tongji_hudong(self, tongji_str):
        """解析 tongji_hudong 字段：评论_5,收藏_3,值_10,不值_2"""
        result = {'comments': 0, 'collection': 0, 'worthy': 0, 'unworthy': 0}
        if not tongji_str:
            return result
        mapping = {'评论': 'comments', '收藏': 'collection', '值': 'worthy', '不值': 'unworthy'}
        for part in tongji_str.split(','):
            if '_' in part:
                key, value = part.split('_', 1)
                if key in mapping and value.isdigit():
                    result[mapping[key]] = int(value)
        return result

    def _build_title_fingerprint(self, title):
        """生成跨文章 ID 的商品指纹，降低重复爆料推送。"""
        text = title.lower()
        text = re.sub(r'<[^>]+>', '', text)
        text = re.sub(r'（[^）]*(券|优惠|返|plus|会员|满减|到手|需买|凑单)[^）]*）', '', text)
        text = re.sub(r'\([^)]*(券|优惠|返|plus|会员|满减|到手|需买|凑单)[^)]*\)', '', text)

        promo_words = [
            'plus会员', '88vip', '值友专享', '今日必买', '百亿补贴', '淘金币可用',
            '历史低价', '历史新低', '新低价', '新低', '限地区', '限时', '临期品',
            '凑单品', '需用券', '券后', '下单立减', '满减', '可用券', '可用',
        ]
        for word in promo_words:
            text = text.replace(word, '')

        # 价格可能出现在商品名前或套餐名中；只归一化金额，不能删除后面的商品主体。
        text = re.sub(r'\d+(?:\.\d+)?\s*元', '价格', text)
        text = re.sub(r'[（(](?:\d+色|多色|多款)可选[）)]', '', text)
        text = re.sub(r'[^\w\u4e00-\u9fff]+', '', text)
        if len(text) < CONFIG['fingerprint_min_len']:
            return ''
        return text[:80]

    def _parse_price_value(self, price_text):
        """从价格文本中提取可比较的人民币数值，解析失败返回 None。"""
        text = str(price_text or '')
        text = text.replace(',', '')
        values = []
        for match in re.finditer(r'(\d+(?:\.\d+)?)\s*元', text):
            prefix = text[max(0, match.start() - 16):match.start()]
            # 返还额、满减门槛、赠品面值和原价不是当前售价。
            if re.search(r'(?:返(?:还)?|退(?:还)?|赠|送|赔|满|减|立减|省|原价|价值|面值|券面额)\s*$', prefix):
                continue
            values.append(float(match.group(1)))
        if not values:
            return None
        if any(keyword in text for keyword in ('低至', '折合', '约', '券后', '到手')):
            return min(values)
        return values[-1]

    # ==================== 趋势快照 ====================

    def _attach_trend_metrics(self, parsed):
        key = self._candidate_snapshot_key(parsed)
        article_id = str(parsed.get('id') or '').strip()
        empty_metrics = self._empty_trend_metrics()
        empty_metrics.update({'item_key': key, 'article_id': article_id})
        if not key:
            parsed['trend_metrics'] = empty_metrics
            return

        cursor = self.conn.execute(
            '''
            SELECT captured_at, comments, collection, worthy, unworthy
            FROM candidate_snapshots
            WHERE article_id = ?
            ORDER BY captured_at ASC
            ''',
            # 旧数据库的 key 可能是 fp/sku；按文章取数既保留同文章旧历史，
            # 又不把另一篇爆料独立的互动计数当作本篇的增长。
            (article_id,)
        )
        rows = cursor.fetchall()
        if not rows:
            parsed['trend_metrics'] = empty_metrics
            return

        first = rows[0]
        previous = rows[-1]
        now = datetime.utcnow()
        first_time = self._parse_snapshot_time(first[0])
        previous_time = self._parse_snapshot_time(previous[0])
        elapsed_minutes = self._minutes_between(first_time, now)
        recent_minutes = self._minutes_between(previous_time, now)

        first_delta = self._build_growth_delta(parsed, first)
        recent_delta = self._build_growth_delta(parsed, previous)
        growth_score = self._calculate_growth_score(first_delta)
        recent_growth_score = self._calculate_growth_score(recent_delta)
        growth_per_hour = round(growth_score / max(elapsed_minutes / 60, 0.25), 1)
        recent_growth_per_hour = round(recent_growth_score / max(recent_minutes / 60, 0.25), 1)

        parsed['trend_metrics'] = {
            'snapshot_count': len(rows),
            'item_key': key,
            'article_id': article_id,
            'first_captured_at': first[0],
            'previous_captured_at': previous[0],
            'elapsed_minutes': round(elapsed_minutes),
            'recent_minutes': round(recent_minutes),
            'delta_comments': first_delta['comments'],
            'delta_collection': first_delta['collection'],
            'delta_worthy': first_delta['worthy'],
            'delta_unworthy': first_delta['unworthy'],
            'recent_delta_comments': recent_delta['comments'],
            'recent_delta_collection': recent_delta['collection'],
            'recent_delta_worthy': recent_delta['worthy'],
            'recent_delta_unworthy': recent_delta['unworthy'],
            'growth_score': growth_score,
            'recent_growth_score': recent_growth_score,
            'signal_growth_score': self._calculate_signal_growth_score(first_delta),
            'recent_signal_growth_score': self._calculate_signal_growth_score(recent_delta),
            'growth_per_hour': growth_per_hour,
            'recent_growth_per_hour': recent_growth_per_hour,
        }
        self.stats['total_trend_candidates'] += 1

    @staticmethod
    def _empty_trend_metrics():
        return {
            'snapshot_count': 0,
            'item_key': '',
            'article_id': '',
            'first_captured_at': '',
            'previous_captured_at': '',
            'elapsed_minutes': 0,
            'recent_minutes': 0,
            'delta_comments': 0,
            'delta_collection': 0,
            'delta_worthy': 0,
            'delta_unworthy': 0,
            'recent_delta_comments': 0,
            'recent_delta_collection': 0,
            'recent_delta_worthy': 0,
            'recent_delta_unworthy': 0,
            'growth_score': 0,
            'recent_growth_score': 0,
            'signal_growth_score': 0,
            'recent_signal_growth_score': 0,
            'growth_per_hour': 0,
            'recent_growth_per_hour': 0,
        }

    @staticmethod
    def _parse_snapshot_time(value):
        if not value:
            return datetime.utcnow()
        text = str(value).replace('Z', '+00:00')
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            try:
                parsed = datetime.strptime(str(value), '%Y-%m-%d %H:%M:%S')
            except ValueError:
                return datetime.utcnow()
        if parsed.tzinfo is not None:
            parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
        return parsed

    @staticmethod
    def _minutes_between(start, end):
        return max(0, (end - start).total_seconds() / 60)

    @staticmethod
    def _build_growth_delta(parsed, row):
        return {
            'comments': max(0, int(parsed.get('comments', 0) or 0) - int(row[1] or 0)),
            'collection': max(0, int(parsed.get('collection', 0) or 0) - int(row[2] or 0)),
            'worthy': max(0, int(parsed.get('worthy', 0) or 0) - int(row[3] or 0)),
            'unworthy': max(0, int(parsed.get('unworthy', 0) or 0) - int(row[4] or 0)),
        }

    @staticmethod
    def _calculate_growth_score(delta):
        weights = CONFIG['trend_weights']
        return (
            delta['comments'] * weights['comments']
            + delta['collection'] * weights['collection']
            + delta['worthy'] * weights['worthy']
            + delta['unworthy'] * weights['unworthy']
        )

    @staticmethod
    def _calculate_signal_growth_score(delta):
        """Only count purchase-intent signals; comments alone cannot confirm warming."""
        weights = CONFIG['trend_weights']
        return (
            delta['collection'] * weights['collection']
            + delta['worthy'] * weights['worthy']
        )

    def _save_candidate_snapshot(self, parsed):
        if not self._should_save_candidate_snapshot(parsed):
            return

        item_key = self._candidate_snapshot_key(parsed)
        if not item_key or item_key in self.snapshot_keys_seen_this_run:
            return
        self.snapshot_keys_seen_this_run.add(item_key)

        metrics = self._current_interaction_metrics(parsed)
        self.conn.execute(
            '''
            INSERT INTO candidate_snapshots (
                item_key, article_id, title, fingerprint, product_key, mall,
                price, price_value, comments, collection, worthy, unworthy,
                score_rate, composite_score, age_hours, quality_path, captured_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''',
            (
                item_key,
                str(parsed.get('id') or '').strip(),
                parsed.get('title', '')[:100],
                parsed.get('fingerprint', ''),
                parsed.get('product_key') or self._build_product_key(parsed),
                parsed.get('mall', ''),
                parsed.get('price', ''),
                parsed.get('price_value'),
                parsed.get('comments', 0),
                parsed.get('collection', 0),
                parsed.get('worthy', 0),
                parsed.get('unworthy', 0),
                metrics['score_rate'],
                metrics['composite_score'],
                parsed.get('age_hours', 0),
                parsed.get('quality_path', ''),
                datetime.utcnow().isoformat(timespec='seconds'),
            )
        )
        self.pending_snapshot_writes += 1
        self.stats['total_candidate_snapshots_saved'] += 1

    def _commit_candidate_snapshots(self):
        if self.pending_snapshot_writes <= 0:
            return
        self.conn.commit()
        self.pending_snapshot_writes = 0

    def _should_save_candidate_snapshot(self, parsed):
        if self._is_inactive_deal(parsed):
            return False
        return (
            parsed.get('worthy', 0) >= 1
            or parsed.get('comments', 0) >= 1
            or parsed.get('collection', 0) >= CONFIG['snapshot_min_collection']
        )

    def _candidate_snapshot_key(self, parsed):
        # 互动计数属于文章；SKU/标题指纹仅用于商品推送去重。
        article_id = str(parsed.get('id') or '').strip()
        return f"id:{article_id}" if article_id else ''

    @staticmethod
    def _current_interaction_metrics(parsed):
        comments = parsed.get('comments', 0) or 0
        collection = parsed.get('collection', 0) or 0
        worthy = parsed.get('worthy', 0) or 0
        unworthy = parsed.get('unworthy', 0) or 0
        total_votes = worthy + unworthy
        weights = CONFIG['score_weights']
        return {
            'total_engagement': comments + collection + worthy,
            'score_rate': round(worthy / total_votes * 100) if total_votes > 0 else 0,
            'composite_score': (
                comments * weights['comments']
                + collection * weights['collection']
                + worthy * weights['worthy']
            ),
        }

    # ==================== 筛选 ====================

    @staticmethod
    def _candidate_log_context(parsed):
        article_id = str(parsed.get('id') or parsed.get('article_id') or '-').strip()
        source = '延迟详情' if parsed.get('late_recheck_source') else '主列表'
        if parsed.get('keyword_search_source'):
            source = '关键词查询'
        if parsed.get('rank_source'):
            rank = f"{parsed.get('rank_window_hours') or 12}小时榜#{parsed.get('rank_position') or '-'}"
            source = rank if not parsed.get('late_recheck_source') else f'{source}+{rank}'
        return f'id:{article_id} 来源:{source}'

    @staticmethod
    def _trend_log_context(parsed):
        trend = parsed.get('trend_metrics') or {}
        return (
            f"趋势文章:{trend.get('article_id') or parsed.get('id') or '-'} "
            f"首快照:{trend.get('first_captured_at') or '-'} "
            f"前快照:{trend.get('previous_captured_at') or '-'}"
        )

    def _is_title_blocked(self, parsed):
        text = self._normalize_task_title(
            f"{parsed.get('mall', '')} {parsed.get('title', '')}"
        )
        for pattern in CONFIG.get('title_block_patterns', []):
            if re.search(pattern, text, flags=re.IGNORECASE):
                parsed['blocked_title_pattern'] = pattern
                logging.warning(
                    f"[标题正则过滤] {parsed['title'][:40]}... | {self._candidate_log_context(parsed)} "
                    f"pattern:{pattern}"
                )
                return True
        return False

    @staticmethod
    def _normalize_task_title(value):
        """Normalize common reward-title obfuscation before applying task rules."""
        text = str(value or '').translate(str.maketrans('０１２３４５６７８９', '0123456789'))
        text = text.replace('\ufe0f', '').replace('\u20e3', '')
        text = re.sub(r'[①❶➀]', '1', text)
        text = re.sub(r'[②❷➁]', '2', text)
        text = re.sub(r'[③❸➂]', '3', text)
        text = re.sub(r'[④❹➃]', '4', text)
        text = re.sub(r'[⑤❺➄]', '5', text)
        text = re.sub(r'[⑥❻➅]', '6', text)
        text = re.sub(r'[⑦❼➆]', '7', text)
        text = re.sub(r'[⑧❽➇]', '8', text)
        text = re.sub(r'[⑨❾➈]', '9', text)
        text = re.sub(r'\s+', ' ', text)
        for token in ('入会', '京自', '店铺'):
            text = re.sub(r'\s+'.join(token), token, text)
        return text.strip()

    @staticmethod
    def _should_wait_for_trend_confirmation(parsed):
        if not CONFIG.get('trend_confirmation_enabled'):
            return False
        if parsed.get('quality_path') not in CONFIG.get('trend_confirmation_paths', []):
            return False
        trend = parsed.get('trend_metrics') or {}
        balanced_now = (
            parsed.get('comments', 0) >= CONFIG['trend_confirmation_min_comments']
            and parsed.get('composite_score', 0) >= CONFIG['trend_confirmation_min_score']
            and parsed.get('worthy', 0) >= CONFIG['trend_confirmation_min_worthy']
            and parsed.get('collection', 0) >= CONFIG['trend_confirmation_min_collection']
        )
        discussion_now = (
            parsed.get('comments', 0) >= CONFIG['trend_confirmation_discussion_min_comments']
            and parsed.get('composite_score', 0) >= CONFIG['trend_confirmation_discussion_min_score']
            and parsed.get('worthy', 0) >= CONFIG['trend_confirmation_discussion_min_worthy']
            and parsed.get('collection', 0) >= CONFIG['trend_confirmation_discussion_min_collection']
        )
        if balanced_now or discussion_now:
            return False
        return not SmzdmScraper._has_confirmed_early_trend(trend)

    @staticmethod
    def _has_confirmed_early_trend(trend):
        if trend.get('snapshot_count', 0) <= 0:
            return False
        recent_ok = (
            trend.get('recent_minutes', 0) <= CONFIG['warming_recent_max_minutes']
            and trend.get('recent_growth_score', 0) >= CONFIG['trend_confirmation_min_growth_score']
            and trend.get('recent_signal_growth_score', 0)
            >= CONFIG['trend_confirmation_min_signal_growth_score']
            and trend.get('recent_delta_worthy', 0)
            >= CONFIG['trend_confirmation_min_worthy_growth']
            and trend.get('recent_growth_per_hour', 0)
            >= CONFIG['trend_confirmation_min_growth_per_hour']
        )
        cumulative_ok = (
            trend.get('elapsed_minutes', 0) <= CONFIG['warming_cumulative_max_minutes']
            and trend.get('growth_score', 0) >= CONFIG['trend_confirmation_min_growth_score']
            and trend.get('signal_growth_score', 0)
            >= CONFIG['trend_confirmation_min_signal_growth_score']
            and trend.get('delta_worthy', 0)
            >= CONFIG['trend_confirmation_min_worthy_growth']
            and trend.get('growth_per_hour', 0)
            >= CONFIG['trend_confirmation_min_growth_per_hour']
        )
        return recent_ok or cumulative_ok

    @staticmethod
    def _is_low_growth_candidate(parsed):
        if not CONFIG.get('trend_low_growth_filter_enabled'):
            return False
        if parsed.get('quality_path') in CONFIG.get('trend_low_growth_exempt_paths', []):
            return False
        if parsed.get('comments', 0) > CONFIG['trend_low_growth_max_comments']:
            return False

        trend = parsed.get('trend_metrics') or {}
        if trend.get('snapshot_count', 0) <= 0:
            return False
        if trend.get('elapsed_minutes', 0) < CONFIG['trend_low_growth_min_age_minutes']:
            return False

        return (
            trend.get('growth_score', 0) < CONFIG['trend_low_growth_min_growth_score']
            and trend.get('recent_growth_score', 0) < CONFIG['trend_low_growth_min_recent_score']
        )

    @staticmethod
    def _is_slow_growth_candidate(parsed):
        if not CONFIG.get('trend_slow_filter_enabled'):
            return False
        if parsed.get('quality_path') in CONFIG.get('trend_slow_exempt_paths', []):
            return False
        if parsed.get('comments', 0) > CONFIG['trend_slow_max_comments']:
            return False

        trend = parsed.get('trend_metrics') or {}
        if trend.get('snapshot_count', 0) <= 0:
            return False
        if trend.get('elapsed_minutes', 0) < CONFIG['trend_slow_min_age_minutes']:
            return False
        if trend.get('recent_growth_score', 0) > CONFIG['trend_slow_max_recent_score']:
            return False

        return trend.get('growth_per_hour', 0) <= CONFIG['trend_slow_max_growth_per_hour']

    @staticmethod
    def _has_recent_warming_trend(trend):
        if trend.get('snapshot_count', 0) <= 0:
            return False
        if trend.get('recent_minutes', 0) > CONFIG['warming_recent_max_minutes']:
            return False
        return (
            trend.get('recent_growth_score', 0) >= CONFIG['warming_recent_min_growth_score']
            and trend.get('recent_signal_growth_score', 0)
            >= CONFIG['warming_min_signal_growth_score']
            and trend.get('recent_delta_worthy', 0) >= CONFIG['warming_min_worthy_growth']
            and trend.get('recent_growth_per_hour', 0) >= CONFIG['warming_min_growth_per_hour']
        )

    @staticmethod
    def _has_cumulative_warming_trend(trend):
        if trend.get('snapshot_count', 0) <= 0:
            return False
        if trend.get('elapsed_minutes', 0) > CONFIG['warming_cumulative_max_minutes']:
            return False
        return (
            trend.get('growth_score', 0) >= CONFIG['warming_min_growth_score']
            and trend.get('signal_growth_score', 0) >= CONFIG['warming_min_signal_growth_score']
            and trend.get('delta_worthy', 0) >= CONFIG['warming_min_worthy_growth']
            and trend.get('growth_per_hour', 0) >= CONFIG['warming_min_growth_per_hour']
        )

    @staticmethod
    def _has_warming_trend(trend):
        return (
            SmzdmScraper._has_recent_warming_trend(trend)
            or SmzdmScraper._has_cumulative_warming_trend(trend)
        )

    @staticmethod
    def _effective_trend_score(trend):
        if SmzdmScraper._has_recent_warming_trend(trend):
            return trend.get('recent_growth_score', 0)
        if SmzdmScraper._has_cumulative_warming_trend(trend):
            return trend.get('growth_score', 0)
        return 0

    @staticmethod
    def _meets_score_rate_requirement(worthy, unworthy, score_rate, required_rate):
        if score_rate >= required_rate:
            return True
        return (
            worthy >= CONFIG['single_unworthy_tolerance_min_worthy']
            and unworthy <= CONFIG['single_unworthy_tolerance_max_unworthy']
            and score_rate >= CONFIG['single_unworthy_tolerance_min_score_rate']
        )

    def _filter_stage1(self, parsed):
        """第一阶段：综合评分筛选"""
        comments = parsed['comments']
        collection = parsed['collection']
        worthy = parsed['worthy']
        unworthy = parsed['unworthy']

        if self._is_inactive_deal(parsed):
            return False

        if worthy < CONFIG['min_signal_worthy'] and comments < CONFIG['min_signal_comments']:
            return False

        metrics = self._current_interaction_metrics(parsed)
        total_engagement = metrics['total_engagement']
        score_rate = metrics['score_rate']
        composite_score = metrics['composite_score']
        trend = parsed.get('trend_metrics') or self._empty_trend_metrics()
        growth_score = trend.get('growth_score', 0)
        recent_growth_score = trend.get('recent_growth_score', 0)
        effective_trend_score = self._effective_trend_score(trend)
        total_votes = worthy + unworthy

        if total_votes >= 3 and score_rate < CONFIG['min_score_rate_relaxed']:
            return False

        quality_path = ''
        if (comments >= CONFIG['super_deal_min_comments']
                and worthy >= CONFIG['super_deal_min_worthy']
                and collection >= CONFIG['super_deal_min_collection']
                and composite_score >= CONFIG['super_deal_min_composite_score']
                and score_rate >= CONFIG['super_deal_min_score_rate']):
            quality_path = '超级好价'
        elif (comments >= CONFIG['discussion_min_comments']
              and worthy >= CONFIG['discussion_min_worthy']
              and collection >= CONFIG['discussion_min_collection']
              and total_engagement >= CONFIG['discussion_min_total_engagement']
              and composite_score >= CONFIG['discussion_min_composite_score']
              and score_rate >= CONFIG['min_score_rate_relaxed']):
            quality_path = '高讨论'
        elif (comments >= CONFIG['min_balanced_comments']
                and worthy >= CONFIG['min_balanced_worthy']
                and collection >= CONFIG['min_balanced_collection']
                and total_engagement >= CONFIG['min_total_engagement']
                and composite_score >= CONFIG['min_composite_score']
                and self._meets_score_rate_requirement(
                    worthy, unworthy, score_rate, CONFIG['min_score_rate']
                )):
            quality_path = '均衡热度'
        elif (worthy >= CONFIG['emerging_min_worthy']
              and comments >= CONFIG['emerging_min_comments']
              and collection >= CONFIG['emerging_min_collection']
              and total_engagement >= CONFIG['emerging_min_total_engagement']
              and composite_score >= CONFIG['emerging_min_composite_score']
              and self._meets_score_rate_requirement(
                  worthy, unworthy, score_rate, CONFIG['emerging_min_score_rate']
              )):
            quality_path = '早期好价'
        elif (worthy >= CONFIG['early_signal_min_worthy']
              and collection >= CONFIG['early_signal_min_collection']
              and total_engagement >= CONFIG['early_signal_min_total_engagement']
              and composite_score >= CONFIG['early_signal_min_composite_score']
              and self._meets_score_rate_requirement(
                  worthy, unworthy, score_rate, CONFIG['early_signal_min_score_rate']
              )):
            quality_path = '早期强信号'
        elif (trend.get('snapshot_count', 0) > 0
              and worthy >= CONFIG['warming_min_worthy']
              and collection >= CONFIG['warming_min_collection']
              and total_engagement >= CONFIG['warming_min_total_engagement']
              and composite_score >= CONFIG['warming_min_composite_score']
              and self._meets_score_rate_requirement(
                  worthy, unworthy, score_rate, CONFIG['warming_min_score_rate']
              )
              and self._has_warming_trend(trend)):
            quality_path = '升温好价'

        if not quality_path:
            return False

        # 好评率和综合评分挂到 parsed 上，供推送使用
        parsed['score_rate'] = score_rate
        parsed['composite_score'] = composite_score
        parsed['trend_score'] = effective_trend_score
        parsed['deal_score'] = composite_score + effective_trend_score
        parsed['quality_path'] = quality_path
        self.quality_path_pass_counts[quality_path] = self.quality_path_pass_counts.get(quality_path, 0) + 1

        logging.info(
            f"[综合评分通过:{quality_path}] {parsed['title'][:40]}... | {self._candidate_log_context(parsed)} "
            f"{self._trend_log_context(parsed)} 评分:{composite_score} 有效增长:{effective_trend_score} "
            f"累计:{growth_score} 近期:{recent_growth_score} "
            f"值增长:{trend.get('delta_worthy', 0)}/"
            f"{trend.get('recent_delta_worthy', 0)} "
            f"收藏增长:{trend.get('delta_collection', 0)}/"
            f"{trend.get('recent_delta_collection', 0)} "
            f"速率:{trend.get('growth_per_hour', 0)}/h 值票支持率:{parsed['score_rate']}% "
            f"评论:{comments} 收藏:{collection} 值:{worthy} 不值:{unworthy}"
        )
        return True

    def _refresh_quality_metrics(self, parsed):
        comments = parsed['comments']
        collection = parsed['collection']
        worthy = parsed['worthy']
        unworthy = parsed['unworthy']
        total_votes = worthy + unworthy
        metrics = self._current_interaction_metrics(parsed)

        parsed['score_rate'] = metrics['score_rate'] if total_votes > 0 else 0
        parsed['composite_score'] = metrics['composite_score']
        trend_score = self._effective_trend_score(parsed.get('trend_metrics') or {})
        parsed['trend_score'] = trend_score
        parsed['deal_score'] = parsed['composite_score'] + trend_score

    def _upgrade_comment_count_from_module(self, parsed, coverage):
        module_total = int(coverage.get('module_total') or 0)
        current_comments = int(parsed.get('comments', 0) or 0)
        if module_total <= current_comments:
            return

        parsed['comments'] = module_total
        parsed['comment_count_note'] = f"评论数按详情模块更新：{current_comments}->{module_total}"
        self._refresh_quality_metrics(parsed)
        self.stats['total_comment_count_upgraded'] += 1
        logging.info(
            f"[评论总数更新] {parsed['title'][:40]}... | "
            f"列表:{current_comments} 模块:{module_total} "
            f"新评分:{parsed.get('composite_score', 0)}"
        )

    def _prioritize_candidates(self, candidates):
        """把有限的评论校验预算优先用在早期/高风险候选上。"""
        path_rank = {
            '超级好价': 6,
            '升温好价': 5,
            '早期好价': 4,
            '早期强信号': 4,
            '高讨论': 3,
            '均衡热度': 2,
        }

        def priority(parsed):
            return (
                path_rank.get(parsed.get('quality_path'), 0),
                parsed.get('comments', 0) >= CONFIG['comment_level_min_comments'],
                parsed.get('composite_score', 0),
                parsed.get('trend_score', 0),
                parsed.get('comments', 0),
                parsed.get('score_rate', 0),
                parsed.get('worthy', 0),
                parsed.get('collection', 0),
            )

        return sorted(candidates, key=priority, reverse=True)

    def _calculate_jd_self_check_limit(self, candidates):
        if not CONFIG.get('jd_self_filter_enabled'):
            return 0

        jd_candidates = sum(1 for parsed in candidates if parsed.get('mall') == '京东')
        if jd_candidates <= 0:
            return 0

        min_budget = max(0, int(CONFIG.get('jd_self_check_min_per_run', 0)))
        max_budget = max(min_budget, int(CONFIG.get('max_jd_self_checks_per_run', min_budget)))
        ratio = float(CONFIG.get('jd_self_check_candidate_ratio', 0))
        ratio = min(max(ratio, 0), 1)

        scaled_budget = int(jd_candidates * ratio)
        if jd_candidates * ratio > scaled_budget:
            scaled_budget += 1

        return min(jd_candidates, max_budget, max(min_budget, scaled_budget))

    def _calculate_comment_level_check_limit(self, candidates):
        if not CONFIG.get('comment_level_check_enabled'):
            return 0

        min_comments = int(CONFIG.get('comment_level_min_comments', 0))
        checkable_candidates = sum(
            1 for parsed in candidates
            if parsed.get('comments', 0) >= min_comments
        )
        if checkable_candidates <= 0:
            return 0

        min_budget = max(0, int(CONFIG.get('comment_level_check_min_per_run', 0)))
        max_budget = max(min_budget, int(CONFIG.get('max_comment_level_checks_per_run', min_budget)))
        ratio = float(CONFIG.get('comment_level_check_candidate_ratio', 0))
        scaled_budget = int(checkable_candidates * ratio)
        if checkable_candidates * ratio > scaled_budget:
            scaled_budget += 1

        return min(checkable_candidates, max_budget, max(min_budget, scaled_budget))

    def _is_inactive_deal(self, parsed):
        if parsed.get('is_sold_out') or parsed.get('is_timeout'):
            return True
        status_text = parsed.get('status_text', '')
        return any(keyword in status_text for keyword in CONFIG.get('excluded_status_keywords', []))

    def _check_jd_self_operated(self, parsed):
        """京东渠道只放行京东自营商品。

        当前主列表接口只返回 article_mall=京东，不含店铺名；实测频道列表
        article_link 可解析到京东商品页，商品页 HTML 中有 isSelf:true/false。
        """
        if parsed['mall'] != '京东':
            return True

        if self.external_checks_suspended:
            return self._handle_jd_unverified(parsed, "外部校验已熔断")

        if self._is_known_jd_self_from_title(parsed):
            logging.info(f"[京东自营标题兜底通过] {parsed['title'][:40]}...")
            return True

        article_link = self._get_article_link(parsed)
        if not article_link:
            return self._handle_jd_unverified(parsed, "未找到 article_link")

        if self.jd_page_checks_unavailable:
            return self._handle_jd_unverified(parsed, "京东商品页云端不可用")

        if self.jd_self_checks >= self.jd_self_check_limit:
            return self._handle_jd_unverified(parsed, "达到本轮京东自营校验上限")
        self.jd_self_checks += 1
        self.jd_fetch_debug = []

        jd_url = self._resolve_smzdm_go_link(article_link, parsed.get('link'))
        if not jd_url:
            return self._handle_jd_unverified(parsed, "无法解析 SMZDM 跳转链接")

        parsed['jd_url'] = jd_url
        try:
            self._throttle_external_request()
            response = self.session.get(
                jd_url,
                headers={
                    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
                    'Accept-Encoding': 'identity',
                },
                timeout=CONFIG['timeout'],
                allow_redirects=True
            )
            self._record_jd_fetch_debug('direct', response)
            if self._is_waf_response(response):
                self._suspend_external_checks("京东商品页触发反爬/验证码")
                return self._handle_jd_unverified(parsed, "京东商品页触发反爬/验证码")
            html_text = response.text
        except Exception as e:
            return self._handle_jd_unverified(parsed, f"京东商品页请求异常: {e}")

        is_self = self._extract_jd_is_self(html_text)
        if is_self is None:
            canonical_url = self._build_jd_canonical_url(jd_url)
            if canonical_url and canonical_url != jd_url:
                parsed['jd_url'] = canonical_url
                is_self = self._fetch_jd_is_self_from_url(canonical_url)
                jd_url = canonical_url
        if is_self is None:
            is_self = self._fetch_jd_is_self_from_mobile(jd_url)

        if is_self is True:
            logging.info(f"[京东自营通过] {parsed['title'][:40]}... | {jd_url}")
            return True
        if is_self is False:
            logging.warning(f"[京东非自营过滤] {parsed['title'][:40]}... | {jd_url}")
            return False
        reason = "京东商品页未找到 isSelf"
        if self.jd_fetch_debug:
            logging.warning(
                f"[京东判定诊断] #{parsed['id']} {parsed['title'][:30]}... | "
                + " ; ".join(self.jd_fetch_debug)
            )
            if not self._jd_fetch_has_product_markers():
                self.jd_page_checks_unavailable = True
                reason = "京东商品页返回云端空壳页"
        return self._handle_jd_unverified(parsed, reason)

    def _handle_jd_unverified(self, parsed, reason):
        action = "过滤" if CONFIG['jd_reject_when_unverified'] else "放行"
        log_fn = logging.info if "达到本轮京东自营校验上限" in reason else logging.warning
        log_fn(f"[京东自营无法确认，{action}] #{parsed['id']} {parsed['title'][:40]}... | {reason}")
        return not CONFIG['jd_reject_when_unverified']

    def _fetch_jd_is_self_from_url(self, jd_url):
        if self.external_checks_suspended:
            return None
        try:
            self._throttle_external_request()
            response = self.session.get(
                jd_url,
                headers={
                    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
                    'Accept-Encoding': 'identity',
                },
                timeout=CONFIG['timeout'],
                allow_redirects=True
            )
            self._record_jd_fetch_debug('canonical', response)
            if self._is_waf_response(response):
                self._suspend_external_checks("京东 canonical 商品页触发反爬/验证码")
                return None
            return self._extract_jd_is_self(response.text)
        except Exception as e:
            logging.warning(f"京东 canonical 商品页请求异常: {e}")
            return None

    def _fetch_jd_is_self_from_mobile(self, jd_url):
        mobile_url = self._build_jd_mobile_url(jd_url)
        if not mobile_url or self.external_checks_suspended:
            return None
        try:
            self._throttle_external_request()
            response = self.session.get(
                mobile_url,
                headers={
                    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
                    'Accept-Encoding': 'identity',
                    'Referer': jd_url,
                    'User-Agent': 'Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) '
                                  'AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148',
                },
                timeout=CONFIG['timeout'],
                allow_redirects=True
            )
            self._record_jd_fetch_debug('mobile', response)
            if self._is_waf_response(response):
                self._suspend_external_checks("京东移动商品页触发反爬/验证码")
                return None
            return self._extract_jd_mobile_is_self(response.text)
        except Exception as e:
            logging.warning(f"京东移动商品页请求异常: {e}")
            return None

    def _record_jd_fetch_debug(self, label, response):
        text = response.text or ''
        markers = []
        for marker in ['isSelf', 'shopName', '自营', 'passport', '验证', '风险']:
            if marker in text:
                markers.append(marker)
        prefix = re.sub(r'\s+', ' ', text[:80]).strip()
        if len(prefix) > 40:
            prefix = prefix[:40]
        self.jd_fetch_debug.append(
            f"{label}:http={response.status_code},ce={response.headers.get('content-encoding') or '-'},"
            f"len={len(text)},markers={','.join(markers) or '-'},head={prefix or '-'}"
        )

    def _jd_fetch_has_product_markers(self):
        return any('markers=-' not in item for item in self.jd_fetch_debug)

    def _is_known_jd_self_from_title(self, parsed):
        title = parsed.get('title', '')
        return any(keyword in title for keyword in CONFIG.get('jd_self_title_keywords', []))

    @staticmethod
    def _build_jd_canonical_url(jd_url):
        sku_id = SmzdmScraper._extract_jd_sku_id(jd_url)
        if not sku_id:
            return ''
        return f"https://item.jd.com/{sku_id}.html"

    @staticmethod
    def _build_jd_mobile_url(jd_url):
        sku_id = SmzdmScraper._extract_jd_sku_id(jd_url)
        if not sku_id:
            return ''
        return f"https://item.m.jd.com/product/{sku_id}.html"

    @staticmethod
    def _extract_jd_sku_id(jd_url):
        match = re.search(r'item(?:\.m)?\.jd\.com/(?:product/)?(\d+)\.html', jd_url)
        return match.group(1) if match else ''

    def _get_article_link(self, parsed):
        article_link = parsed.get('article_link')
        if article_link:
            return article_link

        channel_data = self._get_channel_article_data(parsed)
        article_link = channel_data.get('article_link')
        if article_link:
            parsed['article_link'] = article_link
            return article_link

        return ''

    def _enrich_candidates_from_channel_apis(self, candidates):
        for parsed in candidates:
            channel_data = self._get_channel_article_data(parsed)
            if not channel_data:
                continue

            enriched = False
            article_link = channel_data.get('article_link')
            if article_link and not parsed.get('article_link'):
                parsed['article_link'] = article_link
                enriched = True

            for field in ('mall_no', 'product_no'):
                value = channel_data.get(field)
                if value and not parsed.get(field):
                    parsed[field] = value
                    enriched = True

            product_key = self._build_product_key(parsed)
            if product_key and parsed.get('product_key') != product_key:
                parsed['product_key'] = product_key
                enriched = True

            if enriched:
                self.stats['total_channel_metadata_enriched'] += 1
                logging.info(
                    f"[频道元数据补充] {parsed['title'][:40]}... | "
                    f"link:{'有' if parsed.get('article_link') else '无'} "
                    f"sku:{parsed.get('product_key') or '-'}"
                )

    def _get_channel_article_data(self, parsed):
        article_id = parsed['id']
        if article_id in self.article_data_cache:
            return self.article_data_cache[article_id]
        if article_id in self.article_link_cache:
            return {'article_link': self.article_link_cache[article_id]}

        endpoints = []
        if parsed.get('channel_type') == 'faxian':
            endpoints.append('faxian/list')
        elif parsed.get('channel_type') == 'youhui':
            endpoints.append('youhui/list')
        endpoints.extend(['youhui/list', 'faxian/list'])

        for endpoint in dict.fromkeys(endpoints):
            found = self._lookup_article_data_from_channel_api(article_id, endpoint)
            if found:
                if found.get('article_link'):
                    self.article_link_cache[article_id] = found['article_link']
                self.article_data_cache[article_id] = found
                return found

        self.article_link_cache[article_id] = ''
        self.article_data_cache[article_id] = {}
        return {}

    def _lookup_article_link_from_channel_api(self, article_id, endpoint):
        return self._lookup_article_data_from_channel_api(article_id, endpoint).get('article_link', '')

    def _lookup_article_data_from_channel_api(self, article_id, endpoint):
        endpoint_cache = self.channel_article_data_cache.setdefault(endpoint, {})
        if article_id in endpoint_cache:
            return endpoint_cache[article_id]
        if endpoint in self.channel_link_exhausted:
            return {}

        page_size = CONFIG['jd_link_lookup_page_size']
        start_page = self.channel_link_pages_loaded.get(endpoint, 0)
        for page in range(start_page, CONFIG['jd_link_lookup_pages']):
            offset = page * page_size
            url = f'https://api.smzdm.com/v1/{endpoint}?limit={page_size}&offset={offset}&version=2'
            try:
                response = self.session.get(url, timeout=CONFIG['timeout'])
                if response.status_code != 200:
                    continue
                resp_json = response.json()
            except Exception as e:
                logging.warning(f"article_link 回查失败 {endpoint} offset={offset}: {e}")
                continue

            rows = (resp_json.get('data') or {}).get('rows') if isinstance(resp_json, dict) else []
            if not rows:
                self.channel_link_exhausted.add(endpoint)
                break

            for item in rows:
                item_id = str(item.get('article_id', '')).strip()
                if item_id:
                    endpoint_cache[item_id] = self._extract_channel_article_data(item)
            self.channel_link_pages_loaded[endpoint] = page + 1

            if article_id in endpoint_cache:
                return endpoint_cache[article_id]
            time.sleep(0.1)
        return {}

    def _extract_channel_article_data(self, item):
        mall_no, product_no = self._extract_mall_client(item)
        return {
            'article_link': str(item.get('article_link') or '').strip(),
            'mall_no': mall_no,
            'product_no': product_no,
        }

    def _resolve_smzdm_go_link(self, article_link, referer):
        """解析 SMZDM go 链接到最终京东商品链接。"""
        if self.external_checks_suspended:
            return ''
        try:
            self._throttle_external_request()
            response = self.session.get(
                article_link,
                headers={
                    'Accept': 'application/json, text/plain, */*',
                    'Referer': referer or 'https://www.smzdm.com/',
                },
                timeout=CONFIG['timeout'],
                allow_redirects=False
            )
            if self._is_waf_response(response):
                self._suspend_external_checks("SMZDM 跳转页触发反爬/验证码")
                return ''
        except Exception as e:
            logging.warning(f"SMZDM 跳转页请求失败: {e}")
            return ''

        target = response.headers.get('Location', '')
        if not target:
            target = self._extract_smzdmhref(response.text)
        if not target:
            return ''

        if 'union-click.jd.com/jdc' in target:
            return self._resolve_jd_union_link(target)
        if 'item.jd.com/' in target or 'item.m.jd.com/' in target:
            return target
        return ''

    def _extract_smzdmhref(self, page_text):
        match = re.search(r"smzdmhref\s*=\s*['\"]([^'\"]+)['\"]", page_text)
        if match:
            return html.unescape(match.group(1))

        unpacked = self._unpack_packer_js(page_text)
        unpacked = unpacked.replace("\\'", "'").replace('\\"', '"').replace('\\/', '/')
        match = re.search(r"smzdmhref\s*=\s*['\"]([^'\"]+)['\"]", unpacked)
        if match:
            return html.unescape(match.group(1))
        return ''

    def _unpack_packer_js(self, page_text):
        match = re.search(
            r"eval\((function\(p,a,c,k,e,d\).*?\}\('(?P<payload>.*?)',(?P<base>\d+),(?P<count>\d+),'(?P<words>.*?)'\.split\('\|'\),0,\{\}\))\)",
            page_text,
            re.DOTALL
        )
        if not match:
            return ''

        payload = match.group('payload')
        base = int(match.group('base'))
        count = int(match.group('count'))
        words = match.group('words').split('|')
        if count > len(words):
            words.extend([''] * (count - len(words)))

        for index in range(count - 1, -1, -1):
            word = words[index]
            if not word:
                continue
            payload = re.sub(rf'\b{self._base_n(index, base)}\b', lambda _match: word, payload)
        return payload

    @staticmethod
    def _base_n(num, base):
        chars = '0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ'
        if num == 0:
            return '0'
        result = ''
        while num:
            num, rem = divmod(num, base)
            result = chars[rem] + result
        return result

    def _resolve_jd_union_link(self, union_url):
        if self.external_checks_suspended:
            return ''
        try:
            self._throttle_external_request()
            response = self.session.get(
                union_url,
                headers={
                    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
                    'Referer': 'https://www.smzdm.com/',
                },
                timeout=CONFIG['timeout'],
                allow_redirects=False
            )
            if self._is_waf_response(response):
                self._suspend_external_checks("京东联盟链接触发反爬/验证码")
                return ''
        except Exception as e:
            logging.warning(f"京东联盟链接请求失败: {e}")
            return ''

        location = response.headers.get('Location', '')
        if location:
            return requests.compat.urljoin(union_url, location)

        match = re.search(r"https://union-click\.jd\.com/jda\?[^'\"<>]+", response.text)
        if not match:
            return ''

        jump_url = html.unescape(match.group(0))
        if 'h5st=' not in jump_url:
            jump_url = f"{jump_url}&h5st={self._js_hash_code(self.session.headers.get('User-Agent', ''))}"

        try:
            self._throttle_external_request()
            response = self.session.get(
                jump_url,
                headers={
                    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
                    'Referer': 'https://www.smzdm.com/',
                },
                timeout=CONFIG['timeout'],
                allow_redirects=False
            )
            if self._is_waf_response(response):
                self._suspend_external_checks("京东联盟二跳触发反爬/验证码")
                return ''
        except Exception as e:
            logging.warning(f"京东联盟二跳请求失败: {e}")
            return ''

        location = response.headers.get('Location', '')
        if location:
            return requests.compat.urljoin(jump_url, location)
        return ''

    @staticmethod
    def _js_hash_code(value):
        result = 0
        for char in value:
            result = ((result << 5) - result + ord(char)) & 0xffffffff
        if result >= 0x80000000:
            result -= 0x100000000
        return result

    @staticmethod
    def _extract_jd_is_self(html_text):
        if re.search(r'''["']?\bisSelf\b["']?\s*:\s*true\b''', html_text):
            return True
        if re.search(r'''["']?\bisSelf\b["']?\s*:\s*false\b''', html_text):
            return False
        if (re.search(r'''(?:title|alt)\s*=\s*["'][^"']*自营[^"']*["']''', html_text)
                or re.search(r'''["'](?:shopName|vender)["']\s*:\s*["'][^"']*自营[^"']*["']''', html_text)):
            return True
        return None

    @staticmethod
    def _extract_jd_mobile_is_self(html_text):
        if re.search(r'''["']?\bisSelf\b["']?\s*:\s*true\b''', html_text):
            return True
        if re.search(r'''["']?\bisSelf\b["']?\s*:\s*false\b''', html_text):
            return False

        shop_names = re.findall(r'''["'](?:shopName|vender)["']\s*:\s*["']([^"']+)["']''', html_text)
        if any('自营' in name for name in shop_names):
            return True
        if re.search(r'''alt\s*=\s*["']自营["']''', html_text):
            return True
        if re.search(r'''["']po["']\s*:\s*false\b''', html_text) and '自营订单' in html_text:
            return True
        if shop_names and re.search(r'''["']po["']\s*:\s*true\b''', html_text):
            return False
        return None

    def _check_comment_user_levels(self, parsed):
        """代表性样本中，仅在等级证据充足时用 vip_level 比例拒绝。"""
        audit = self._candidate_log_context(parsed)
        if self.external_checks_suspended:
            self._mark_comment_level_unavailable(parsed, 'external')
            logging.info(f"[评论等级跳过] {parsed['title'][:40]}... | {audit} 外部校验已熔断")
            return True

        if parsed.get('comments', 0) < CONFIG['comment_level_min_comments']:
            if self._is_early_signal_path(parsed):
                parsed['comment_level_status'] = 'skipped'
                parsed['comment_level_note'] = (
                    f"早期强信号，列表评论{parsed.get('comments', 0)}条，评论可能审核延迟，跳过等级判断"
                )
                parsed.pop('comment_level_stats', None)
                parsed.pop('comment_level_unavailable_reason', None)
                logging.info(
                    f"[早期强信号评论跳过] {parsed['title'][:40]}... | {audit} "
                    f"评论:{parsed.get('comments', 0)} 收藏:{parsed.get('collection', 0)} "
                    f"值:{parsed.get('worthy', 0)} 评分:{parsed.get('composite_score', 0)} "
                    f"值票支持率:{parsed.get('score_rate', 0)}%"
                )
                return True
            self._mark_comment_level_unavailable(parsed, 'low_comments')
            logging.info(
                f"[评论等级跳过] {parsed['title'][:40]}... | {audit} "
                f"列表评论数 {parsed.get('comments', 0)} 不足 {CONFIG['comment_level_min_comments']}"
            )
            return True

        if self.comment_level_checks >= self.comment_level_check_limit:
            self._mark_comment_level_unavailable(parsed, 'budget')
            logging.info(f"[评论等级跳过] {parsed['title'][:40]}... | {audit} 达到本轮评论等级校验上限")
            return True
        self.comment_level_checks += 1

        samples, comment_meta = self._fetch_comment_samples(parsed['id'])
        parsed['comment_module_stats'] = comment_meta
        level_stats = self._build_comment_level_stats(samples)
        coverage = self._build_comment_module_coverage(parsed, comment_meta)
        parsed['comment_module_coverage'] = coverage
        self._upgrade_comment_count_from_module(parsed, coverage)
        self._observe_comment_anomalies(parsed, samples, coverage)
        audit += (
            f" 列表:{coverage['list_count']} 模块声明:{coverage['module_total']} "
            f"返回:{coverage['returned']}/{coverage['expected']} "
            f"覆盖率:{coverage['ratio']:.0%} "
            f"作者排除:{comment_meta.get('author_count', 0)} 非作者:{len(samples)}"
        )

        if not coverage['representative']:
            if coverage['expected'] >= CONFIG['comment_module_undercovered_skip_min_comments']:
                self.stats['total_comment_level_undercovered_skipped'] += 1
                parsed['comment_level_status'] = 'skipped'
                parsed['comment_level_note'] = (
                    f"评论模块仅返回{coverage['returned']}/{coverage['expected']}，跳过等级判断"
                )
                parsed.pop('comment_level_stats', None)
                parsed.pop('comment_level_unavailable_reason', None)
                logging.info(
                    f"[评论模块覆盖不足，跳过等级判断] {parsed['title'][:40]}... | {audit} "
                    f"列表评论:{coverage['list_count']} 模块声明:{coverage['module_total']} "
                    f"返回评论:{coverage['returned']} "
                    f"覆盖率:{coverage['ratio']:.0%} 模块评论:{comment_meta.get('module_total', 0)} "
                    f"作者:{comment_meta.get('author_count', 0)} 非作者:{len(samples)} | "
                    f"评分:{parsed.get('composite_score', 0)} 值票支持率:{parsed.get('score_rate', 0)}%"
                )
                return True

            self._mark_comment_level_unavailable(parsed, 'sample')
            parsed.pop('comment_level_stats', None)
            logging.info(
                f"[评论模块覆盖不足，暂缓] {parsed['title'][:40]}... | {audit} "
                f"列表评论:{coverage['list_count']} 模块声明:{coverage['module_total']} "
                f"返回评论:{coverage['returned']} "
                f"覆盖率:{coverage['ratio']:.0%} 模块评论:{comment_meta.get('module_total', 0)} "
                f"作者:{comment_meta.get('author_count', 0)} 非作者:{len(samples)}"
            )
            return True

        if samples:
            parsed['comment_level_stats'] = level_stats

        # 作者回复可能占满整个模块，但不应让少数非作者反复评论绕过集中度检查。
        # 两条独立高等级样本的成熟候选例外仍在下方处理；集中度只从四条起检查。
        if (len(samples) >= CONFIG['comment_concentration_min_comments']
                and level_stats['unique_users'] < CONFIG['comment_concentration_min_users']):
            parsed['comment_level_status'] = 'rejected'
            parsed['comment_level_note'] = '代表性样本中独立评论用户不足'
            logging.warning(
                f"[评论集中水军过滤] {parsed['title'][:40]}... | {audit} "
                f"独立用户:{level_stats['unique_users']}/{len(samples)}"
            )
            return False

        if (len(samples) >= CONFIG['comment_concentration_min_comments']
                and level_stats['max_user_ratio'] > CONFIG['comment_concentration_max_user_ratio']):
            parsed['comment_level_status'] = 'rejected'
            parsed['comment_level_note'] = '代表性样本中单用户评论过于集中'
            logging.warning(
                f"[评论集中水军过滤] {parsed['title'][:40]}... | {audit} "
                f"最高单用户:{level_stats['max_user_comments']}/{len(samples)}={level_stats['max_user_ratio']:.0%}"
            )
            return False

        if len(samples) < CONFIG['comment_level_min_comments']:
            if parsed.get('comments', 0) >= CONFIG['comment_level_min_comments']:
                if self._is_large_thread_partial_pass(parsed, samples, level_stats, comment_meta):
                    self.stats['total_comment_level_large_thread_allowed'] += 1
                    parsed['comment_level_status'] = 'skipped'
                    parsed['comment_level_note'] = (
                        f"大评论区少样本放行，等级证据不足（模块评论{comment_meta.get('module_total', 0)}）"
                    )
                    parsed.pop('comment_level_unavailable_reason', None)
                    logging.info(
                        f"[评论大区少样本放行，等级证据不足] {parsed['title'][:40]}... | {audit} "
                        f"模块评论:{comment_meta.get('module_total', 0)} 原始样本:{comment_meta.get('all_count', 0)} "
                        f"作者:{comment_meta.get('author_count', 0)} 非作者:{len(samples)} | "
                        f"Lv<={CONFIG['comment_level_low_max']} {level_stats['low']}/{len(samples)} "
                        f"Lv>={CONFIG['comment_level_high_min']} {level_stats['high']} | "
                        f"独立用户:{level_stats['unique_users']}/{len(samples)} | "
                        f"评分:{parsed.get('composite_score', 0)} 值票支持率:{parsed.get('score_rate', 0)}%"
                    )
                    return True

                if self._is_partial_sample_pass(parsed, samples, level_stats):
                    parsed['comment_level_status'] = 'skipped'
                    parsed['comment_level_note'] = '少样本放行，等级证据不足'
                    parsed.pop('comment_level_unavailable_reason', None)
                    logging.info(
                        f"[评论少样本放行，等级证据不足] {parsed['title'][:40]}... | {audit} "
                        f"Lv<={CONFIG['comment_level_low_max']} {level_stats['low']}/{len(samples)}="
                        f"{level_stats['low_ratio']:.0%}, "
                        f"Lv>={CONFIG['comment_level_high_min']} {level_stats['high']} | "
                        f"独立用户:{level_stats['unique_users']}/{len(samples)} | "
                        f"评分:{parsed.get('composite_score', 0)} 值票支持率:{parsed.get('score_rate', 0)}%"
                    )
                    return True

                self._mark_comment_level_unavailable(parsed, 'sample')
                logging.info(
                    f"[评论等级不足，跳过] {parsed['title'][:40]}... | {audit} "
                    f"API评论:{parsed['comments']} 可取评论:{len(samples)} "
                    f"模块评论:{comment_meta.get('module_total', 0)} 原始样本:{comment_meta.get('all_count', 0)} "
                    f"作者:{comment_meta.get('author_count', 0)} "
                    f"Lv<={CONFIG['comment_level_low_max']} {level_stats['low']}/{len(samples)} "
                    f"Lv>={CONFIG['comment_level_high_min']} {level_stats['high']} "
                    f"独立用户:{level_stats['unique_users']}/{len(samples)}"
                )
            return True

        if self._is_large_thread_under_sampled(samples, comment_meta):
            if self._is_large_thread_partial_pass(parsed, samples, level_stats, comment_meta):
                self.stats['total_comment_level_large_thread_allowed'] += 1
                parsed['comment_level_status'] = 'skipped'
                parsed['comment_level_note'] = (
                    f"大评论区少样本放行，等级证据不足（模块评论{comment_meta.get('module_total', 0)}）"
                )
                parsed.pop('comment_level_unavailable_reason', None)
                logging.info(
                    f"[评论大区少样本放行，等级证据不足] {parsed['title'][:40]}... | {audit} "
                    f"模块评论:{comment_meta.get('module_total', 0)} 原始样本:{comment_meta.get('all_count', 0)} "
                    f"作者:{comment_meta.get('author_count', 0)} 非作者:{len(samples)} | "
                    f"Lv<={CONFIG['comment_level_low_max']} {level_stats['low']}/{len(samples)} "
                    f"Lv>={CONFIG['comment_level_high_min']} {level_stats['high']} | "
                    f"独立用户:{level_stats['unique_users']}/{len(samples)} | "
                    f"评分:{parsed.get('composite_score', 0)} 值票支持率:{parsed.get('score_rate', 0)}%"
                )
                return True

            self._mark_comment_level_unavailable(parsed, 'sample')
            logging.info(
                f"[评论大区样本不足，跳过] {parsed['title'][:40]}... | {audit} "
                f"API评论:{parsed['comments']} 模块评论:{comment_meta.get('module_total', 0)} "
                f"原始样本:{comment_meta.get('all_count', 0)} 作者:{comment_meta.get('author_count', 0)} "
                f"非作者:{len(samples)} Lv<={CONFIG['comment_level_low_max']} {level_stats['low']}/{len(samples)} "
                f"Lv>={CONFIG['comment_level_high_min']} {level_stats['high']} "
                f"独立用户:{level_stats['unique_users']}/{len(samples)}"
            )
            return True

        low_count = level_stats['low']
        high_count = level_stats['high']
        low_ratio = level_stats['low_ratio']
        grade_evidence_sufficient = (
            level_stats['level_count'] >= CONFIG['comment_level_min_known_samples']
            and level_stats['known_ratio'] >= CONFIG['comment_level_min_known_ratio']
        )
        audit += (
            f" 等级已知:{level_stats['level_count']}/{len(samples)} "
            f"等级覆盖率:{level_stats['known_ratio']:.0%}"
        )
        level_stats['evidence_sufficient'] = grade_evidence_sufficient

        if grade_evidence_sufficient and low_ratio > CONFIG['comment_level_max_low_ratio']:
            parsed['comment_level_status'] = 'rejected'
            parsed['comment_level_note'] = '代表性样本中低等级占比超过门槛'
            logging.warning(
                f"[评论等级特征过滤] {parsed['title'][:40]}... | {audit} "
                f"Lv<={CONFIG['comment_level_low_max']} {low_count}/{level_stats['level_count']}={low_ratio:.0%}, "
                f"Lv>={CONFIG['comment_level_high_min']} {high_count}"
            )
            return False

        if not grade_evidence_sufficient:
            self.stats['total_comment_level_insufficient_evidence'] = (
                self.stats.get('total_comment_level_insufficient_evidence', 0) + 1
            )
            parsed['comment_level_status'] = 'skipped'
            parsed['comment_level_note'] = (
                f"等级证据不足：等级已知{level_stats['level_count']}/{len(samples)}"
                f"（{level_stats['known_ratio']:.0%}），需至少"
                f"{CONFIG['comment_level_min_known_samples']}条且覆盖"
                f"{CONFIG['comment_level_min_known_ratio']:.0%}；仅作诊断"
            )
            parsed.pop('comment_level_unavailable_reason', None)
            logging.info(
                f"[评论等级证据不足，仅作诊断] {parsed['title'][:40]}... | {audit} "
                f"Lv<={CONFIG['comment_level_low_max']} {low_count}/{level_stats['level_count']}={low_ratio:.0%}, "
                f"Lv>={CONFIG['comment_level_high_min']} {high_count} | "
                f"独立用户:{level_stats['unique_users']}/{len(samples)} "
                f"最高单用户:{level_stats['max_user_comments']}/{len(samples)}"
            )
            return True

        parsed['comment_level_status'] = 'passed'
        parsed.pop('comment_level_note', None)
        parsed.pop('comment_level_unavailable_reason', None)
        logging.info(
            f"[评论等级通过] {parsed['title'][:40]}... | {audit} "
            f"Lv<={CONFIG['comment_level_low_max']} {low_count}/{level_stats['level_count']}={low_ratio:.0%}, "
            f"Lv>={CONFIG['comment_level_high_min']} {high_count} | "
            f"独立用户:{level_stats['unique_users']}/{len(samples)}"
        )
        return True

    def _build_comment_level_stats(self, samples):
        levels = [sample['level'] for sample in samples if sample.get('level') is not None]
        low_count = sum(1 for level in levels if level <= CONFIG['comment_level_low_max'])
        high_count = sum(1 for level in levels if level >= CONFIG['comment_level_high_min'])
        low_ratio = low_count / len(levels) if levels else 0
        user_stats = self._calculate_comment_user_stats(samples)
        return {
            'count': len(samples),
            'level_count': len(levels),
            'known_ratio': len(levels) / len(samples) if samples else 0,
            'low': low_count,
            'high': high_count,
            'low_ratio': low_ratio,
            'unique_users': user_stats['unique_users'],
            'max_user_comments': user_stats['max_user_comments'],
            'max_user_ratio': user_stats['max_user_ratio'],
        }

    @staticmethod
    def _build_comment_module_coverage(parsed, comment_meta):
        list_comments = max(0, int(parsed.get('comments', 0) or 0))
        module_total = max(0, int(comment_meta.get('module_total', 0) or 0))
        expected_comments = max(list_comments, module_total)
        returned_comments = max(0, int(comment_meta.get('all_count', 0) or 0))
        ratio = returned_comments / expected_comments if expected_comments else 0
        tolerance = int(CONFIG.get('comment_module_coverage_tolerance', 0))
        min_ratio = float(CONFIG.get('comment_module_min_coverage_ratio', 1))
        representative = (
            expected_comments > 0
            and returned_comments > 0
            and (
                returned_comments >= expected_comments
                or expected_comments - returned_comments <= tolerance
                or ratio >= min_ratio
            )
        )
        return {
            'listed': expected_comments,
            'expected': expected_comments,
            'list_count': list_comments,
            'module_total': module_total,
            'returned': returned_comments,
            'ratio': ratio,
            'representative': representative,
        }

    def _is_partial_sample_pass(self, parsed, samples, level_stats):
        if parsed.get('quality_path') == '早期好价':
            return False
        if len(samples) < CONFIG['partial_sample_min_samples']:
            return False
        if level_stats['level_count'] < len(samples):
            return False
        if level_stats['high'] < len(samples) or level_stats['low'] > 0:
            return False
        if level_stats['unique_users'] < CONFIG['partial_sample_min_unique_users']:
            return False
        if level_stats['max_user_ratio'] > CONFIG['comment_concentration_max_user_ratio']:
            return False
        if parsed.get('score_rate', 0) < CONFIG['partial_sample_min_score_rate']:
            return False
        return (
            parsed.get('comments', 0) >= CONFIG['partial_sample_min_comments']
            or parsed.get('composite_score', 0) >= CONFIG['partial_sample_min_score']
        )

    def _is_large_thread_partial_pass(self, parsed, samples, level_stats, comment_meta):
        if parsed.get('quality_path') == '早期好价':
            return False
        if comment_meta.get('module_total', 0) < CONFIG['large_thread_min_total']:
            return False
        if len(samples) < CONFIG['large_thread_min_samples']:
            return False
        if level_stats['level_count'] < len(samples):
            return False
        if level_stats['unique_users'] < CONFIG['large_thread_min_unique_users']:
            return False
        if level_stats['low'] > CONFIG['large_thread_max_low_samples']:
            return False
        if level_stats['high'] < CONFIG['large_thread_min_high_samples']:
            return False
        if level_stats['max_user_ratio'] > CONFIG['comment_concentration_max_user_ratio']:
            return False
        if parsed.get('score_rate', 0) < CONFIG['large_thread_min_score_rate']:
            return False
        return (
            parsed.get('comments', 0) >= CONFIG['large_thread_min_comments']
            and parsed.get('composite_score', 0) >= CONFIG['large_thread_min_score']
        )

    def _is_large_thread_under_sampled(self, samples, comment_meta):
        return (
            comment_meta.get('module_total', 0) >= CONFIG['large_thread_min_total']
            and len(samples) < CONFIG['large_thread_representative_min_samples']
        )

    def _mark_comment_level_unavailable(self, parsed, reason):
        parsed['comment_level_status'] = 'unavailable'
        parsed['comment_level_unavailable_reason'] = reason
        self.stats['total_comment_level_unavailable'] += 1

        stat_key = {
            'budget': 'total_comment_level_unavailable_budget',
            'sample': 'total_comment_level_unavailable_sample',
            'low_comments': 'total_comment_level_unavailable_low_comments',
            'external': 'total_comment_level_unavailable_external',
        }.get(reason)
        if stat_key:
            self.stats[stat_key] += 1

    def _should_defer_for_comment_unavailable(self, parsed):
        if parsed.get('comment_level_status') != 'unavailable':
            return False

        pending = self._get_pending_review(parsed)
        pending_count = pending['unavailable_count'] if pending else 0
        quality_path = parsed.get('quality_path')
        reason = parsed.get('comment_level_unavailable_reason', 'unknown')

        if not CONFIG.get('defer_comment_unavailable', True):
            parsed['comment_level_status'] = 'skipped'
            parsed['comment_level_note'] = (
                f"评论等级未确认:{reason}，按互动评分和增长趋势判断"
            )
            logging.info(
                f"[评论等级不可用放行] {parsed['title'][:40]}... | "
                f"路径:{quality_path} 原因:{reason} 评分:{parsed.get('composite_score', 0)} "
                f"增长:{parsed.get('trend_score', 0)} 评论:{parsed.get('comments', 0)} "
                f"收藏:{parsed.get('collection', 0)} 值:{parsed.get('worthy', 0)}"
            )
            return False

        if (reason == 'budget'
                and quality_path in ('均衡热度', '高讨论')
                and self._is_strong_budget_pass(parsed)):
            self.stats['total_comment_level_budget_strong_allowed'] += 1
            logging.info(
                f"[评论预算不足强信号放行] {parsed['title'][:40]}... | "
                f"路径:{quality_path} 评分:{parsed.get('composite_score', 0)} 评论:{parsed.get('comments', 0)}"
            )
            return False

        if reason == 'budget':
            logging.info(
                f"[评论预算不足暂缓] {parsed['title'][:40]}... | "
                f"路径:{quality_path} 评分:{parsed.get('composite_score', 0)} 评论:{parsed.get('comments', 0)}"
            )
            return True

        if self._is_early_signal_path(parsed):
            parsed['comment_level_status'] = 'skipped'
            parsed['comment_level_note'] = (
                f"早期强信号，评论等级未确认:{reason}，按评论审核延迟处理"
            )
            logging.info(
                f"[早期强信号评论放行] {parsed['title'][:40]}... | "
                f"原因:{reason} 评分:{parsed.get('composite_score', 0)} "
                f"评论:{parsed.get('comments', 0)} 收藏:{parsed.get('collection', 0)} "
                f"值:{parsed.get('worthy', 0)} 值票支持率:{parsed.get('score_rate', 0)}%"
            )
            return False

        if quality_path == '早期好价' and CONFIG.get('defer_emerging_when_comment_unavailable'):
            logging.info(
                f"[早期好价暂缓] {parsed['title'][:40]}... | "
                f"评论等级未确认:{reason}，已暂缓 {pending_count} 轮"
            )
            return True

        if (quality_path in ('均衡热度', '高讨论')
                and reason in CONFIG['fallback_allowed_reasons']
                and pending_count >= CONFIG['pending_review_fallback_runs']
                and self._is_comment_fallback_quality_pass(parsed)):
            self.stats['total_comment_level_fallback_allowed'] += 1
            logging.info(
                f"[评论等级兜底放行] {parsed['title'][:40]}... | "
                f"已暂缓 {pending_count} 轮仍不可用，当前路径:{quality_path} 原因:{reason} "
                f"评分:{parsed.get('composite_score', 0)} 评论:{parsed.get('comments', 0)} "
                f"值票支持率:{parsed.get('score_rate', 0)}%"
            )
            return False

        if quality_path in ('均衡热度', '高讨论'):
            logging.info(
                f"[评论等级继续暂缓] {parsed['title'][:40]}... | "
                f"已暂缓 {pending_count} 轮，当前路径:{quality_path}，原因:{reason}"
            )
            return True

        return False

    def _is_strong_budget_pass(self, parsed):
        return (
            parsed.get('composite_score', 0) >= CONFIG['budget_strong_pass_min_score']
            and parsed.get('comments', 0) >= CONFIG['budget_strong_pass_min_comments']
        )

    def _is_comment_fallback_quality_pass(self, parsed):
        return (
            parsed.get('score_rate', 0) >= CONFIG['fallback_min_score_rate']
            and parsed.get('comments', 0) >= CONFIG['fallback_min_comments']
            and parsed.get('composite_score', 0) >= CONFIG['fallback_min_score']
        )

    @staticmethod
    def _is_early_signal_path(parsed):
        return parsed.get('quality_path') == '早期强信号'

    def _fetch_comment_samples(self, article_id):
        empty_meta = {
            'module_total': 0,
            'module_rows': 0,
            'all_count': 0,
            'author_count': 0,
            'non_author_count': 0,
        }
        url = f'https://haojia.m.smzdm.com/detail_modul/user_related_modul?article_id={article_id}'
        try:
            self._throttle_external_request()
            response = self.session.get(
                url,
                headers={
                    'Accept': 'application/json, text/plain, */*',
                    'Referer': f'https://www.smzdm.com/p/{article_id}/',
                },
                timeout=CONFIG['timeout']
            )
            if self._is_waf_response(response):
                self._suspend_external_checks("评论等级接口触发反爬/验证码")
                return [], empty_meta
            if response.status_code != 200:
                logging.warning(f"评论等级接口 HTTP {response.status_code}: {article_id}")
                self._record_comment_request_failure(f"HTTP {response.status_code}")
                return [], empty_meta
            resp_json = response.json()
            self.consecutive_comment_request_failures = 0
        except Exception as e:
            logging.warning(f"评论等级接口请求失败 {article_id}: {e}")
            self._record_comment_request_failure(type(e).__name__)
            return [], empty_meta

        all_samples = []
        # 只读该文章实际展示的评论树。aigc_comment_rows 当前是镜像，
        # comment_parent_id 还可能是父评论引用对象，不能当作新增用户发言。
        hot_comments = self._comment_hot_module(resp_json)
        rows = hot_comments.get('rows', [])
        self._collect_comment_samples(rows, all_samples, include_author=True)
        all_samples = self._dedupe_comment_samples(all_samples)
        author_ids = self._extract_comment_author_ids(resp_json)
        samples = [
            sample for sample in all_samples
            if not self._is_author_comment_sample(sample, author_ids)
        ]
        meta = self._extract_comment_module_meta(resp_json, all_samples, samples, author_ids)
        return samples, meta

    def _collect_comment_samples(self, node, samples, include_author=False):
        if isinstance(node, dict):
            raw_id = node.get('comment_id')
            valid_id = isinstance(raw_id, (str, int)) and not isinstance(raw_id, bool) and str(raw_id).strip() not in ('', '0')
            is_comment = valid_id and ('content' in node or 'vip_level' in node)
            is_author = node.get('display_author') in (True, 1, '1')
            if is_comment and (include_author or not is_author):
                user_id = str(node.get('user_smzdm_id') or '').strip()
                identity_known = bool(user_id and user_id != '0')
                level = None
                try:
                    raw_level = node.get('vip_level')
                    if raw_level is not None and not isinstance(raw_level, bool):
                        level = int(raw_level)
                        if level < 0:
                            level = None
                except (TypeError, ValueError):
                    pass
                samples.append({
                    'level': level,
                    # 昵称不唯一；未知账号以评论 ID 分隔，不能合并为同一人。
                    'user_id': user_id if identity_known else f"comment:{node.get('comment_id')}",
                    'identity_known': identity_known,
                    'comment_id': str(node.get('comment_id', '')),
                    'display_author': is_author,
                    'content': node.get('content') if isinstance(node.get('content'), str) else '',
                    'creation_date': node.get('creation_date') if isinstance(node.get('creation_date'), str) else '',
                })
            # 只深入实际子评论，忽略引用对象和推荐/镜像分支。
            children = [node.get('child_list', [])] if is_comment else node.values()
            for value in children:
                self._collect_comment_samples(value, samples, include_author=include_author)
        elif isinstance(node, list):
            for value in node:
                self._collect_comment_samples(value, samples, include_author=include_author)

    @staticmethod
    def _comment_hot_module(resp_json):
        node = resp_json
        for field in ('data', 'comments', 'hot_comments_b'):
            if not isinstance(node, dict):
                return {}
            node = node.get(field)
        return node if isinstance(node, dict) else {}

    @staticmethod
    def _extract_comment_author_ids(resp_json):
        hot_comments = SmzdmScraper._comment_hot_module(resp_json)
        author_ids = set()
        if isinstance(hot_comments, dict):
            for field in ('author_smzdm_id', 'author_id'):
                author_id = str(hot_comments.get(field) or '').strip()
                if author_id:
                    author_ids.add(author_id)
        return author_ids

    @staticmethod
    def _is_author_comment_sample(sample, author_ids=None):
        if sample.get('display_author'):
            return True
        author_ids = author_ids or set()
        user_id = str(sample.get('user_id') or '').strip()
        return bool(user_id and user_id in author_ids)

    @staticmethod
    def _extract_comment_module_meta(resp_json, all_samples, samples, author_ids=None):
        hot_comments = SmzdmScraper._comment_hot_module(resp_json)
        rows = hot_comments.get('rows') if isinstance(hot_comments, dict) else []
        try:
            module_total = int(hot_comments.get('total') or 0)
        except (TypeError, ValueError):
            module_total = 0
        return {
            'module_total': module_total,
            'module_rows': len(rows) if isinstance(rows, list) else 0,
            'all_count': len(all_samples),
            'author_count': sum(
                1 for sample in all_samples
                if SmzdmScraper._is_author_comment_sample(sample, author_ids)
            ),
            'non_author_count': len(samples),
        }

    @staticmethod
    def _calculate_comment_user_stats(samples):
        counts = {}
        for sample in samples:
            user_id = sample.get('user_id') or sample.get('comment_id') or 'unknown'
            counts[user_id] = counts.get(user_id, 0) + 1
        max_count = max(counts.values()) if counts else 0
        total = len(samples)
        return {
            'unique_users': len(counts),
            'max_user_comments': max_count,
            'max_user_ratio': max_count / total if total else 0,
        }

    @staticmethod
    def _dedupe_comment_samples(samples):
        deduped = []
        seen_ids = set()
        for sample in samples:
            comment_id = sample.get('comment_id')
            if comment_id:
                if comment_id in seen_ids:
                    continue
                seen_ids.add(comment_id)
            deduped.append(sample)
        return deduped

    @staticmethod
    def _comment_time_bounds(value, observed_at):
        """以区间保留相对时间精度，不把“12小时前”伪装成秒级时间。"""
        value = str(value or '').strip()
        if len(value) > 64:
            return None
        match = re.fullmatch(r'(\d+)\s*(秒|分钟|小时)前', value)
        if match:
            unit = {'秒': 1, '分钟': 60, '小时': 3600}[match.group(2)]
            seconds = int(match.group(1)) * unit
            if seconds > 366 * 24 * 3600 or observed_at - seconds - unit < 0:
                return None
            return observed_at - seconds - unit, observed_at - seconds
        if value == '刚刚':
            return observed_at - 60, observed_at
        try:
            # SMZDM 无时区的完整日期按北京时间解释。
            stamp = datetime.strptime(value, '%Y-%m-%d %H:%M:%S').replace(
                tzinfo=timezone(timedelta(hours=8))
            ).timestamp()
            if stamp > observed_at + 60:
                return None
            return stamp, stamp
        except (ValueError, TypeError, OverflowError):
            return None

    @staticmethod
    def _comment_normalized_text(value):
        if not isinstance(value, str):
            return ''
        # 显式引用块不是评论者原创正文；保留数字及标点语义，不做模糊情感判断。
        value = re.sub(r'<blockquote\b[^>]*>.*?</blockquote>', '', value, flags=re.I | re.S)
        value = html.unescape(re.sub(r'<[^>]+>', '', value))
        return re.sub(r'\s+', '', unicodedata.normalize('NFKC', value)).casefold()

    def _observe_comment_anomalies(self, parsed, samples, coverage):
        """复用非作者样本进行影子检测；记录聚合证据，不影响放行、不存正文/用户 ID。"""
        if not CONFIG.get('comment_anomaly_enabled'):
            return
        samples = self._dedupe_comment_samples([
            sample for sample in samples if not self._is_author_comment_sample(sample)
        ])
        groups = {}
        known_users = set()
        for sample in samples:
            user_id = str(sample.get('user_id') or '')
            known = sample.get('identity_known', bool(user_id and not user_id.startswith('comment:')))
            if not known or not user_id or self._is_author_comment_sample(sample):
                continue
            known_users.add(user_id)
            text = self._comment_normalized_text(sample.get('content'))
            if len(text) >= CONFIG['comment_anomaly_min_text_length']:
                groups.setdefault(text, []).append(sample)

        observed_at = time.time()
        evidence = {
            'mode': 'observe', 'sample_count': len(samples),
            'known_users': len(known_users), 'representative': bool(coverage.get('representative')),
            'flags': [], 'duplicate_comments': 0, 'duplicate_users': 0,
            'duplicate_ratio': 0, 'timed_users': 0, 'burst_span_seconds': None,
        }
        if len(samples) >= CONFIG['comment_anomaly_min_samples']:
            for text, group in sorted(groups.items(), key=lambda item: len(item[1]), reverse=True):
                users = {sample['user_id'] for sample in group}
                ratio = len(group) / len(samples)
                if (len(users) < CONFIG['comment_anomaly_min_duplicate_users']
                        or ratio < CONFIG['comment_anomaly_min_duplicate_ratio']):
                    continue
                evidence.update({
                    'flags': ['repeated_long_text'], 'duplicate_comments': len(group),
                    'duplicate_users': len(users), 'duplicate_ratio': round(ratio, 3),
                    'text_hash': hashlib.sha256(text.encode('utf-8')).hexdigest()[:12],
                })
                bounds = [
                    (sample['user_id'], self._comment_time_bounds(sample.get('creation_date'), observed_at))
                    for sample in group
                ]
                bounds = [(user_id, span) for user_id, span in bounds if span is not None]
                evidence['timed_users'] = len({user_id for user_id, _ in bounds})
                # 要求重复组每条都能解释时间，且用整个时间区间的最宽跨度。
                if len(bounds) == len(group):
                    span = max(interval[1] for _, interval in bounds) - min(interval[0] for _, interval in bounds)
                    evidence['burst_span_seconds'] = round(span)
                    if span <= CONFIG['comment_anomaly_burst_seconds']:
                        evidence['flags'].append('duplicate_text_burst')
                break

        parsed['comment_anomaly_stats'] = evidence
        if evidence['flags']:
            self.stats['total_comment_anomaly_observed'] = self.stats.get('total_comment_anomaly_observed', 0) + 1
            if len(evidence['flags']) > 1:
                self.stats['total_comment_anomaly_combined'] = self.stats.get('total_comment_anomaly_combined', 0) + 1
            logging.info(
                f"[评论异常观察] {self._candidate_log_context(parsed)} "
                f"非作者:{len(samples)} 稳定账号:{len(known_users)} "
                f"模块覆盖:{coverage.get('ratio', 0):.0%} "
                f"重复长文本:{evidence['duplicate_comments']}/{len(samples)} "
                f"涉及账号:{evidence['duplicate_users']} 时间跨度上界:{evidence['burst_span_seconds']}秒 "
                f"信号:{','.join(evidence['flags'])} textHash:{evidence['text_hash']} "
                "仅作诊断，未确认水军"
            )

    def _throttle_external_request(self):
        time.sleep(random.uniform(*CONFIG['external_request_delay']))

    def _is_waf_response(self, response):
        if response.status_code in CONFIG['waf_status_codes']:
            return True
        content_type = response.headers.get('content-type', '')
        text = response.text[:5000].lower()
        looks_like_html = 'text/html' in content_type or text.lstrip().startswith(('<!doctype', '<html', '<script'))
        if not looks_like_html:
            return False
        return any(marker.lower() in text for marker in CONFIG['waf_markers'])

    def _suspend_external_checks(self, reason):
        if not self.external_checks_suspended:
            self.external_checks_suspended = True
            self.stats['total_external_checks_suspended'] += 1
            logging.warning(f"[外部校验熔断] {reason}，本轮停止详情/跳转类请求")

    def _record_comment_request_failure(self, reason):
        self.consecutive_comment_request_failures += 1
        limit = max(1, int(CONFIG.get('max_consecutive_comment_failures', 2)))
        if self.consecutive_comment_request_failures >= limit:
            self._suspend_external_checks(
                f"评论等级接口连续 {self.consecutive_comment_request_failures} 次请求失败（{reason}）"
            )

    def _check_shill(self, parsed):
        """保留增长停滞过滤，投票/评论比例只记录异常，不推断账号真伪。"""
        if self._is_growth_stalled(parsed):
            self.stats['total_filtered_trend_stale'] += 1
            trend = parsed.get('trend_metrics') or {}
            logging.warning(
                f"[增长停滞过滤] {parsed['title'][:40]}... | {self._candidate_log_context(parsed)} "
                f"已观察:{trend.get('elapsed_minutes', 0)}分钟 "
                f"增长分:{trend.get('growth_score', 0)} "
                f"评论:{parsed.get('comments', 0)} 收藏:{parsed.get('collection', 0)} "
                f"值:{parsed.get('worthy', 0)}"
            )
            return False

        worthy = parsed['worthy']
        unworthy = parsed['unworthy']
        comments = parsed['comments']
        total_votes = worthy + unworthy

        # 投票数太少，无法判断，放行
        if total_votes < CONFIG['shill_min_votes_for_check']:
            return True

        reasons = []

        # 检查1：值/不值比例异常
        if unworthy == 0:
            wu_ratio = float('inf')
        else:
            wu_ratio = worthy / unworthy

        if wu_ratio > CONFIG['shill_max_worthy_unworthy_ratio']:
            reasons.append(f"值/不值比异常: {worthy}/{unworthy}")

        # 检查2：有票无评论
        if worthy > 0:
            comment_ratio = comments / worthy
            if comment_ratio < CONFIG['shill_min_comment_worthy_ratio']:
                reasons.append(f"评论/值票比过低: {comments}/{worthy}={comment_ratio:.2f}")

        if len(reasons) >= CONFIG['shill_min_flags']:
            self.stats['total_engagement_anomaly_observed'] = self.stats.get('total_engagement_anomaly_observed', 0) + 1
            parsed['engagement_anomaly_flags'] = reasons
            logging.info(
                f"[互动比例异常观察] {parsed['title'][:40]}... | {self._candidate_log_context(parsed)} "
                + " | ".join(reasons) + " | 仅作诊断，未确认水军"
            )

        return True

    @staticmethod
    def _is_growth_stalled(parsed):
        if not CONFIG.get('trend_stale_filter_enabled'):
            return False
        if parsed.get('quality_path') == '超级好价':
            return False
        trend = parsed.get('trend_metrics') or {}
        if trend.get('snapshot_count', 0) <= 0:
            return False
        if trend.get('elapsed_minutes', 0) < CONFIG['trend_stale_min_age_minutes']:
            return False
        if parsed.get('comments', 0) > CONFIG['trend_stale_max_comments']:
            return False
        return (
            trend.get('growth_score', 0) <= CONFIG['trend_stale_max_growth_score']
            and trend.get('recent_growth_score', 0) <= CONFIG['trend_stale_max_growth_score']
        )

    # ==================== 关键词低价提醒 ====================

    def _match_keyword_rule(self, title):
        normalized = normalize_keyword_text(title)
        if not normalized:
            return None
        for rule in self.keyword_rules:
            if any(term and term in normalized for term in rule['normalized_terms']):
                return rule
        return None

    def _observe_keyword_match(self, parsed):
        """记录标题命中关键词的文章；价格、状态与去重在推送前统一判断。"""
        if not self.keyword_rules or not parsed or not parsed.get('id'):
            return None
        rule = self._match_keyword_rule(parsed.get('title'))
        if not rule:
            return None
        article_id = parsed['id']
        if article_id not in self.keyword_candidates:
            # 复制一份，避免热度流程后续写入的趋势/评论字段影响关键词推送。
            candidate = dict(parsed)
            candidate['keyword_rule'] = rule
            self.keyword_candidates[article_id] = candidate
            self.stats['total_keyword_matched'] += 1
            key = rule['keyword']
            self.keyword_match_counts[key] = self.keyword_match_counts.get(key, 0) + 1
        return rule

    def _search_keyword_deals(self):
        """每个关键词/别名只请求一次主列表接口的 keyword 查询，失败不影响主流程。"""
        if not self.keyword_rules or not CONFIG.get('keyword_search_enabled'):
            return
        budget = max(0, int(CONFIG.get('max_keyword_searches_per_run', 0)))
        terms = [term for rule in self.keyword_rules for term in rule['terms']]
        if len(terms) > budget:
            logging.warning(f"关键词查询 {len(terms)} 个，超出单轮上限 {budget}，其余仅依赖主列表命中")
        for term in terms[:budget]:
            self.stats['total_keyword_searches'] += 1
            rows = self._fetch_keyword_rows(term)
            if rows is None:
                self.stats['total_keyword_search_failed'] += 1
                continue
            matched = 0
            for item in rows:
                parsed = self._parse_item(item)
                if not parsed or parsed.get('age_hours', 0) > CONFIG['keyword_max_age_hours']:
                    continue
                parsed['keyword_search_source'] = True
                if self._observe_keyword_match(parsed):
                    matched += 1
            logging.info(f"[关键词查询] {term}: 返回 {len(rows)} 条，标题命中 {matched} 条（{CONFIG['keyword_max_age_hours']} 小时内）")
            time.sleep(random.uniform(*CONFIG['request_delay']))

    def _fetch_keyword_rows(self, term):
        try:
            response = self.session.get(
                'https://api.smzdm.com/v1/list',
                params={'keyword': term, 'limit': CONFIG['keyword_search_limit'], 'offset': 0},
                timeout=CONFIG['timeout'],
            )
            if response.status_code != 200:
                logging.warning(f"[关键词查询失败] {term}: HTTP {response.status_code}")
                return None
            payload = response.json()
            data = payload.get('data') if isinstance(payload, dict) else None
            rows = data.get('rows') if isinstance(data, dict) else None
            if str(payload.get('error_code')) != '0' or not isinstance(rows, list):
                logging.warning(f"[关键词查询失败] {term}: 返回格式异常")
                return None
            return [row for row in rows if isinstance(row, dict)]
        except Exception as e:
            logging.warning(f"[关键词查询失败] {term}: {type(e).__name__}")
            return None

    def _keyword_skip_reason(self, parsed):
        rule = parsed.get('keyword_rule') or {}
        if parsed.get('age_hours', 0) > CONFIG['keyword_max_age_hours']:
            return 'age', None
        if self._is_inactive_deal(parsed):
            self.stats['total_keyword_inactive'] += 1
            return 'inactive', '已售罄/过期'
        if self._is_title_blocked(parsed):
            return 'title', '任务/活动标题'
        price_value = parsed.get('price_value')
        if price_value is None:
            self.stats['total_keyword_no_price'] += 1
            return 'no_price', '价格无法解析'
        if price_value > rule.get('max_price', 0):
            self.stats['total_keyword_over_price'] += 1
            return 'over_price', f"价格 {price_value:g} > {rule.get('max_price', 0):g}"
        if self._is_keyword_history_duplicate(parsed):
            self.stats['total_keyword_duplicates'] += 1
            return 'duplicate', '已推送过'
        return '', None

    def _is_keyword_history_duplicate(self, parsed):
        # 不使用 seen_ids：热度候选在本轮也会加入 seen_ids，但它们尚未推送。
        if self.conn.execute('SELECT 1 FROM history WHERE id = ?', (parsed['id'],)).fetchone():
            return True
        return self._is_send_duplicate(parsed)

    def _process_keyword_candidates(self):
        # 同一关键词同价的多条爆料（如同一杂志被连续重复发帖）每轮只推最新一条。
        pushed_prices = set()
        for parsed in sorted(self.keyword_candidates.values(), key=lambda row: row.get('age_hours', 0)):
            rule = parsed['keyword_rule']
            reason, note = self._keyword_skip_reason(parsed)
            price_key = (rule['keyword'], parsed.get('price_value'))
            if not reason and price_key in pushed_prices:
                self.stats['total_keyword_duplicates'] += 1
                reason, note = 'same_price', '本轮同关键词同价已推送'
            if reason:
                if note:
                    logging.info(
                        f"[关键词跳过:{note}] {rule['keyword']} | {parsed['title'][:40]} | "
                        f"{self._candidate_log_context(parsed)} 价格:{parsed.get('price', '-')}"
                    )
                continue
            parsed['quality_path'] = f"关键词:{rule['keyword']}"
            logging.info(
                f"[关键词命中] {rule['keyword']}≤{rule['max_price']:g} | {parsed['title'][:40]} | "
                f"{self._candidate_log_context(parsed)} 价格:{parsed.get('price', '-')}"
            )
            sent = self._send_notification(parsed)
            if sent or DRY_RUN:
                pushed_prices.add(price_key)
            if sent:
                self._save_history(parsed)
                self.keyword_pushed_ids.add(parsed['id'])
                self.stats['total_sent'] += 1
                self.stats['total_keyword_sent'] += 1
                path = parsed['quality_path']
                self.quality_path_sent_counts[path] = self.quality_path_sent_counts.get(path, 0) + 1

    # ==================== 推送 ====================

    @staticmethod
    def _push_recipient_key():
        """只保存接收人哈希；冷却按企业/应用/接收人隔离，也不属于已推送历史。"""
        target = f"{WECOM_CORPID}:{WECOM_AGENTID}:{WECOM_TOUSER}"
        if (not isinstance(WECOM_TOUSER, str) or not WECOM_TOUSER
                or re.search(r'[\s\x00-\x1f\x7f,;]', WECOM_TOUSER)):
            return ''
        return hashlib.sha256(target.encode('utf-8')).hexdigest()

    def _is_push_keyword_cooling_down(self, article_id):
        recipient_key = self._push_recipient_key()
        article_id = str(article_id or '').strip()
        if not recipient_key or not article_id:
            return False
        return self.conn.execute(
            '''SELECT 1 FROM push_keyword_cooldowns
               WHERE recipient_key = ? AND article_id = ? AND expires_at > ?''',
            (recipient_key, article_id, datetime.utcnow().isoformat(timespec='seconds')),
        ).fetchone() is not None

    def _skip_push_keyword_cooldown(self, data):
        if not self._is_push_keyword_cooling_down(data.get('id')):
            return False
        self.stats['total_push_keyword_cooldown_skipped'] += 1
        logging.info(
            f"[关键词冷却跳过] {self._candidate_log_context(data)} "
            "接收人明确的关键词过滤尚在冷却期，未写入推送历史"
        )
        return True

    def _save_push_keyword_cooldown(self, article_id):
        """旧 WXPusher 关键词过滤冷却。企业微信没有对应的拒绝语义，发送流程不再调用，
        保留以兼容旧缓存数据库中的冷却表和延迟复查排除逻辑。"""
        minutes = max(0, int(CONFIG.get('push_keyword_cooldown_minutes', 45)))
        recipient_key = self._push_recipient_key()
        article_id = str(article_id or '').strip()
        if not minutes or not recipient_key or not article_id:
            return
        expires_at = datetime.utcnow() + timedelta(minutes=minutes)
        self.conn.execute(
            '''INSERT INTO push_keyword_cooldowns (recipient_key, article_id, expires_at)
               VALUES (?, ?, ?)
               ON CONFLICT(recipient_key, article_id) DO UPDATE SET
                   expires_at = excluded.expires_at''',
            (recipient_key, article_id, expires_at.isoformat(timespec='seconds')),
        )
        self.conn.commit()

    # ---------- 企业微信 ----------

    @staticmethod
    def _clip_utf8(value, max_bytes, ellipsis='…'):
        """按 UTF-8 字节截断，不切断多字节字符。"""
        text = str(value or '')
        if len(text.encode('utf-8')) <= max_bytes:
            return text
        budget = max_bytes - len(ellipsis.encode('utf-8'))
        result = []
        used = 0
        for char in text:
            size = len(char.encode('utf-8'))
            if used + size > budget:
                break
            result.append(char)
            used += size
        return ''.join(result).rstrip() + ellipsis

    @staticmethod
    def _format_price_number(value):
        return f"{float(value):.2f}".rstrip('0').rstrip('.')

    def _wecom_redact(self, value):
        if not isinstance(value, (str, int, float)) or isinstance(value, bool):
            return '-'
        text = str(value)
        secrets = [WECOM_SECRET, getattr(self, 'wecom_access_token', ''), WECOM_WEBHOOK_URL]
        webhook_key = self._webhook_key(WECOM_WEBHOOK_URL)
        if webhook_key:
            secrets.append(webhook_key)
        for secret in secrets:
            if secret:
                text = text.replace(secret, '[已隐藏]')
        return re.sub(r'[\r\n\t\x00-\x1f\x7f]', ' ', text)[:160]

    def _build_wecom_title(self, data):
        short_title = self._notification_short_title(data)
        rule = data.get('keyword_rule')
        if rule and data.get('price_value') is not None:
            prefix = f"[关键词:{rule['keyword']}] ¥{self._format_price_number(data['price_value'])}"
        else:
            prefix = self._notification_display_price(data)
        return self._clip_utf8(f"{prefix} {short_title}".strip(), CONFIG['wecom_title_max_bytes'])

    def _build_wecom_description(self, data):
        mall = self._notification_plain_text(data.get('mall')) or '未知平台'
        pub_time = self._notification_plain_text(data.get('pub_time'))
        source_line = f"{mall} · 发布 {pub_time}" if pub_time else mall
        rule = data.get('keyword_rule')
        if rule:
            conditions = '/'.join(self._notification_purchase_conditions(data)) or '未识别额外条件'
            lines = [
                f"{self._notification_display_price(data)}｜{conditions}｜阈值≤{rule['max_price']:g}元",
                self._notification_short_title(data),
                source_line,
                '关键词低价提醒，未经热度筛选；价格与购买条件以原文为准',
            ]
        else:
            metrics = self._current_interaction_metrics(data)
            lines = [
                self._build_notification_summary(data),
                source_line,
                f"{int(data.get('worthy', 0))}值/{int(data.get('unworthy', 0))}不值 · "
                f"{int(data.get('comments', 0))}评 · {int(data.get('collection', 0))}藏 · "
                f"互动分{metrics['composite_score']} · {data.get('quality_path', '综合筛选')}",
            ]
        return self._clip_utf8('\n'.join(line for line in lines if line), CONFIG['wecom_description_max_bytes'])

    def _wecom_article_url(self, data):
        url = self._notification_original_url(data.get('link'))
        if not url:
            article_id = str(data.get('id') or '').strip()
            if article_id.isdigit():
                url = f'https://www.smzdm.com/p/{article_id}/'
        if url and len(url.encode('utf-8')) > CONFIG['wecom_url_max_bytes']:
            return ''
        return url

    def _build_wecom_payload(self, data):
        article = {
            'title': self._build_wecom_title(data),
            'description': self._build_wecom_description(data),
        }
        url = self._wecom_article_url(data)
        if url:
            article['url'] = url
        picurl = self._notification_original_url(data.get('pic'))
        if picurl:
            article['picurl'] = picurl
        agentid = int(WECOM_AGENTID) if str(WECOM_AGENTID).isdigit() else WECOM_AGENTID
        return {
            'touser': WECOM_TOUSER,
            'msgtype': 'news',
            'agentid': agentid,
            'news': {'articles': [article]},
            # 缓存回滚导致同一商品重复发送时，由企业微信在 30 分钟内去重。
            'enable_duplicate_check': 1,
            'duplicate_check_interval': 1800,
        }

    @staticmethod
    def _webhook_key(url):
        match = re.search(r'[?&]key=([^&#]+)', str(url or ''))
        return match.group(1) if match else ''

    @classmethod
    def _mask_webhook_url(cls, url):
        """日志里只保留 key 前 4 位，避免泄露 Webhook。"""
        url = str(url or '')
        key = cls._webhook_key(url)
        if key:
            return url.split('?', 1)[0] + f'?key={key[:4]}***'
        return url.split('?', 1)[0] + '?***' if url else '-'

    def _build_wecom_webhook_payload(self, data):
        """群机器人 news：沿用应用消息的 title/description/url/picurl 与字节上限。"""
        article = self._build_wecom_payload(data)['news']['articles'][0]
        return {'msgtype': 'news', 'news': {'articles': [article]}}

    def _pace_webhook_send(self):
        interval = float(CONFIG.get('wecom_webhook_min_interval', 0) or 0)
        last = getattr(self, 'wecom_webhook_last_send', None)
        if interval > 0 and last is not None:
            wait = interval - (time.monotonic() - last)
            if wait > 0:
                time.sleep(wait)
        self.wecom_webhook_last_send = time.monotonic()

    def _post_wecom_webhook(self, payload):
        """返回 (result_dict, 失败原因, 是否结果未知)。45009 超频时等待后只重试一次。"""
        result = None
        for attempt in range(2):
            self._pace_webhook_send()
            try:
                response = requests.post(WECOM_WEBHOOK_URL, json=payload, timeout=10)
            except requests.Timeout:
                return None, 'Webhook POST 超时，未即时重试；服务端是否接受未知', True
            if response.status_code != 200:
                return None, f'Webhook HTTP {response.status_code}', False
            result = response.json()
            if not isinstance(result, dict):
                return None, 'Webhook 返回的 JSON 不是对象', False
            if attempt == 0 and result.get('errcode') in CONFIG['wecom_webhook_rate_limit_codes']:
                wait = float(CONFIG.get('wecom_webhook_rate_limit_wait', 60))
                logging.warning(f"群机器人超频 errcode:{self._wecom_redact(result.get('errcode'))}，等待 {wait:g} 秒后重试一次")
                time.sleep(wait)
                continue
            break
        return result, '', False

    def _get_wecom_access_token(self, force_refresh=False):
        """单轮运行内缓存 access_token；失败返回 (None, 原因)。"""
        cached = getattr(self, 'wecom_access_token', '')
        if cached and not force_refresh:
            return cached, ''
        self.wecom_access_token = ''
        try:
            response = requests.get(
                f'{WECOM_API_BASE}/gettoken',
                params={'corpid': WECOM_CORPID, 'corpsecret': WECOM_SECRET},
                timeout=10,
            )
            if response.status_code != 200:
                return None, f'gettoken HTTP {response.status_code}'
            result = response.json()
        except requests.Timeout:
            return None, 'gettoken 超时'
        except ValueError:
            return None, 'gettoken JSON 无法解析'
        except Exception as e:
            return None, f'gettoken 异常类型:{type(e).__name__}'
        if not isinstance(result, dict):
            return None, 'gettoken 返回不是对象'
        token = result.get('access_token')
        if result.get('errcode') != 0 or not isinstance(token, str) or not token:
            return None, (f"gettoken errcode:{self._wecom_redact(result.get('errcode'))} "
                          f"errmsg:{self._wecom_redact(result.get('errmsg'))}")
        self.wecom_access_token = token
        return token, ''

    def _send_notification(self, data):
        """企业微信 errcode=0 才视为成功并允许写入历史。"""
        data.pop('push_send_record_id', None)

        def failed(reason, unknown=False):
            self.stats['total_push_failed'] += 1
            if unknown:
                self.stats['total_push_unknown'] += 1
            logging.error(f"[推送{'结果未知' if unknown else '失败'}] {self._candidate_log_context(data)} {reason}")
            return False

        if (not isinstance(WECOM_TOUSER, str) or not WECOM_TOUSER
                or re.search(r'[\s\x00-\x1f\x7f,;]', WECOM_TOUSER)):
            return failed('接收人配置为空或格式无效（多人请用 | 分隔）')
        if self._skip_push_keyword_cooldown(data):
            return False

        try:
            use_webhook = bool(WECOM_WEBHOOK_URL)
            payload = (self._build_wecom_webhook_payload(data) if use_webhook
                       else self._build_wecom_payload(data))
            if DRY_RUN:
                self.stats['total_push_dry_run'] += 1
                logging.info(
                    f"[DRY_RUN 推送载荷] {self._candidate_log_context(data)} "
                    + json.dumps(payload, ensure_ascii=False)
                )
                return False
            if use_webhook:
                result, reason, unknown = self._post_wecom_webhook(payload)
                if result is None:
                    return failed(reason, unknown=unknown)
                errcode = result.get('errcode')
                delivery_note = (
                    f"群机器人 errcode:{self._wecom_redact(errcode)} errmsg:{self._wecom_redact(result.get('errmsg'))}"
                )
                if errcode != 0 or isinstance(errcode, bool):
                    return failed(delivery_note)
                data['push_send_record_id'] = ''
                metrics = self._current_interaction_metrics(data)
                logging.info(
                    f"[推送成功] {data['title'][:40]}... | "
                    f"{self._candidate_log_context(data)} "
                    f"路径:{data.get('quality_path', '未知')} "
                    f"价格:{data.get('price', '-')} "
                    f"评分:{metrics['composite_score']} | {delivery_note}"
                )
                return True
            if not (WECOM_CORPID and WECOM_AGENTID and WECOM_SECRET):
                return failed('企业微信配置不完整')

            result = None
            for attempt in range(2):
                token, reason = self._get_wecom_access_token(force_refresh=attempt > 0)
                if not token:
                    return failed(reason)
                response = requests.post(
                    f'{WECOM_API_BASE}/message/send',
                    params={'access_token': token},
                    json=payload,
                    timeout=10,
                )
                if response.status_code != 200:
                    return failed(f'HTTP {response.status_code}')
                result = response.json()
                if not isinstance(result, dict):
                    return failed('接口返回的 JSON 不是对象')
                # 令牌失效时消息未被接受，刷新后只重试一次。
                if attempt == 0 and result.get('errcode') in CONFIG['wecom_token_error_codes']:
                    logging.warning(f"企业微信 access_token 失效 errcode:{self._wecom_redact(result.get('errcode'))}，刷新后重试一次")
                    continue
                break

            errcode = result.get('errcode')
            delivery_note = (
                f"errcode:{self._wecom_redact(errcode)} errmsg:{self._wecom_redact(result.get('errmsg'))}"
            )
            if errcode != 0 or isinstance(errcode, bool):
                return failed(delivery_note)
            msgid = result.get('msgid')
            record_id = msgid if isinstance(msgid, str) and re.fullmatch(r'[A-Za-z0-9_\-]{1,128}', msgid) else ''
            invalid = [
                key for key in ('invaliduser', 'invalidparty', 'invalidtag', 'unlicenseduser')
                if result.get(key)
            ]
            if invalid:
                delivery_note += ' 部分接收人无效:' + ','.join(invalid)
            data['push_send_record_id'] = record_id
            metrics = self._current_interaction_metrics(data)
            logging.info(
                f"[推送成功] {data['title'][:40]}... | "
                f"{self._candidate_log_context(data)} "
                f"路径:{data.get('quality_path', '未知')} "
                f"价格:{data.get('price', '-')} "
                f"评分:{metrics['composite_score']} "
                f"值票支持率:{metrics['score_rate']}% "
                f"评论:{data.get('comments', 0)} "
                f"收藏:{data.get('collection', 0)} "
                f"值:{data.get('worthy', 0)} "
                f"不值:{data.get('unworthy', 0)} | "
                f"{delivery_note} msgid:{'有' if record_id else '-'}"
            )
            return True
        except requests.Timeout:
            # POST 超时无法判断服务端是否已发送，不立即重试以免重复通知。
            return failed('POST 超时，未即时重试；服务端是否接受未知', unknown=True)
        except ValueError:
            return failed('接口 JSON 无法解析')
        except Exception as e:
            return failed(f'异常类型:{type(e).__name__}')

    @staticmethod
    def _notification_plain_text(value):
        text = re.sub(r'<[^>]*>', '', html.unescape(str(value or '')))
        return re.sub(r'\s+', ' ', text).strip()

    def _notification_short_title(self, data):
        title = self._notification_plain_text(data.get('title')) or '商品名称见详情'
        # 只移除开头的促销标签，保留品牌、型号、规格和商品名中的 Plus 等词。
        prefix = re.compile(
            r'^(?:PLUS会员|88VIP|值友专享|今日必买|每日必抢|百亿补贴|国家补贴|'
            r'大件超省|移动端|淘金币可用|淘金币\d+(?:\.\d+)?元|限地区|凑单品)'
            r'[\s:：,，、]+', re.IGNORECASE,
        )
        while prefix.search(title):
            title = prefix.sub('', title, count=1).strip()
        return title

    def _notification_purchase_conditions(self, data):
        title = self._notification_plain_text(data.get('title'))
        price = self._notification_plain_text(data.get('price'))
        text = f'{title} {price}'
        conditions = []
        # 影响支付/返还的条件放在前面；不把“4件套”误读为需要买4件。
        if re.search(r'返.{0,8}[eE]卡', price):
            conditions.append('含返E卡')
        if re.search(r'返.{0,8}积分', price):
            conditions.append('含返积分')
        quantity = re.search(r'(?:需买|需购|须买|拍)\s*(\d+)\s*件', text)
        if quantity:
            conditions.append(f'需买{quantity.group(1)}件')
        if '88vip' in text.lower():
            conditions.append('88VIP')
        if 'plus会员' in text.lower():
            conditions.append('PLUS会员')
        if re.search(r'国家补贴|国补', text):
            conditions.append('国补条件')
        if '淘金币' in text:
            conditions.append('淘金币')
        if re.search(r'需用券|用券|券后', text):
            conditions.append('需用券')
        if '限地区' in text:
            conditions.append('限地区')
        if '凑单品' in title:
            conditions.append('凑单品')
        if '移动端' in title:
            conditions.append('移动端')
        if '随机免单' in title:
            conditions.append('随机免单活动')
        if re.search(r'(?:评论|晒单|评价).{0,8}(?:送|返|奖励)', text):
            conditions.append('晒单/评论奖励')
        return conditions

    def _notification_display_price(self, data):
        raw_price = self._notification_plain_text(data.get('price'))
        number = r'\d+(?:,\d{3})*(?:\.\d+)?'
        match = re.match(
            rf'^(?:低至|折合|折算|约)?\s*(?:{number}(?:\s*元)?\s*[-~—至]\s*{number}\s*元|{number}\s*元)(?:\s*起)?',
            raw_price,
        )
        if not match:
            if not raw_price or raw_price == '未知':
                return '价格见详情'
            return raw_price if len(raw_price) <= 40 else '复杂价格见详情'
        price = match.group(0).replace(' ', '')
        # 不能把“返100元E卡后194元”中的返还额当售价；仅简化明确的起始售价。
        if raw_price[match.end():].startswith(('/', '每')):
            return raw_price if len(raw_price) <= 40 else '复杂价格见详情'
        if (not price.startswith(('折算', '折合'))
                and re.search(r'返.{0,20}(?:积分|[eE]卡).{0,8}后', raw_price)):
            return f'折算{price}'
        return price

    def _notification_reasons(self, data):
        """只描述可核对的信号，不用互动分数宣称低价或商品质量。"""
        reasons = []
        price_drop_note = self._notification_plain_text(data.get('price_drop_note'))
        if price_drop_note:
            reasons.append(price_drop_note)
        trend = data.get('trend_metrics') or {}
        if trend.get('snapshot_count', 0) > 0:
            if (trend.get('recent_minutes', 0) <= CONFIG['warming_recent_max_minutes']
                    and trend.get('recent_growth_score', 0) > 0):
                minutes = int(trend.get('recent_minutes', 0))
                worthy = int(trend.get('recent_delta_worthy', 0))
                collection = int(trend.get('recent_delta_collection', 0))
            elif (trend.get('elapsed_minutes', 0) <= CONFIG['trend_display_recent_after_minutes']
                    and trend.get('growth_score', 0) > 0):
                minutes = int(trend.get('elapsed_minutes', 0))
                worthy = int(trend.get('delta_worthy', 0))
                collection = int(trend.get('delta_collection', 0))
            else:
                worthy = collection = 0
                minutes = 0
            growth = []
            if worthy > 0:
                growth.append(f'新增{worthy}个值票')
            if collection > 0:
                growth.append(f'{collection}个收藏')
            if growth:
                reasons.append(f'{max(1, minutes)}分钟内' + '、'.join(growth))
        if data.get('rank_position'):
            reasons.append(
                f"{int(data.get('rank_window_hours') or 12)}小时热榜第{int(data['rank_position'])}名"
            )
        if not reasons:
            reasons.append(f"{data.get('worthy', 0)}个值票、{data.get('comments', 0)}条评论")
        return reasons

    def _build_notification_summary(self, data):
        """WXPusher 最多100字符；价格和购买门槛在可能被截断的商品名前。"""
        price = self._notification_display_price(data)
        conditions = self._notification_purchase_conditions(data)
        compact_conditions = {
            '含返E卡': '返E卡', '含返积分': '返积分', '国补条件': '国补',
            'PLUS会员': 'PLUS', '需用券': '券',
            '随机免单活动': '随机免单', '晒单/评论奖励': '评论奖励',
        }

        def compact_condition(value):
            compact = compact_conditions.get(value, value.replace('需买', '需'))
            if value.startswith('需买') and len(compact) > 12:
                return '数量见原文'
            return compact

        condition_text = '/'.join(compact_condition(value) for value in conditions)
        reason = self._notification_reasons(data)[0]
        reason = re.sub(r'较历史(?:同 SKU|相似商品)推送低', '较此前推送低', reason)
        for original, short in (
            ('分钟内新增', '分钟增'), ('个值票', '值'), ('个收藏', '藏'), ('条评论', '评'),
        ):
            reason = reason.replace(original, short)
        price_note = '；价差未核实' if not data.get('price_drop_note') else ''
        prefix = '｜'.join(part for part in (price, condition_text) if part)
        if len(prefix) > 100 - 8 - 2 - len(price_note):
            # 极端复杂价格仍在详情保留原文，摘要不能截掉商品及未核价提示。
            prefix = '｜'.join(part for part in ('复杂价格见详情', condition_text) if part)
        reason_budget = max(0, min(36 - len(price_note), 100 - len(prefix) - 8 - 2 - len(price_note)))
        reason = reason[:reason_budget] + price_note
        title_budget = max(8, min(24, 100 - len(prefix) - len(reason) - 2))
        title = self._notification_short_title(data)
        if len(title) > title_budget:
            title = title[:title_budget - 1] + '…'
        return f'{prefix}｜{title}｜{reason}'[:100]

    @staticmethod
    def _notification_original_url(value):
        """原文入口仅复用可用的 HTTP(S) 链接，不猜第三方 App Scheme。"""
        if not isinstance(value, str):
            return ''
        text = value.strip()
        if len(text) > 1000 or re.search(r'[\s\\<>"\x27\x00-\x1f\x7f]', text):
            return ''
        try:
            parts = urlsplit(text)
            if (parts.scheme.lower() not in ('http', 'https') or not parts.hostname
                    or parts.username is not None or parts.password is not None):
                return ''
            parts.port  # 非法端口同样不是可用 URL。
        except ValueError:
            return ''
        return text

    @staticmethod
    def _notification_safe_url(value):
        return html.escape(SmzdmScraper._notification_original_url(value), quote=True)

    def _build_notification_content(self, data):
        """购物决策信息在前，内部筛选诊断在后。"""
        escape = lambda value: html.escape(str(value), quote=True)
        title = escape(self._notification_short_title(data))
        price = escape(self._notification_display_price(data))
        raw_price = self._notification_plain_text(data.get('price'))
        conditions = self._notification_purchase_conditions(data)
        conditions_line = ''
        if conditions:
            conditions_line = (
                '<div style="margin-top:8px;color:#b45309;">购买条件：'
                + escape(' / '.join(conditions)) + '</div>'
            )
        raw_price_line = ''
        if raw_price and raw_price != self._notification_display_price(data):
            raw_price_line = (
                '<div style="margin-top:6px;">原文价格：' + escape(raw_price) + '</div>'
            )
        if any(condition in conditions for condition in ('含返积分', '含返E卡')):
            raw_price_line += '<div style="color:#777;">涉及返还条件，实际支付和返还请分别核对。</div>'
        reasons = ''.join('<li>' + escape(reason) + '</li>'
                          for reason in self._notification_reasons(data))
        if data.get('price_drop_note'):
            price_reference = '价格对照来自本监控此前推送，购买条件可能不同，请核对原文。'
        else:
            price_reference = '尚无可比历史价，价格优势待核实。'
        metrics = self._current_interaction_metrics(data)
        worthy = int(data.get('worthy', 0))
        unworthy = int(data.get('unworthy', 0))
        votes = worthy + unworthy
        vote_line = f'{worthy} 值 / {unworthy} 不值'
        if votes:
            vote_line += f"（值票支持率 {metrics['score_rate']}%，共 {votes} 票）"
        else:
            vote_line += '（暂无投票）'
        link = self._notification_safe_url(data.get('link'))
        link_line = (
            f'<div style="margin:16px 0;"><a href="{link}" '
            'style="font-size:17px;font-weight:bold;">查看好价与购买方法 →</a></div>'
        ) if link else '<div style="margin:16px 0;">购买链接暂不可用</div>'
        copy_style = 'display:inline-block;border:1px solid #888;border-radius:6px;padding:4px 10px;margin-right:8px;'
        copy_buttons = []
        if self._notification_plain_text(data.get('title')):
            copy_buttons.append(
                f'<copy style="{copy_style}" data-clipboard-text="{title}">复制商品名</copy>'
            )
        if link:
            copy_buttons.append(
                f'<copy style="{copy_style}" data-clipboard-text="{link}">复制原文链接</copy>'
            )
        copy_line = '<div style="margin-bottom:16px;">' + ' '.join(copy_buttons) + '</div>' if copy_buttons else ''
        coverage = data.get('comment_module_coverage') or {}
        diagnostics = [
            f"互动分 {metrics['composite_score']} · 路径 {data.get('quality_path', '综合筛选')}"
        ]
        if coverage.get('expected'):
            returned = int(coverage.get('returned') or 0)
            expected = int(coverage['expected'])
            status = '覆盖足够' if coverage.get('representative') else '覆盖不足，仅作参考'
            diagnostics.append(f'评论样本 {returned}/{expected}，{status}')
        level_note = self._notification_plain_text(data.get('comment_level_note'))
        if level_note:
            level_note = level_note.replace('评论等级未确认:sample', '评论等级样本不足')
            diagnostics.append(level_note)
        anomaly = data.get('comment_anomaly_stats') or {}
        if anomaly.get('flags'):
            note = '评论样本有重复长文本'
            if 'duplicate_text_burst' in anomaly['flags']:
                note += '及时间集中'
            diagnostics.append(note + '，仅作参考，未确认水军')
        if data.get('engagement_anomaly_flags'):
            diagnostics.append('投票与评论比例异常，仅作参考，未确认水军')
        trend = data.get('trend_metrics') or {}
        if trend.get('snapshot_count', 0):
            diagnostics.append(
                f"本文章已观察 {int(trend.get('elapsed_minutes', 0))} 分钟，"
                f"增长分 {trend.get('growth_score', 0)}"
            )
        diagnostic_lines = '<br>'.join(escape(line) for line in diagnostics)
        return f"""
        <div style="font-size:15px;line-height:1.7;">
          <div style="font-size:21px;font-weight:bold;color:#e62828;">{price}</div>
          <div style="font-size:18px;font-weight:bold;margin-top:8px;">{title}</div>
          <div style="color:#777;margin-top:5px;">{escape(data.get('mall', '未知平台'))} · 发布 {escape(data.get('pub_time', ''))}</div>
          {conditions_line}
          {raw_price_line}
          <hr style="border:none;border-top:1px solid #ddd;margin:16px 0;">
          <div style="font-weight:bold;">这次为什么推送</div>
          <ul style="padding-left:22px;margin:6px 0;">{reasons}</ul>
          <div style="color:#777;">{escape(price_reference)}</div>
          <div style="margin-top:12px;">{escape(vote_line)}<br>
          {int(data.get('comments', 0))} 条评论 · {int(data.get('collection', 0))} 个收藏</div>
          {link_line}
          {copy_line}
          <hr style="border:none;border-top:1px solid #ddd;margin:14px 0;">
          <div style="font-size:12px;color:#888;">筛选记录<br>{diagnostic_lines}</div>
        </div>
        """

    # ==================== 数据库 ====================

    def _is_duplicate(self, article_id):
        if isinstance(article_id, dict):
            parsed = article_id
            if parsed['id'] in self.seen_ids:
                return True
            if self._is_product_key_duplicate(parsed):
                return True
            # 只拦截历史中已成功推送过的相似商品；未推送商品不会写入指纹，后续扫描仍可复评。
            return self._is_fingerprint_duplicate(parsed)
        return article_id in self.seen_ids

    def _is_send_duplicate(self, parsed):
        if self._is_product_key_duplicate(parsed):
            return True
        return self._is_fingerprint_duplicate(parsed)

    def _is_product_key_duplicate(self, parsed):
        product_key = parsed.get('product_key') or self._build_product_key(parsed)
        if product_key and product_key in self.seen_product_keys:
            if self._is_meaningful_price_drop_by_key(
                    parsed, product_key, self.product_key_min_prices, '同 SKU'):
                return False
            self.stats['total_product_key_duplicates'] += 1
            logging.info(f"[同 SKU 重复跳过] {parsed['title'][:40]}... | key:{product_key}")
            return True
        return False

    def _is_fingerprint_duplicate(self, parsed):
        fingerprint = parsed.get('fingerprint')
        if fingerprint and fingerprint in self.seen_fingerprints:
            if self._is_meaningful_price_drop_by_key(
                    parsed, fingerprint, self.fingerprint_min_prices, '相似商品'):
                return False
            self.stats['total_fingerprint_duplicates'] += 1
            logging.info(f"[相似商品重复跳过] {parsed['title'][:40]}... | 指纹:{fingerprint}")
            return True
        return False

    @staticmethod
    def _build_product_key(parsed):
        product_no = str(parsed.get('product_no') or '').strip()
        if not product_no:
            return ''
        mall = str(parsed.get('mall') or '').strip().lower()
        mall_no = str(parsed.get('mall_no') or '').strip()
        mall_part = mall_no or mall or 'unknown'
        return f"{mall_part}:{product_no}".lower()

    def _is_meaningful_price_drop_by_key(self, parsed, dedupe_key, min_price_map, label):
        new_price = parsed.get('price_value')
        old_price = min_price_map.get(dedupe_key)
        if new_price is None or old_price is None or old_price <= 0:
            return False

        drop_amount = old_price - new_price
        drop_percent = drop_amount / old_price * 100
        if (drop_amount >= CONFIG['price_drop_min_amount']
                or drop_percent >= CONFIG['price_drop_min_percent']):
            parsed['price_drop_note'] = f"较历史{label}推送低 {drop_amount:.2f} 元（{drop_percent:.1f}%）"
            logging.info(
                f"[{label}降价放行] {parsed['title'][:40]}... | "
                f"历史:{old_price:.2f} 新价:{new_price:.2f} 降幅:{drop_percent:.1f}%"
            )
            return True
        return False

    def _pending_review_key(self, parsed):
        return parsed.get('product_key') or parsed.get('fingerprint') or f"id:{parsed.get('id')}"

    def _get_pending_review(self, parsed):
        review_key = self._pending_review_key(parsed)
        if not review_key:
            return None

        cursor = self.conn.execute(
            '''
            SELECT unavailable_count, last_quality_path, last_reason
            FROM pending_reviews
            WHERE review_key = ?
            ''',
            (review_key,)
        )
        row = cursor.fetchone()
        if not row:
            return None
        return {
            'unavailable_count': int(row[0] or 0),
            'last_quality_path': row[1],
            'last_reason': row[2],
        }

    def _save_pending_review(self, parsed):
        review_key = self._pending_review_key(parsed)
        if not review_key:
            return

        pending = self._get_pending_review(parsed)
        unavailable_count = (pending['unavailable_count'] if pending else 0) + 1
        reason = parsed.get('comment_level_unavailable_reason', 'unknown')
        self.conn.execute(
            '''
            INSERT INTO pending_reviews (
                review_key, article_id, title, fingerprint, mall,
                unavailable_count, first_seen, last_seen, last_quality_path, last_reason
            )
            VALUES (?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, ?, ?)
            ON CONFLICT(review_key) DO UPDATE SET
                article_id = excluded.article_id,
                title = excluded.title,
                fingerprint = excluded.fingerprint,
                mall = excluded.mall,
                unavailable_count = excluded.unavailable_count,
                last_seen = CURRENT_TIMESTAMP,
                last_quality_path = excluded.last_quality_path,
                last_reason = excluded.last_reason
            ''',
            (
                review_key,
                parsed.get('id'),
                parsed.get('title', '')[:100],
                parsed.get('fingerprint', ''),
                parsed.get('mall', ''),
                unavailable_count,
                parsed.get('quality_path', ''),
                reason,
            )
        )
        self.conn.commit()
        logging.info(
            f"[暂缓复评记录] {parsed['title'][:40]}... | "
            f"key:{review_key[:40]} 次数:{unavailable_count} 原因:{reason}"
        )

    def _clear_pending_review(self, parsed):
        review_key = self._pending_review_key(parsed)
        if not review_key:
            return
        self.conn.execute("DELETE FROM pending_reviews WHERE review_key = ?", (review_key,))
        self.conn.commit()

    def _save_history(self, data):
        try:
            self.conn.execute(
                '''
                INSERT OR REPLACE INTO history (
                    id, title, fingerprint, product_key, mall, price, price_value, send_record_id, last_seen
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                ''',
                (
                    data['id'],
                    data['title'][:100],
                    data.get('fingerprint', ''),
                    data.get('product_key') or self._build_product_key(data),
                    data.get('mall', ''),
                    data.get('price', ''),
                    data.get('price_value'),
                    data.get('push_send_record_id', ''),
                )
            )
            self.conn.commit()
            self.seen_ids.add(data['id'])
            if data.get('fingerprint'):
                self.seen_fingerprints.add(data['fingerprint'])
                price_value = data.get('price_value')
                if price_value is not None:
                    old_price = self.fingerprint_min_prices.get(data['fingerprint'])
                    if old_price is None or price_value < old_price:
                        self.fingerprint_min_prices[data['fingerprint']] = price_value
            product_key = data.get('product_key') or self._build_product_key(data)
            if product_key:
                self.seen_product_keys.add(product_key)
                price_value = data.get('price_value')
                if price_value is not None:
                    old_price = self.product_key_min_prices.get(product_key)
                    if old_price is None or price_value < old_price:
                        self.product_key_min_prices[product_key] = price_value
        except Exception as e:
            logging.error(f"保存历史失败: {e}")

    def _clean_old_records(self):
        cutoff = datetime.now() - timedelta(days=30)
        cursor = self.conn.execute("DELETE FROM history WHERE last_seen < ?", (cutoff,))
        deleted = cursor.rowcount

        pending_cutoff = datetime.now() - timedelta(days=CONFIG['pending_review_keep_days'])
        pending_cursor = self.conn.execute("DELETE FROM pending_reviews WHERE last_seen < ?", (pending_cutoff,))
        pending_deleted = pending_cursor.rowcount

        snapshot_cutoff = datetime.utcnow() - timedelta(days=CONFIG['snapshot_keep_days'])
        snapshot_cursor = self.conn.execute(
            "DELETE FROM candidate_snapshots WHERE captured_at < ?",
            (snapshot_cutoff.isoformat(timespec='seconds'),)
        )
        snapshot_deleted = snapshot_cursor.rowcount
        recheck_cursor = self.conn.execute(
            "DELETE FROM late_recheck_state WHERE last_checked < ?",
            (snapshot_cutoff.isoformat(timespec='seconds'),)
        )
        recheck_deleted = recheck_cursor.rowcount
        cooldown_cursor = self.conn.execute(
            'DELETE FROM push_keyword_cooldowns WHERE expires_at <= ?',
            (datetime.utcnow().isoformat(timespec='seconds'),),
        )
        cooldown_deleted = cooldown_cursor.rowcount

        self.conn.commit()
        if deleted > 0:
            logging.info(f"清理 {deleted} 条过期记录")
        if pending_deleted > 0:
            logging.info(f"清理 {pending_deleted} 条过期待复评记录")
        if snapshot_deleted > 0:
            logging.info(f"清理 {snapshot_deleted} 条过期候选快照")
        if recheck_deleted > 0:
            logging.info(f"清理 {recheck_deleted} 条过期延迟复查状态")
        if cooldown_deleted > 0:
            logging.info(f"清理 {cooldown_deleted} 条到期关键词冷却状态")

    # ==================== 辅助 ====================

    def _print_statistics(self):
        logging.info("=" * 50)
        logging.info(f"扫描完成:")
        logging.info(f"  获取商品: {self.stats['total_fetched']}")
        logging.info(f"  排行榜获取: {self.stats['total_rank_fetched']}")
        logging.info(f"  排行榜合并主列表: {self.stats['total_rank_merged']}")
        logging.info(f"  排行榜候选: {self.stats['total_rank_candidates']}")
        logging.info(f"  排行榜不可用: {self.stats['total_rank_unavailable']}")
        logging.info(f"  排行榜重复跳过: {self.stats['total_rank_duplicates']}")
        logging.info(f"  排行榜标题过滤: {self.stats['total_rank_filtered_title']}")
        logging.info(f"  排行榜评分过滤: {self.stats['total_rank_filtered_stage1']}")
        logging.info(f"  延迟复查符合队列: {self.stats['total_late_recheck_eligible']}")
        logging.info(f"  延迟复查选中: {self.stats['total_late_recheck_selected']}")
        logging.info(f"  延迟复查请求: {self.stats['total_late_recheck_attempted']}")
        logging.info(f"  延迟复查获取: {self.stats['total_late_recheck_fetched']}")
        logging.info(f"  延迟复查候选: {self.stats['total_late_recheck_candidates']}")
        logging.info(f"  延迟复查不可用: {self.stats['total_late_recheck_unavailable']}")
        logging.info(f"  延迟复查评分过滤: {self.stats['total_late_recheck_filtered_stage1']}")
        logging.info(f"  频道元数据补充: {self.stats['total_channel_metadata_enriched']}")
        logging.info(f"  详情评论数更新: {self.stats['total_comment_count_upgraded']}")
        logging.info(f"  候选快照保存: {self.stats['total_candidate_snapshots_saved']}")
        logging.info(f"  有趋势历史候选: {self.stats['total_trend_candidates']}")
        logging.info(f"  重复跳过: {self.stats['total_duplicates']}")
        logging.info(f"  同 SKU 重复: {self.stats['total_product_key_duplicates']}")
        logging.info(f"  相似商品重复: {self.stats['total_fingerprint_duplicates']}")
        logging.info(f"  标题正则过滤: {self.stats['total_filtered_title_pattern']}")
        logging.info(f"  综合评分过滤: {self.stats['total_filtered_stage1']}")
        logging.info(f"  主列表请求失败页: {self.stats['total_page_request_failures']}")
        logging.info(f"  主列表故障提前结束: {self.stats['total_main_scan_aborted']}")
        if self.quality_path_pass_counts:
            path_summary = ", ".join(
                f"{path}:{count}"
                for path, count in sorted(self.quality_path_pass_counts.items())
            )
            logging.info(f"  初筛路径分布: {path_summary}")
        logging.info(f"  等待趋势确认: {self.stats['total_deferred_trend_confirmation']}")
        logging.info(f"  京东非自营过滤: {self.stats['total_filtered_jd_self']}")
        if CONFIG['jd_self_filter_enabled']:
            logging.info(f"  京东自营校验: {self.jd_self_checks}/{self.jd_self_check_limit}")
        else:
            logging.info("  京东自营校验: 关闭")
        logging.info(f"  评论等级/集中度过滤: {self.stats['total_filtered_comment_level']}")
        logging.info(f"  评论等级不可用: {self.stats['total_comment_level_unavailable']}")
        logging.info(f"    预算不足: {self.stats['total_comment_level_unavailable_budget']}")
        logging.info(f"    样本不足: {self.stats['total_comment_level_unavailable_sample']}")
        logging.info(f"    列表评论不足: {self.stats['total_comment_level_unavailable_low_comments']}")
        logging.info(f"    外部熔断: {self.stats['total_comment_level_unavailable_external']}")
        logging.info(f"  评论等级暂缓复评: {self.stats['total_comment_level_deferred']}")
        logging.info(f"  评论等级兜底放行: {self.stats['total_comment_level_fallback_allowed']}")
        logging.info(f"  评论大区少样本放行: {self.stats['total_comment_level_large_thread_allowed']}")
        logging.info(f"  评论模块覆盖不足跳过: {self.stats['total_comment_level_undercovered_skipped']}")
        logging.info(f"  评论等级证据不足仅诊断: {self.stats['total_comment_level_insufficient_evidence']}")
        logging.info(f"  评论长文本重复观察: {self.stats['total_comment_anomaly_observed']}")
        logging.info(f"    同组时间集中观察: {self.stats['total_comment_anomaly_combined']}")
        logging.info(f"  互动比例异常仅诊断: {self.stats['total_engagement_anomaly_observed']}")
        logging.info(f"  评论预算不足强信号放行: {self.stats['total_comment_level_budget_strong_allowed']}")
        logging.info(f"  互动检查拒绝（增长停滞）: {self.stats['total_filtered_shill']}")
        logging.info(f"  增长停滞过滤: {self.stats['total_filtered_trend_stale']}")
        logging.info(f"  增长过低过滤: {self.stats['total_filtered_trend_low_growth']}")
        logging.info(f"  增长过慢过滤: {self.stats['total_filtered_trend_slow']}")
        logging.info(f"  外部校验熔断: {self.stats['total_external_checks_suspended']}")
        logging.info(f"  成功推送: {self.stats['total_sent']}")
        logging.info(f"  推送失败: {self.stats['total_push_failed']}")
        logging.info(f"    POST 超时结果未知: {self.stats['total_push_unknown']}")
        logging.info(f"  DRY_RUN 未发送载荷: {self.stats['total_push_dry_run']}")
        logging.info(f"  关键词查询请求: {self.stats['total_keyword_searches']}（失败 {self.stats['total_keyword_search_failed']}）")
        logging.info(f"  关键词标题命中: {self.stats['total_keyword_matched']}")
        if self.keyword_match_counts:
            logging.info("    " + ", ".join(f"{key}:{count}" for key, count in self.keyword_match_counts.items()))
        logging.info(f"  关键词超价跳过: {self.stats['total_keyword_over_price']}")
        logging.info(f"  关键词无价格跳过: {self.stats['total_keyword_no_price']}")
        logging.info(f"  关键词售罄/过期跳过: {self.stats['total_keyword_inactive']}")
        logging.info(f"  关键词已推送跳过: {self.stats['total_keyword_duplicates']}")
        logging.info(f"  关键词低价推送: {self.stats['total_keyword_sent']}")
        logging.info(f"  关键词冷却候选跳过: {self.stats['total_push_keyword_cooldown_skipped']}")
        if self.quality_path_sent_counts:
            sent_summary = ", ".join(
                f"{path}:{count}"
                for path, count in sorted(self.quality_path_sent_counts.items())
            )
            logging.info(f"  推送路径分布: {sent_summary}")
        logging.info("=" * 50)

    def _cleanup(self):
        if hasattr(self, 'conn'):
            self.conn.close()
        if hasattr(self, 'session'):
            self.session.close()


def main():
    scraper = SmzdmScraper()
    try:
        scraper.run()
    except KeyboardInterrupt:
        logging.warning("用户终止")
    except Exception as e:
        logging.error(f"运行异常: {e}")
        sys.exit(1)
    finally:
        scraper._cleanup()


if __name__ == "__main__":
    main()
