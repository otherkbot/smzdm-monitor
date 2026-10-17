# CLAUDE.md

This file provides repository guidance to Claude Code. It reflects the implementation as of `2026-10-11`. `AGENTS.md` contains the complete engineering guide; keep both files aligned when application behavior changes.

## WeCom Push and Keyword Alerts (2026-10-17)

This section supersedes the WXPusher-specific wording elsewhere in this file (send acceptance, recipient keyword cooldown, `summary`/`url`/HTML `copy` details).

- Push channel: WeCom (企业微信) self-built app, `msgtype: news`, one article per deal. Env: `WECOM_CORPID`, `WECOM_AGENTID`, `WECOM_SECRET`, `WECOM_TOUSER` (default `@all`, multiple users separated by `|`). `access_token` comes from `cgi-bin/gettoken`, is cached for the run, and is refreshed once when `message/send` returns `40001`/`40014`/`42001`. Only `errcode == 0` (integer) is success; it enters `history` with `msgid` stored in `send_record_id`. Other errcodes, malformed JSON, HTTP errors, and token failures never enter history. POST timeouts are unknown outcomes and are not retried. `enable_duplicate_check` (1800 s) is set as a guard against stale-cache duplicates. Never log the secret or access token.
- Article mapping: title = display price + short title (`[关键词:X] ¥price short title` for keyword alerts), clipped to 128 UTF-8 bytes; description = existing `_build_notification_summary` first line + mall/publish time + votes/comments/collection/path (keyword alerts: price/conditions/threshold, short title, mall/time, "未经热度筛选"), clipped to 512 bytes; `url` = validated SMZDM link, falling back to `https://www.smzdm.com/p/{id}/`; `picurl` = validated `article_pic`. `_build_notification_content` (HTML) is retained but not sent.
- `DRY_RUN=1` logs the full payload, makes no WeCom request, and writes no history.
- The WXPusher recipient keyword cooldown is dormant: nothing writes new rows, but the table, the read path, late-recheck exclusion, and cleanup remain for cache compatibility. The recipient key is SHA-256 of `corpid:agentid:touser`.
- Keyword low-price alerts: rules from `SMZDM_KEYWORDS` (non-empty wins) or `keywords.json`; spec `kw[|alias...]:max_price,...`. Titles are matched after NFKC, lowercasing, and stripping whitespace/punctuation. Sources: every main-feed/rank row, plus one `https://api.smzdm.com/v1/list?keyword=...&limit=50` query per keyword/alias per run (hard cap 10 requests, 12-hour age window, `faxian`/`youhui` only). A match is pushed when the parsed price is `<=` the threshold and the deal is active and not a task title. It bypasses stage-1, trend, comment, and shill filters. Unparseable prices are never pushed. Dedupe uses DB history ID plus SKU/fingerprint (`_is_send_duplicate`, not run-local `seen_ids`), and pushes one item per keyword+price per run. Keyword-pushed IDs are skipped by the hot loop; the hot (热度) logic is otherwise unchanged.

## Project

SMZDM (什么值得买) deal monitor. `smzdm.py` performs one scan, filters candidates, sends accepted deals through WeCom (企业微信) `news` messages, sends keyword low-price alerts, saves state in SQLite, and exits. Scheduling is external; there is no local polling loop.

The project deliberately treats list engagement and snapshot growth as its primary evidence. SMZDM does not expose a reliable complete distribution of comment-user levels through the endpoint available to GitHub Actions, and JD store/self-operated fields are also unreliable on hosted runners. Missing external data is diagnostic rather than a default rejection.

Keep discovery broad. The user wants price, product, purchase conditions, and a factual reason visible before opening a notification; do not infer a global category blacklist or claim that engagement establishes price advantage or superior quality. Brand names or a low price cannot certify product quality. Formatting reuses existing data without new external requests.

## Commands

```bash
pip install -r requirements.txt
WECOM_CORPID=ww... WECOM_AGENTID=1000003 WECOM_SECRET=xxx python3 smzdm.py
DRY_RUN=1 SMZDM_DB_PATH=/tmp/dry.db python3 smzdm.py  # log payloads only
python3 -m unittest -v
```

`SMZDM_DB_PATH` optionally overrides the default `smzdm.db` path. The sole runtime dependency is `requests`.

## Inputs and Scan Scope

