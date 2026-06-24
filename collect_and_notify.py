#!/usr/bin/env python3
"""キーワード検索 → リプライ案生成 → Slack 通知"""

import asyncio
import json
import os
import random
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path
from urllib.parse import quote

JST = timezone(timedelta(hours=9))

import yaml
from playwright.async_api import async_playwright
from slack_sdk import WebClient

from reply import update_github_secret
from slack_sdk.errors import SlackApiError


def load_config(path: str) -> dict:
    with open(path) as f:
        return yaml.safe_load(f)


async def search_tweets(page, keyword: str, max_tweets: int, search_filter: str = "") -> list[dict]:
    q = keyword + " -filter:replies"
    if search_filter:
        q += " " + search_filter
    url = f"https://x.com/search?q={quote(q)}&f=live"
    await page.goto(url, wait_until="domcontentloaded", timeout=60_000)
    await page.wait_for_timeout(random.randint(3_000, 5_000))

    tweets = []
    processed: set[str] = set()
    no_new_streak = 0

    while len(tweets) < max_tweets:
        articles = await page.query_selector_all('article[data-testid="tweet"]')
        prev_count = len(tweets)

        for article in articles:
            if len(tweets) >= max_tweets:
                break
            try:
                link_el = await article.query_selector('a[href*="/status/"]')
                if not link_el:
                    continue
                href = await link_el.get_attribute("href")
                tweet_id = href.split("/status/")[1].split("?")[0]

                if tweet_id in processed:
                    continue
                processed.add(tweet_id)

                username_el = await article.query_selector('[data-testid="User-Name"] a')
                username = ""
                if username_el:
                    u_href = await username_el.get_attribute("href")
                    username = u_href.strip("/")

                display_name = username
                name_container = await article.query_selector('[data-testid="User-Name"]')
                if name_container:
                    full_text = await name_container.inner_text()
                    display_name = full_text.split("\n")[0].strip()

                text_el = await article.query_selector('[data-testid="tweetText"]')
                text = await text_el.inner_text() if text_el else ""

                # 広告・返信ツイートはスキップ
                if not text or "プロモーション" in text:
                    continue
                article_text = await article.inner_text()
                if "Replying to" in article_text or "返信先" in article_text:
                    continue

                time_el = await article.query_selector('time')
                created_at = ""
                if time_el:
                    created_at = await time_el.get_attribute("datetime") or ""

                tweets.append({
                    "tweet_url": f"https://x.com{href}",
                    "username": username,
                    "display_name": display_name,
                    "text": text,
                    "keyword": keyword,
                    "created_at": created_at,
                })
            except Exception:
                pass

        if len(tweets) == prev_count:
            no_new_streak += 1
            if no_new_streak >= 3:
                break
        else:
            no_new_streak = 0

        if len(tweets) >= max_tweets:
            break

        prev_h = await page.evaluate("document.documentElement.scrollHeight")
        await page.evaluate("window.scrollBy(0, 600)")
        await page.wait_for_timeout(2_000)
        new_h = await page.evaluate("document.documentElement.scrollHeight")
        if new_h == prev_h:
            break

    return tweets


def _clean_display_name(name: str) -> str:
    for sep in ["｜", "|", "＠", "@"]:
        if sep in name:
            name = name[:name.index(sep)]
    return name.strip()


_ACHIEVEMENT_KEYWORDS = {"フォロワー達成"}

_GREETING_REPLIES = {
    "X始めました":        "よろしくお願いします！",
    "おはようございます": "おはようございます！",
    "おはよう":           "おはようございます！",
    "おはよー":           "おはようございます！",
    "お疲れ様でした":     "お疲れ様でした！",
    "おつかれさまでした": "お疲れ様でした！",
    "おつかれ":           "お疲れ様です！",
    "こんにちは":         "こんにちは！",
    "こんばんは":         "こんばんは！",
    "おやすみ":           "おやすみなさい！",
    "今日もお疲れ":       "お疲れ様です！",
    "今日疲れた":         "お疲れ様です！",
    "今週の振り返り":     "お疲れ様でした！",
    "頑張った":           "お疲れ様でした！",
    "頑張ります":         "頑張りましょう！",
}

_GREETING_KEYWORDS = set(_GREETING_REPLIES.keys())

