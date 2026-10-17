# AGENTS.md

This file gives coding agents the repository-specific context needed to change and validate the project. It reflects the code and workflow as of `2026-10-11`. Treat `smzdm.py` and `.github/workflows/smzdm.yml` as the source of truth when they differ from documentation.

## WeCom Push and Keyword Alerts (2026-10-17)

This section supersedes the WXPusher-specific wording elsewhere in this file (send acceptance, recipient keyword cooldown, `summary`/`url`/HTML `copy` details).

- Push channel: WeCom (企业微信) self-built app, `msgtype: news`, one article per deal. Env: `WECOM_CORPID`, `WECOM_AGENTID`, `WECOM_SECRET`, `WECOM_TOUSER` (default `@all`, multiple users separated by `|`). `access_token` comes from `cgi-bin/gettoken`, is cached for the run, and is refreshed once when `message/send` returns `40001`/`40014`/`42001`. Only `errcode == 0` (integer) is success; it enters `history` with `msgid` stored in `send_record_id`. Other errcodes, malformed JSON, HTTP errors, and token failures never enter history. POST timeouts are unknown outcomes and are not retried. `enable_duplicate_check` (1800 s) is set as a guard against stale-cache duplicates. Never log the secret or access token.
- Article mapping: title = display price + short title (`[关键词:X] ¥price short title` for keyword alerts), clipped to 128 UTF-8 bytes; description = existing `_build_notification_summary` first line + mall/publish time + votes/comments/collection/path (keyword alerts: price/conditions/threshold, short title, mall/time, "未经热度筛选"), clipped to 512 bytes; `url` = validated SMZDM link, falling back to `https://www.smzdm.com/p/{id}/`; `picurl` = validated `article_pic`. `_build_notification_content` (HTML) is retained but not sent.
- `DRY_RUN=1` logs the full payload, makes no WeCom request, and writes no history.
- The WXPusher recipient keyword cooldown is dormant: nothing writes new rows, but the table, the read path, late-recheck exclusion, and cleanup remain for cache compatibility. The recipient key is SHA-256 of `corpid:agentid:touser`.
- Keyword low-price alerts: rules from `SMZDM_KEYWORDS` (non-empty wins) or `keywords.json`; spec `kw[|alias...]:max_price,...`. Titles are matched after NFKC, lowercasing, and stripping whitespace/punctuation. Sources: every main-feed/rank row, plus one `https://api.smzdm.com/v1/list?keyword=...&limit=50` query per keyword/alias per run (hard cap 10 requests, 12-hour age window, `faxian`/`youhui` only). A match is pushed when the parsed price is `<=` the threshold and the deal is active and not a task title. It bypasses stage-1, trend, comment, and shill filters. Unparseable prices are never pushed. Dedupe uses DB history ID plus SKU/fingerprint (`_is_send_duplicate`, not run-local `seen_ids`), and pushes one item per keyword+price per run. Keyword-pushed IDs are skipped by the hot loop; the hot (热度) logic is otherwise unchanged.

## Project Overview

This repository contains a single-run SMZDM (什么值得买) deal monitor. It scans deal feeds, evaluates engagement quality and growth, performs limited comment-sample checks, deduplicates accepted products in SQLite, and sends WeCom (企业微信) image-text `news` notifications, plus keyword low-price alerts.

The main design constraint is asymmetric data quality: list engagement fields are broadly available, while full comment-level distributions and JD store/self-operated fields are not reliably available from GitHub Actions. Do not turn unavailable external data into a hard rejection unless the user explicitly changes that policy.

The user wants broad discovery with a more informative notification list. Do not add a global category blacklist or infer personal interests from the existing pushed items. Engagement shows community attention, not verified price advantage, superior product quality, or a personal purchase recommendation. Do not infer a quality certification from brand names or a low price. Notification formatting reuses existing data without new external requests.

## Commands

Install and run:

```bash
pip install -r requirements.txt
WECOM_CORPID=ww... WECOM_AGENTID=1000003 WECOM_SECRET=xxx python3 smzdm.py
DRY_RUN=1 SMZDM_DB_PATH=/tmp/dry.db python3 smzdm.py  # log payloads only
```

Run the test suite:

```bash
python3 -m unittest -v
```

Optional environment variable:

- `SMZDM_DB_PATH` changes the SQLite path. The default is `smzdm.db`.

The script performs one scan, sends any accepted deals, and exits. Scheduling belongs to GitHub Actions and cron-job.org; do not add a local loop.

## Repository Layout