- Main source: `https://api.smzdm.com/v1/list?limit=100&offset=N`.
- Scan cap: 51 pages, 5100 rows, six hours, and only `faxian`/`youhui` channels. At peak volume the API row cap is reached before the time cap.
- Ranking source: one request per run to `https://m.smzdm.com/sou/category_rank`, returning up to 50 rows while 1-hour, 3-hour, and 12-hour windows rotate every 15 minutes. Ranking items still pass all normal filters.
- Late-detail source: 4–16 signed JSON requests per run to `https://haojia-api.smzdm.com/detail/{article_id}`, scaled at 4% of the eligible backlog. It refreshes previously near-threshold, unsent candidates after they leave the feeds, with an 18-hour horizon and a 45-minute per-article interval. Current-feed/rank IDs are excluded.
- `faxian/list` and `youhui/list` enrich `article_link`, `mall_no`, and `product_no` when present.
- `tongji_hudong` supplies the preferred comment, collection, worthy, and unworthy counts.

## Quality Paths

Composite score:

```text
comments * 3 + collection * 2 + worthy
```

- `超级好价`: comments >= 20, worthy >= 20, collection >= 10, score >= 120, score rate >= 90%.
- `均衡热度`: comments >= 6, worthy >= 8, collection >= 5, engagement >= 25, score >= 65, score rate >= 90%.
- `高讨论`: comments >= 12, worthy >= 8, collection >= 5, engagement >= 30, score >= 90, score rate >= 85%.
- `早期好价`: worthy >= 4, comments >= 6, collection >= 4, engagement >= 16, score >= 35, score rate >= 95%.
- `早期强信号`: worthy >= 6, collection >= 8, engagement >= 16, score >= 28, score rate >= 95%.
- `升温好价`: worthy >= 4, collection >= 4, engagement >= 15, score >= 32, score rate >= 95%, plus qualifying snapshot growth with at least one new worthy vote.

Balanced, early, and warming paths tolerate one unworthy vote only when worthy >= 6 and the resulting score rate remains >= 85%. This is a narrow exception to the stricter path rate, not a global threshold reduction; all count, score, and trend requirements remain.

All paths require at least two worthy votes or two comments. Inactive items are rejected.

The script has no broad category blacklist. Its narrow normalized-regex filter targets non-product tasks and reward pages, including variable `入会...京豆`, follow/add-to-cart/sign-in rewards, reward-day/E-card sequences, store-only titles, livestream rooms, coupon roundups, points conversion, popup red packets, ambiguous campaign pages, and the concrete variants found in Actions logs. Title filtering runs before snapshot persistence; late rechecks also retire blocked legacy rows and backfill the bounded request budget. Add paired blocked and allowed regression tests whenever changing title expressions.

Store-only filtering must preserve concrete product categories, models, and specifications in titles ending with `官方旗舰店`; the suffix alone does not identify a task page.

## Trends

`candidate_snapshots` retains two days of interactions. Growth weights are worthy 3, collection 2, comments 4, and unworthy -2.

Trend queries and per-run snapshot deduplication use article ID. Different articles for one SKU/fingerprint cannot share interaction growth. Legacy SKU/fingerprint-keyed snapshots remain available only to their original article ID; SKU/fingerprint still serves product push deduplication.

- Low-confidence early paths wait for meaningful later growth, including at least one new worthy vote, unless current engagement meets a mature/discussion bypass.
- Warming deals require recent or sufficiently fast short-window growth. Comment-only, collection-only, or very old slow growth cannot qualify.
- Low-comment candidates can be filtered for weak growth from about 25 minutes and for slow long-window growth from 240 minutes.
- `超级好价` and `高讨论` retain explicit trend-filter exemptions in `CONFIG`.

Filtered or failed-push items do not enter push history, so they remain eligible for later reevaluation.

Snapshots with worthy >= 4 plus collection >= 4, worthy >= 6, or comments >= 8 can seed the bounded late-recheck queue. Refreshed rows do not bypass any normal filter. Inactive details are retired, and `late_recheck_state` prioritizes never-checked candidates nearest the 18-hour deadline before repeat checks without altering trend history.

## Comments

Comment samples use `https://haojia.m.smzdm.com/detail_modul/user_related_modul?article_id={article_id}`. This is normally a hot/related module, not full pagination. Read actual `hot_comments_b.rows` and `child_list`, deduplicate comment IDs, and exclude parent-reference objects and `aigc_comment_rows` mirrors. Retain missing grades as unknown rather than dropping those comments. Identify accounts by stable `user_smzdm_id`, never by nickname; missing identities remain separated by comment ID. Author comments are excluded using both row flags and module author IDs, and the module total may upgrade the list comment count.

