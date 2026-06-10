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
from slack_sdk.errors import SlackApiError


def load_config(path: str) -> dict:
    with open(path) as f:
        return yaml.safe_load(f)


async def search_tweets(page, keyword: str, max_tweets: int) -> list[dict]:
    url = f"https://x.com/search?q={quote(keyword)}&f=live"
    await page.goto(url, wait_until="domcontentloaded", timeout=60_000)
    await page.wait_for_timeout(random.randint(3_000, 5_000))

    tweets = []
    processed: set[str] = set()

    while len(tweets) < max_tweets:
        articles = await page.query_selector_all('article[data-testid="tweet"]')

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

                # 自分のアカウントや広告はスキップ
                if not text or "プロモーション" in text:
                    continue

                tweets.append({
                    "tweet_url": f"https://x.com{href}",
                    "username": username,
                    "text": text,
                    "keyword": keyword,
                })
            except Exception:
                pass

        await page.evaluate("window.scrollBy(0, 600)")
        await page.wait_for_timeout(2_000)

        if len(tweets) >= max_tweets:
            break

    return tweets


def generate_reply(tweet: dict, persona: str) -> str:
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

    message = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=200,
        messages=[{
            "role": "user",
            "content": f"""あなたはX(Twitter)ユーザーとして自然なリプライを考えます。

あなたのペルソナ:
{persona}

以下のツイートへのリプライ案を1つ考えてください。

【絶対NG】
- アドバイス・指摘・提案（「〜するといいですよ」「〜ではないでしょうか」など）
- 宣伝・売り込み
- ハッシュタグ

【重視すること】
- 相手の気持ちや状況への共感・共鳴を最優先
- 相手の言葉を受け止めて、同じ目線で寄り添う
- 140文字以内
- リプライ文のみ出力（説明・引用符不要）

【良い例】
- 「それ本当にわかります、自分もそこに可能性感じてます」
- 「めちゃくちゃ共感します、そういう視点大事ですよね」
- 「わかりすぎる、自分もずっとそう思ってました」

ツイート(@{tweet['username']}):
{tweet['text']}"""
        }]
    )
    return message.content[0].text.strip()


def send_to_slack(client: WebClient, channel: str, tweet: dict, reply_text: str, thread_ts: str = "") -> None:
    value = json.dumps({
        "tweet_url": tweet["tweet_url"],
        "reply_text": reply_text,
    })

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
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "✅ そのまま送信"},
                    "style": "primary",
                    "action_id": "reply_direct",
                    "value": value,
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "✏️ 編集して送信"},
                    "action_id": "reply_edit",
                    "value": value,
                },
            ],
        },
        {"type": "divider"},
    ]

    kwargs: dict = dict(
        channel=channel,
        blocks=blocks,
        text=f"@{tweet['username']} へのリプライ案",
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

        await browser.close()

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
            send_to_slack(slack_client, slack_channel, tweet, reply_text, slack_thread_ts)
            print(f"  送信: @{tweet['username']}")
            await asyncio.sleep(1)
        except SlackApiError as e:
            print(f"  Slack ERROR: {e}", file=sys.stderr)
        except Exception as e:
            print(f"  ERROR: {e}", file=sys.stderr)


if __name__ == "__main__":
    asyncio.run(main())