- `smzdm.py`: application code, configuration, SQLite schema, filtering, network access, and notification formatting.
- `test_smzdm.py`: unit tests for feed handling, title filters, quality paths, trends, comments, deduplication, and push logs.
- `test_trends_titles.py`, `test_notification_format.py`, `test_push_delivery.py`: focused tests for article isolation/title protection, notification display, and recipient delivery-result handling.
- `test_keyword_alerts.py`: keyword rule parsing/matching, price thresholds, dedupe, keyword search, and WeCom payload byte limits. `keywords.json`: default keyword rules.
- `test_push_keyword_cooldown.py`, `test_comment_evidence.py`, `test_comment_anomalies.py`: focused tests for dormant legacy recipient keyword cooldowns, independent grade evidence/concentration gates, and comment parsing/anomaly observations.
- `.github/workflows/smzdm.yml`: GitHub Actions runner and SQLite cache handling.
- `README.md`: current user-facing behavior and deployment documentation.
- `analysis/`: dated historical log reviews. A dated report is evidence from that point in time, not current configuration.

The only runtime dependency is `requests`.

## Data Sources

- Main feed: `https://api.smzdm.com/v1/list?limit=100&offset=N`.
- The main scan reads at most 51 pages, 5100 rows, and six hours of `faxian`/`youhui` items. The live API currently returns no rows beyond offset 5000. At busy times the row cap, not the six-hour cap, determines coverage.
- Ranking supplement: `https://m.smzdm.com/sou/category_rank?page=1&limit=50&hour=N`.
- Ranking windows rotate through 1, 3, and 12 hours in 15-minute slots. Only one ranking request is made per run, and ranking membership never bypasses normal filters.
- Bounded late rechecks use `https://haojia-api.smzdm.com/detail/{article_id}` to refresh active status, price, mall, direct product link, and current interaction counts after a candidate has left both discovery feeds.
- Late rechecks select unsent snapshots no older than 18 hours when they previously had worthy >= 4 plus collection >= 4, worthy >= 6, or comments >= 8. They exclude IDs found by the current main/rank scan, use a backlog-scaled budget of 4–16 requests, wait at least 45 minutes per article, and prioritize never-checked rows nearest the age deadline before repeat checks. The selector overfetches local SQLite rows, retires blocked task titles, and backfills the request budget with valid products without increasing external traffic.
- `faxian/list` and `youhui/list` channel endpoints opportunistically enrich `article_link`, `mall_no`, and `product_no`.
- `tongji_hudong` is preferred for comments, collection, worthy, and unworthy counts, with top-level fields as fallback.

## Filtering Pipeline

The weighted composite score is:

```text
comments * 3 + collection * 2 + worthy
```

Stage 1 has six paths:

- `超级好价`: comments >= 20, worthy >= 20, collection >= 10, score >= 120, score rate >= 90%.
- `均衡热度`: comments >= 6, worthy >= 8, collection >= 5, total engagement >= 25, score >= 65, score rate >= 90%.
- `高讨论`: comments >= 12, worthy >= 8, collection >= 5, total engagement >= 30, score >= 90, score rate >= 85%.
- `早期好价`: worthy >= 4, comments >= 6, collection >= 4, total engagement >= 16, score >= 35, score rate >= 95%.
- `早期强信号`: worthy >= 6, collection >= 8, total engagement >= 16, score >= 28, score rate >= 95%.
- `升温好价`: worthy >= 4, collection >= 4, total engagement >= 15, score >= 32, score rate >= 95%, plus qualifying snapshot growth with at least one new worthy vote.

Balanced, early, and warming paths have one controlled score-rate exception: at least six worthy votes, no more than one unworthy vote, and score rate >= 85%. Every other path threshold still applies. This prevents a single negative vote from vetoing a fast-growing `6:1` sample while retaining the global 85% floor; two negative votes do not receive this exception.

All paths require at least two worthy votes or two comments. Sold-out, timed-out, expired, and ended items are rejected.

There is no broad category or keyword blacklist. The title filter is intentionally narrow and blocks non-product reward/task posts such as variable `入会...京豆` forms, shop follow/add-to-cart/sign-in rewards, reward-day/E-card sequences, store-only titles, livestream rooms, coupon roundups, random-red-packet popups, points-to-Jingdou activities, non-guaranteed lucky bags, and ambiguous campaign pages. It normalizes full-width and decorated digits before matching and runs before snapshot persistence so task posts cannot seed trend or late-recheck state. When changing these expressions, add both positive and negative regression samples; product false positives are a higher-risk failure than allowing one new activity-title variant through.