Hard level/concentration decisions require a representative module response: about 80% coverage or no more than two missing comments. Module coverage counts all deduplicated comments, including authors and missing grades; grade coverage separately uses non-author samples.

- Lv5 and below and Lv6 and above are grade features, not proof of fake/real accounts, purchases, or product quality.
- Grade-ratio rejection additionally requires at least six known-level non-author samples and known grades covering at least 80% of non-author samples; reject only then when the low-level ratio exceeds 35%.
- Three to five known levels or poor grade coverage remains diagnostic, with status `skipped` rather than `passed`.
- With at least four samples, reject fewer than three users or one user above 50%.
- Concentration checks are independent of the six-grade floor and run before large-thread partial allowances, including when author comments dominate the module. A narrow two-user mature-deal allowance keeps an insufficient-evidence grade status.
- Undercovered or unavailable data does not block a deal by default.
- The dynamic external-check budget covers all normally eligible candidates up to 80 per run.

`pending_reviews` remains for compatibility, but unavailable-comment deferral is disabled.

Reuse these already fetched samples for shadow comment-anomaly observations, without new requests: at least six non-author samples, identical normalized long text of at least 24 characters, at least four distinct stable user IDs in its group, and at least 60% sample share. Remove explicit quoted blocks; retain numbers/punctuation and do not treat short generic praise as evidence. Add a combined timing signal only when every repeated-group comment has interpretable time bounds and the group's widest possible interval spans no more than 180 seconds. Minute/hour relative timestamps retain minute/hour uncertainty, so hourly wording cannot imply a second-level burst.

Log only aggregate counts, coverage, flags, interval upper bounds, and a short text hash. Do not store or log raw comment text or user IDs. These new signals are diagnostic, not hard rejection or proof of shills. The older worthy/unworthy and comment/worthy imbalance checks also observe only; separate growth/staleness filtering is unchanged.

## JD and Deduplication

JD self-operated hard filtering is disabled (`jd_self_filter_enabled = False`) because hosted runners cannot reliably retrieve store or `isSelf` data. JD follows the same generic quality rules as every other platform.

Only sends accepted for the target UID are recorded: require WXPusher outer `success`/`code` and target UID `code=1000`, and save `send_record_id`. This confirms an accepted asynchronous task, not terminal arrival or reading. Dedup uses article ID, platform product key when available, and normalized title fingerprint. Fingerprints remain active for 30 days. A matching SKU or fingerprint can be pushed again when the parsed price drops by at least RMB 5 or 5% from the previous minimum.

Price comparison excludes rebates, coupon thresholds/face values, gifts, and original prices. Startup recalculates stored history price values to correct legacy rebate amounts mistaken for sale prices.

An explicit WXPusher user keyword rejection starts a separate 45-minute cooldown only after validating the outer success/code, unique target UID, target `code=1001`, and the observed phrase `因为用户设置的关键字过滤，不发送`. Store SHA-256 of UID plus article ID in `push_keyword_cooldowns`, never a complete UID or successful history entry. Main/rank snapshots continue, while candidate enrichment/comments/sending skip that article. Late-detail SQL excludes cooling rows before its limit and fills the existing bounded budget with eligible alternatives. Expiry permits reevaluation and cleanup removes old state; the rule never becomes a global category, keyword, or SKU blacklist.

Keyword rejection logs a sanitized INFO audit and independent count, not an operational send failure. Other failures, unverified/malformed results, and POST timeouts do not create cooldowns.

## Notifications and Diagnostics

Use a separate price-first WXPusher `summary` of at most 100 characters, with purchase conditions before the short product name, followed by a concise factual reason. Strip leading marketing prefixes while preserving recognizable `88VIP`/`PLUS会员`, required quantities, subsidy, Tao-coin, coupon, regional, add-on, mobile-only, random-free-order, and review-reward conditions from title/price.

Summary condition wording is compact (`PLUS`, `需N件`, `券`) while details retain complete terms. Reserve space for the product and unverified-price note; extreme combined quotations show `复杂价格见详情` rather than clipping away uncertainty, and abnormally long quantities show `数量见原文`. Set the independent JSON `url` to the existing valid HTTP(S) article link up to 1000 characters, preserving raw query parameters. Invalid/missing links are omitted without blocking sends; do not invent app schemes or force phone click settings.