# 時間帯ごとの優先キーワード（JST時刻で判定）
_TIME_PRIORITY: dict[str, dict[str, set[str]]] = {
    "morning": {  # 5〜10時
        "high": {"おはようございます", "おはよう", "おはよー"},
        "low":  {"こんにちは", "こんばんは", "おやすみ"},
    },
    "midday": {   # 11〜16時
        "high": {"こんにちは", "フォロワー達成", "頑張ります", "今週の振り返り"},
        "low":  {"おはようございます", "おはよう", "おはよー", "こんばんは", "おやすみ"},
    },
    "night": {    # 17〜4時
        "high": {"こんばんは", "おやすみ", "お疲れ様でした", "おつかれさまでした",
                 "おつかれ", "今日もお疲れ", "今日疲れた", "頑張った"},
        "low":  {"おはようございます", "おはよう", "おはよー", "こんにちは"},
    },
}


def _get_time_bucket() -> str:
    hour = datetime.now(JST).hour
    if 5 <= hour < 11:
        return "morning"
    if 11 <= hour < 17:
        return "midday"
    return "night"


def _keyword_max_tweets(keyword: str, base: int) -> int:
    bucket = _get_time_bucket()
    priority = _TIME_PRIORITY[bucket]
    if keyword in priority["high"]:
        return max(base * 3, 6)
    if keyword in priority["low"]:
        return 0
    return base


def is_today_jst(created_at: str) -> bool:
    if not created_at:
        return True
    try:
        dt = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
        return dt.astimezone(JST).date() == datetime.now(JST).date()
    except Exception:
        return True


def get_replied_tweet_urls(client: WebClient, channel: str, thread_ts: str) -> set[str]:
    replied: set[str] = set()
    cursor = None
    while True:
        kwargs: dict = dict(channel=channel, ts=thread_ts, include_all_metadata=True, limit=100)
        if cursor:
            kwargs["cursor"] = cursor
        try:
            resp = client.conversations_replies(**kwargs)
        except SlackApiError:
            break
        for msg in resp.get("messages", []):
            meta = msg.get("metadata")
            if not meta or meta.get("event_type") != "reply_candidate":
                continue
            reactions = {r["name"] for r in msg.get("reactions", [])}
            if "heavy_check_mark" in reactions:
                replied.add(meta["event_payload"]["tweet_url"])
        if resp.get("has_more"):
            cursor = resp["response_metadata"]["next_cursor"]
        else:
            break
    return replied


def generate_reply(tweet: dict, persona: str) -> str:
    name = _clean_display_name(tweet.get("display_name", tweet["username"]))
    keyword = tweet["keyword"]

    if keyword in _ACHIEVEMENT_KEYWORDS:
        return f"{name}さん、おめでとうございます！"

    greeting = _GREETING_REPLIES.get(keyword, "ありがとうございます！")
    return f"{name}さん、{greeting}"


def send_to_slack(client: WebClient, channel: str, tweet: dict, reply_text: str, thread_ts: str = "", mention_user: str = "") -> None:
    blocks = [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    f"*@{tweet['username']}*  `{tweet['keyword']}`\n"
                    f">{tweet['text']}\n"
                    f"<{tweet['tweet_url']}|ツイートを見る>"
                ),
            },
        },
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"💬 *リプライ案:*\n{reply_text}",
            },
        },
        {
            "type": "context",
            "elements": [
                {"type": "mrkdwn", "text": f"{'<@' + mention_user + '> ' if mention_user else ''}✅ で承認 → 自動リプライ実行　　✔️ がついたら実行済み"}
            ],
        },
        {"type": "divider"},
    ]

    kwargs: dict = dict(
        channel=channel,
        blocks=blocks,
        text=f"@{tweet['username']} へのリプライ案",
        metadata={
            "event_type": "reply_candidate",
            "event_payload": {
                "tweet_url": tweet["tweet_url"],
                "reply_text": reply_text,
            },
        },
    )
    if thread_ts:
        kwargs["thread_ts"] = thread_ts
    client.chat_postMessage(**kwargs)