The store-only rule must preserve titles with concrete product categories, models, or specifications even when they end in `官方旗舰店`. Pair actual product titles from Actions logs with blocked store-entry titles; a flagship-store suffix alone is not sufficient evidence of a task page.

## Trend Logic

`candidate_snapshots` retains two days of candidate interaction history. Growth weights are worthy 3, collection 2, comments 4, and unworthy -2.

Trend queries and run-local snapshot deduplication are isolated by article ID. Two articles for the same SKU/fingerprint have independent engagement counts and must never supply growth to each other. Legacy snapshots keyed by SKU/fingerprint remain readable only for the same article ID. Product push deduplication continues to use SKU/fingerprint; do not couple its key to the trend identity.

- Low-confidence `早期好价` and `早期强信号` candidates normally wait for another observation with meaningful growth and at least one new worthy vote.
- Already mature balanced candidates or highly discussed candidates can bypass this wait using the explicit thresholds in `CONFIG`.
- `升温好价` requires recent growth or sufficiently fast cumulative growth within its configured time window. Comment-only, collection-only, and very old slow growth do not qualify.
- Low-growth checks begin after about 25 minutes for low-comment candidates. Slow-growth checks handle long observation windows from 240 minutes.
- `超级好价` and `高讨论` are exempt from the slow-growth rejection; exact exemption lists are in `CONFIG`.

Filtered and failed-push items are not written to `history`, so later scans can reevaluate them as engagement changes.

Candidates can leave the feed before delayed comments and collections appear. `candidate_snapshots` therefore also seed the bounded late-recheck queue. Refreshed detail rows still pass every normal title, quality, trend, comment, anomaly, and deduplication stage; detail membership is not a bypass. Inactive rows are retired from the queue.

## Comment Checks

Comment samples come from:

```text
https://haojia.m.smzdm.com/detail_modul/user_related_modul?article_id={article_id}
```

This module usually exposes hot/related samples, not the complete comment pagination. Read only `data.comments.hot_comments_b.rows` and actual `child_list` descendants, deduplicate by comment ID, and ignore parent-reference objects and `aigc_comment_rows` mirrors. Keep comments without `vip_level` as unknown-level samples; otherwise module and grade coverage become overstated. Use stable `user_smzdm_id`, never nicknames as account identity; isolate missing identities by comment ID instead of treating them as a single unknown account. The scraper excludes author comments using both `display_author` and module `author_smzdm_id`, records declared totals and sample counts, and may upgrade the list comment count when the module declares a larger total.

Level and concentration checks only become hard filters when the returned sample is representative: roughly 80% coverage or within two comments of the expected total. Module coverage counts all deduplicated returned comments, including authors and unknown levels; grade evidence is computed independently after authors are removed.

- Lv5 and below is a low-level feature; Lv6 and above is a high-level feature. Neither proves fake-account identity, real purchase, or product quality.
- Grade-ratio hard rejection requires at least six known-level non-author samples and known levels covering at least 80% of all non-author samples. Only then is a low-level ratio above 35% rejected.
- Three to five known-level samples, or insufficient known-level coverage, are diagnostic and reported as `skipped`, never `passed`.
- With at least four samples, fewer than three unique users or one user above 50% is rejected.
- Concentration checks remain independent of the six-known-level floor and run before large-thread partial-sample allowances, so author-heavy modules cannot bypass the four-sample checks.
- A representative two-user high-level sample has a narrow mature-deal allowance defined by `CONFIG`; its grade status still indicates insufficient evidence.
- Undercovered, missing, WAF-blocked, or budget-unavailable samples are diagnostic and do not block an interaction/trend-qualified deal by default.
- The dynamic budget checks all normally eligible candidates up to 80 external comment requests per run.

Legacy `pending_reviews` state remains for database compatibility, but unavailable-comment deferral is disabled.

## Anomaly Observations

Reuse already fetched non-author comments; do not add external requests. The new comment detector is shadow-only: at least six samples, the same normalized long text of at least 24 characters, at least four distinct stable user IDs in that group, and that group covering at least 60% of samples produce a repeated-text observation. Remove explicit blockquotes and normalize HTML/Unicode/whitespace while retaining numbers and punctuation; short generic praise does not qualify.

A combined duplicate-text burst additionally requires every comment in that repeated group to have interpretable time bounds and the group's widest possible interval span to be at most 180 seconds. Relative minute/hour timestamps are intervals of that precision; hourly timestamps must not produce a false second-level burst. Missing timing cannot supply burst evidence.