Use official HTML `copy` controls beside the purchase CTA for the full cleaned product name and valid original link. Escape attributes and preserve model/specifications and link parameters. Keep an ordinary hyperlink; browser previews emulate clipboard actions separately from native WXPusher support. The WXPusher review and preview are in `analysis/wxpusher-review-2026-10-11.md` and `analysis/notification-preview-2026-10-11.html`.

Label rebates `含返E卡`/`含返积分`; add `折算` only when original wording explicitly gives a price after rebate. Do not assume `119.9元（返240积分）` already subtracts the points. Preserve starting-price, range, unit-price, and complex-rebate semantics, and retain original price text so payment and rebate can be checked separately.

HTML details lead with actual same-article worthy/collection growth, the fetched rank window/position, or a reduction from this project's historical pushed price; fall back to current worthy/comment counts. Historical pushed price is not market/all-time-low evidence. Without a historical price-drop note, the summary says `价差未核实` and details say price advantage needs checking. Prior-push comparisons may also have different purchase conditions.

Display `score_rate` as `值票支持率` (worthy/(worthy+unworthy)), with concrete vote counts. It is not buyer product-review approval. After the purchase link, show concise diagnostics: quality path, composite score, observation/growth scores, comment coverage, and unavailable-level notes. Full user levels/concentration and trend baselines remain in audit logs. Escape external HTML text.

Filter, comment, trend, and send logs include article ID/source. Record comment coverage and trend baseline article/time; summarize operational send failures, unknown POST outcomes, user keyword rejections, cooldown skips, and shadow observations separately from Actions status. Keep app tokens and full recipient identifiers out of logs.

## Stability and Deployment

Requests use 8/20-second connect/read timeouts and two GET retries for transient 500-series responses. Three consecutive main-page failures stop the scan. External requests are randomly throttled; WAF/captcha responses or two consecutive comment/late-detail failures suspend external checks for the rest of that run. Late-detail failures do not stop main-feed discovery and recover on the next scheduled run.

GET retries set `respect_retry_after_header=False`; do not let a 429 Retry-After header hold the run outside its bounded window before the circuit breaker can inspect it. WXPusher POST is not immediately retried. A timeout has unknown outcome and does not enter success history; a later scan may reevaluate it, so duplicate delivery is still possible.

Do not add send-status polling: the documented query endpoint returned disabled in a 2026-10-11 read-only check. This does not mean the original message failed; accepted tasks still do not prove terminal arrival.

`.github/workflows/smzdm.yml` supports manual and `repository_dispatch` (`cron_trigger`) starts. cron-job.org triggers it every 15 minutes; GitHub schedule is intentionally absent. The job uses Python 3.11, `concurrency: smzdm-scan`, a 12-minute timeout, unit tests before scanning, and `actions/cache` for SQLite.

Keep the 15-minute cadence based on the 2026-10-10 review: recent runs averaged about four minutes, 72/100 reached the row cap, and early signals benefit from timely repeat observations. Hosted-runner startup failures recover on the next scheduled run; do not add immediate redispatches without new evidence. See `analysis/smzdm-review-2026-10-10.md` for the dated pre-fix review.

See `analysis/smzdm-api-research-2026-10-11.md` for the current comment/API research. Earlier dated reports and rule proposals remain historical evidence. The six interaction paths and growth thresholds are unchanged by this update.

Manual runs accept optional `debug_article_id`; the workflow initializes/migrates the restored database before printing that article's candidate snapshots, matching successful-push history, and late-recheck queue position. Queue diagnostics use the configured UID so recipient cooldown exclusions match the scan. Use it to investigate missing items from real stored metrics.

`actions/cache` is not transactional state. Closely spaced or retried runs may restore an older database, so the current system cannot promise exactly-once notifications.

## Change Discipline

- Base parser and filter changes on real API payloads or Actions logs.
- Preserve bounded external traffic and graceful degradation when optional sources fail.
- Add focused tests for new title variants, parsing assumptions, trends, and dedup behavior.
- Run `python3 -m unittest -v` and `git diff --check` before committing.
- Update `README.md`, `AGENTS.md`, and this file together when thresholds, sources, scheduling, or notification behavior changes.
