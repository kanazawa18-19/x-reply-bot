#!/usr/bin/env python3
"""キーワード検索 → リプライ案生成 → Slack 通知"""

import asyncio
import json
import os
import random
import sys
from pathlib import Path
from urllib.parse import quote

import anthropic
import yaml
from playwright.async_api import async_playwright
from slack_sdk import WebClient

from reply import update_github_secret
from slack_sdk.errors import SlackApiError


def load_config(path: str) -> dict:
    with open(path) as f:
        return yaml.safe_load(f)


async def search_tweets(page, keyword: str, max_tweets: int) -> list[dict]:
    url = f"https://x.com/search?q={quote(keyword + ' -filter:replies')}&f=live"
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

                text_el = await article.query_selector('[data-testid="tweetText"]')
                text = await text_el.inner_text() if text_el else ""

                # 広告・返信ツイートはスキップ
                if not text or "プロモーション" in text:
                    continue
                article_text = await article.inner_text()
                if "Replying to" in article_text or "返信先" in article_text:
                    continue

                tweets.append({
                    "tweet_url": f"https://x.com{href}",
                    "username": username,
                    "text": text,
                    "keyword": keyword,
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


def generate_reply(tweet: dict, persona: str) -> str:
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

    message = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=80,
        messages=[{
            "role": "user",
            "content": f"""あなたはX(Twitter)ユーザーとして、ごく短いリプライを1つ考えます。

あなたのペルソナ:
{persona}

【ルール】
- 敬語
- 20〜35文字程度。1〜2文で完結
- 自然な共感のみ。アドバイス・指摘・宣伝は一切NG
- 絵文字・ハッシュタグ不要
- 「！」「…」などで温度感を自然に表現する
- 無理に盛り上げない。あくまで自然に
- リプライ文のみ出力
- このツイートは自分に向けられたものではない。投稿者が世界に向けて発信した言葉に、横から同調・共鳴する立場で返す
- 「元気もらえました」「励みになります」など、ツイートが自分に向けられた前提の返し方はしない

【良い例】
- 「おはようございます！今日も頑張りましょう！」
- 「おはようございます…！良い一日になりますように。」
- 「お疲れ様でした！ゆっくり休んでください。」
- 「お疲れ様です…！今日も一日お疲れ様でした！」

【NG例】
- 「本当にそうですね。朝から元気もらえました、頑張ります！」→ツイートが自分に向けられた前提になっているのでNG
- 「素敵な言葉ですね、励みになります！」→ 同上

ツイート(@{tweet['username']}):
{tweet['text']}"""
        }]
    )
    return message.content[0].text.strip()


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
            try:
                tweets = await search_tweets(page, keyword, max_per_keyword)
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

    print(f"\n合計 {len(unique)} 件 → Slack 通知")

    slack_client = WebClient(token=os.environ["SLACK_BOT_TOKEN"])

    for tweet in unique:
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