Log aggregate sample/user counts, coverage, interval upper bounds, flags, and a short text hash only. Do not persist or log raw comment text or account IDs. These observations do not reject candidates or assert that shills are confirmed. Existing worthy/unworthy and comment/worthy imbalance checks are also diagnostic only; the separate snapshot growth/staleness filters retain their behavior.

## JD Handling

`jd_self_filter_enabled` is `False`. GitHub-hosted runners often receive empty shells, WAF pages, or redirects without reliable store and `isSelf` fields. JD deals therefore use the same title, engagement, trend, comment, anomaly, and dedup rules as other platforms.

Old JD lookup helpers remain behind the disabled flag. Do not enable hard rejection of unverified JD items without new runner-side evidence and false-negative testing.

## Deduplication

- Only sends accepted for the target UID enter `history`: validate outer WXPusher `success`/`code` and that UID's `code=1000`, then retain `send_record_id`. An accepted asynchronous send task does not prove terminal delivery or user reading.
- Article IDs, platform product keys, and normalized title fingerprints are checked.
- Fingerprints remain active for 30 days. Price text is normalized before fingerprinting.
- The same SKU or fingerprint may be pushed again when the parsed price is at least RMB 5 lower or at least 5% lower than the historical minimum.
- Successful push logs include article ID, source, quality path, parsed price, score rate, engagement counts, target status, and send record ID so Actions runs can be audited precisely.

## Recipient Keyword Cooldown

Only a valid WXPusher outer success/code plus a unique matching target UID with `code=1001` and the observed explicit status `因为用户设置的关键字过滤，不发送` starts a 45-minute cooldown. Store it in `push_keyword_cooldowns`, keyed by SHA-256 of the UID plus article ID; never persist the complete UID, mark the item delivered, or insert it into `history`.

Main/rank discovery continues saving snapshots during cooldown, then skips candidate enrichment, comment checks, and sending for that article. Late-detail selection excludes active cooldowns in SQL before the selection limit and fills the bounded budget with other eligible candidates. Expiry permits normal reevaluation after user keyword changes; cleanup removes expired state. This is per recipient/article, not a global category, keyword, or SKU blacklist.

Explicit user keyword rejection is an INFO audit event with sanitized recipient status/code/send record ID and its own counter, separate from operational push failures. Other rejections, transport failures, malformed/unverified results, and unknown POST outcomes never start this cooldown.

## Notification Contract

- Set a separate WXPusher `summary` (at most 100 characters) with price and explicit purchase conditions before the short product name, followed by a concise evidence-based reason. Strip only leading marketing prefixes. Recognize conditions from title/price, including `88VIP`, `PLUS会员`, required quantities, national subsidies, Tao coins, coupons, regional restrictions, add-on items, mobile-only terms, random-free-order campaigns, and review rewards. Link details to the original article for complete terms.
- Compact summary conditions as `PLUS`, `需N件`, and `券`, retaining full wording in details. Reserve room for the product and `价差未核实`; extreme combined price/condition text uses `复杂价格见详情` instead of clipping away uncertainty, and abnormally long quantities use `数量见原文`. Do not omit material purchase conditions to shorten a normal summary.
- Set the independent WXPusher JSON `url` to the existing valid HTTP(S) article link of at most 1000 characters. Keep raw query parameters in JSON; HTML escaping is only for HTML attributes. Omit unavailable/invalid URLs without blocking the send. Do not invent app schemes or force client notification-click settings.
- Add official HTML `copy` controls for the full cleaned product name and valid original link near the purchase CTA. Escape attribute values, preserve model/specifications and raw URL parameters, and retain the ordinary hyperlink. Browser previews emulate clipboard behavior separately and do not verify native WXPusher operation.
- Label E-card/points rebates as `含返E卡`/`含返积分`; add `折算` only when the original price explicitly describes an amount after rebate. `119.9元（返240积分）` does not establish that points were deducted from the price. Preserve starting-price, range, unit-price, and complex-rebate semantics; retain original price text and ask readers to check payment and rebate separately.
- Lead HTML details with price, product, purchase conditions, and verifiable facts: actual same-article worthy/collection growth, the fetched ranking window/position, or a drop from this project's historical pushed price. Fall back to current worthy/comment counts if none is available. Do not call the historical comparison a market/all-time low.
- Without a historical price-drop note, state `价差未核实` in the summary and that price advantage needs checking in details. Even with a prior-push comparison, purchase conditions may differ. Ranking or passing an internal quality path does not independently establish a good purchase price or product quality.
- Display `score_rate` as `值票支持率`, computed from worthy/(worthy+unworthy). It is not the buyer product-review approval rate; include underlying vote counts.
- Keep a concise diagnostic section after the purchase link: composite score, quality path, article observation window/growth score, comment coverage, and unavailable-level notes. Full comment-level/concentration statistics and trend-baseline details belong in audit logs.
- Escape all external text included in HTML. Keep list summaries readable without requiring the user to open an algorithm report.