async def main() -> None:
    config_path = os.environ.get("CONFIG_PATH", "config/knzw.yml")
    config = load_config(config_path)
    account = config.get("account", "knzw")

    cookies_path = Path(f"cookies_{account}.json")
    if not cookies_path.exists():
        print(f"ERROR: {cookies_path} が見つかりません", file=sys.stderr)
        sys.exit(1)

    keywords = config.get("reply_keywords", [])
    max_per_keyword = config.get("max_tweets_per_keyword", 2)
    search_filter = config.get("search_filter", "")
    slack_channel = config.get("slack_channel", "#x-reply-bot")
    slack_thread_ts = config.get("slack_thread_ts", "")
    slack_mention_user = config.get("slack_mention_user", "")
    persona = config.get("persona", "")

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            storage_state=str(cookies_path),
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            viewport={"width": 1280, "height": 900},
        )
        page = await context.new_page()

        await page.goto("https://x.com/home", wait_until="domcontentloaded", timeout=60_000)
        await page.wait_for_timeout(3_000)
        if "/login" in page.url:
            print(f"ERROR: [{account}] セッション切れ", file=sys.stderr)
            sys.exit(1)
        print(f"[{account}] ログインOK")

        all_tweets: list[dict] = []
        for keyword in keywords:
            kw_max = _keyword_max_tweets(keyword, max_per_keyword)
            if kw_max == 0:
                print(f"  [{keyword}] スキップ（時間帯外）")
                continue
            try:
                tweets = await search_tweets(page, keyword, kw_max, search_filter)
                all_tweets.extend(tweets)
                print(f"  [{keyword}] {len(tweets)} 件")
                await asyncio.sleep(random.uniform(5, 10))
            except Exception as e:
                print(f"  ERROR [{keyword}]: {e}", file=sys.stderr)

        # クッキーを保存してから閉じる
        await context.storage_state(path=str(cookies_path))
        await browser.close()

    await update_github_secret(cookies_path, f"X_COOKIES_{account.upper()}")

    # 重複除去
    seen: set[str] = set()
    unique: list[dict] = []
    for t in all_tweets:
        tid = t["tweet_url"].split("/status/")[1] if "/status/" in t["tweet_url"] else t["tweet_url"]
        if tid not in seen:
            seen.add(tid)
            unique.append(t)

    slack_client = WebClient(token=os.environ["SLACK_BOT_TOKEN"])

    # リプライ実行済みツイートを除外（✔️ がついたもの）
    replied_urls: set[str] = set()
    if slack_thread_ts:
        replied_urls = get_replied_tweet_urls(slack_client, slack_channel, slack_thread_ts)
    unique = [t for t in unique if t["tweet_url"] not in replied_urls]
    print(f"  実行済み除外後: {len(unique)} 件")

    # AIイラスト・美少女系を除外（ハッシュタグ含む）
    _EXCLUSION_TERMS = {
        "aiイラスト", "ai画像", "aiart", "ai art", "美少女",
        "#aiイラスト", "#ai画像", "#aiart", "#美少女",
    }
    unique = [
        t for t in unique
        if not any(term in (t.get("text", "") + t.get("display_name", "")).lower() for term in _EXCLUSION_TERMS)
    ]
    print(f"  除外ワードフィルタ後: {len(unique)} 件")

    # 挨拶系は今日の投稿のみ
    filtered = [
        t for t in unique
        if t["keyword"] not in _GREETING_KEYWORDS or is_today_jst(t["created_at"])
    ]
    print(f"  挨拶フィルタ後: {len(filtered)} 件")

    # 営業・エンジニア職を優先、次に投稿日時の新しい順
    _PRIORITY_TERMS = {"営業", "エンジニア", "SE", "営業職", "エンジニア職", "セールス", "sales"}

    def _priority_score(t: dict) -> int:
        haystack = t.get("text", "") + t.get("display_name", "")
        return 1 if any(term in haystack for term in _PRIORITY_TERMS) else 0

    filtered.sort(key=lambda t: (_priority_score(t), t.get("created_at", "")), reverse=True)

    print(f"\n合計 {len(filtered)} 件 → Slack 通知")

    for tweet in filtered:
        try:
            reply_text = generate_reply(tweet, persona)
            send_to_slack(slack_client, slack_channel, tweet, reply_text, slack_thread_ts, slack_mention_user)
            print(f"  送信: @{tweet['username']}")
            await asyncio.sleep(1)
        except SlackApiError as e:
            print(f"  Slack ERROR: {e}", file=sys.stderr)
        except Exception as e:
            print(f"  ERROR: {e}", file=sys.stderr)


if __name__ == "__main__":
    asyncio.run(main())