Price comparison excludes rebates, coupon thresholds/face values, gifts, and original prices. Startup recalculates stored history price values to correct legacy rebate amounts mistaken for sale prices.

## Network Stability

- GET requests use retry total 2, exponential backoff, and retries for HTTP 500/502/503/504.
- Disable automatic `Retry-After` waits (`respect_retry_after_header=False`); HTTP 429 must reach the existing circuit-breaker handling promptly instead of sleeping outside the bounded run window.
- Connect/read timeouts are 8/20 seconds.
- Main-feed scanning stops after three consecutive failed pages and leaves recovery to the next scheduled run.
- External detail/comment calls use randomized throttling.
- HTTP 202/403/429, captcha markers, `probe.js`, or access-frequency responses suspend external checks for the rest of the run.
- Two consecutive comment or late-detail request failures also open the run-local circuit breaker.
- Late-detail traffic scales at 4% of the eligible backlog with a 4-request floor and 16-request hard cap. It is tracked independently in `late_recheck_state`; failure is diagnostic and does not stop main-feed discovery.
- WXPusher POST is not retried immediately. A timeout is an unknown outcome, is counted separately in send diagnostics, and must not be reported as a confirmed rejection or terminal success. Failed or unknown sends do not enter `history`; a future scan may reevaluate them, so duplicate delivery remains possible.
- Do not add send-status polling: a 2026-10-11 read-only call returned that query status is disabled despite its presence in the docs. That response does not imply the original send failed. Preserve recipient-acceptance accounting and the distinction from terminal arrival.

Filter, comment, trend, and send diagnostics should include article ID and source. Log comment coverage and trend baseline article/time; separate operational send failures, unknown POST outcomes, explicit keyword rejections, cooldown skips, and shadow observations in the run summary. Avoid logging app tokens or full recipient identifiers.

Keep the external request count bounded. New sources should first reuse existing JSON endpoints, have strict timeouts, and degrade without blocking the full scan.

## Deployment

`.github/workflows/smzdm.yml` accepts `workflow_dispatch` and `repository_dispatch` with event type `cron_trigger`. cron-job.org currently sends the external trigger every 15 minutes. There is deliberately no GitHub `schedule`, which avoids double triggering.

The 2026-10-10 review supports keeping this cadence: recent runs averaged about four minutes, 72/100 reached the 5100-row cap, and early paths need timely repeat observations. Do not add immediate recovery dispatches for hosted-runner startup failures; the next scheduled run recovers without extra duplicate traffic. Revisit cadence with new latency, coverage, or rate-limit evidence.

The 2026-10-11 comment/API review is recorded in `analysis/smzdm-api-research-2026-10-11.md`. Earlier dated reports, including the rule proposal, remain historical evidence rather than current configuration. The six interaction paths and trend thresholds remain unchanged by the comment/cooldown changes.

Manual dispatch has an optional `debug_article_id` input. It validates a numeric ID, initializes/migrates the restored database before diagnostic SQL, and prints matching `candidate_snapshots` plus successful-push history for the same ID, SKU, or fingerprint before the scan. Queue diagnostics use the configured UID so recipient cooldown exclusions match the actual scan. Use this instead of guessing when a specific article is reported missing; leaving it blank has no effect on normal runs.

The workflow uses `concurrency: smzdm-scan`, a 12-minute job timeout, Python 3.11, unit tests before scanning, and `actions/cache` for `smzdm.db`. Cache is not a transactional mutable store: closely spaced or retried runs can restore stale state. Do not claim exactly-once delivery without moving state to a more reliable store.

## Change Checklist

For filtering or parsing changes:

1. Confirm behavior against real API or Actions payloads; do not invent response fields.
2. Add focused positive and negative tests, especially for title regex, cross-article trend isolation, target-UID send results, notification summaries, and dedup fingerprints.
3. Run `python3 -m unittest -v` and `git diff --check`.
4. Keep README, AGENTS, and CLAUDE descriptions synchronized when behavior changes.
5. After deployment, review Actions summary counters and concrete successful-push log lines before tuning thresholds again.
