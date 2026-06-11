#!/usr/bin/env python3
"""Slack の ✅ リアクションを検知してリプライを実行する"""

import asyncio
import os
import sys
from pathlib import Path

import yaml
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

from reply import post_reply, update_github_secret


def load_config(path: str) -> dict:
    with open(path) as f:
        return yaml.safe_load(f)


def get_pending_replies(client: WebClient, channel: str, thread_ts: str) -> list[dict]:
    pending = []
    cursor = None

    while True:
        kwargs = dict(channel=channel, ts=thread_ts, include_all_metadata=True, limit=100)
        if cursor:
            kwargs["cursor"] = cursor

        resp = client.conversations_replies(**kwargs)
        for msg in resp.get("messages", []):
            meta = msg.get("metadata")
            if not meta or meta.get("event_type") != "reply_candidate":
                continue

            reactions = {r["name"] for r in msg.get("reactions", [])}
            if "white_check_mark" not in reactions:
                continue
            if "heavy_check_mark" in reactions:
                continue

            payload = meta["event_payload"]
            pending.append({
                "ts": msg["ts"],
                "tweet_url": payload["tweet_url"],
                "reply_text": payload["reply_text"],
            })

        if resp.get("has_more"):
            cursor = resp["response_metadata"]["next_cursor"]
        else:
            break

    return pending


def mark_done(client: WebClient, channel: str, ts: str) -> None:
    try:
        client.reactions_remove(channel=channel, name="white_check_mark", timestamp=ts)
    except SlackApiError:
        pass
    client.reactions_add(channel=channel, name="heavy_check_mark", timestamp=ts)


async def main() -> None:
    config_path = os.environ.get("CONFIG_PATH", "config/knzw.yml")
    config = load_config(config_path)
    account = config.get("account", "knzw")
    channel = config.get("slack_channel", "")
    thread_ts = config.get("slack_thread_ts", "")

    if not channel or not thread_ts:
        print("ERROR: slack_channel と slack_thread_ts が設定されていません", file=sys.stderr)
        sys.exit(1)

    slack_client = WebClient(token=os.environ["SLACK_BOT_TOKEN"])
    pending = get_pending_replies(slack_client, channel, thread_ts)

    if not pending:
        print("処理対象なし")
        return

    print(f"{len(pending)} 件を処理します")

    cookies_path = Path(f"cookies_{account}.json")
    if not cookies_path.exists():
        print(f"ERROR: {cookies_path} が見つかりません", file=sys.stderr)
        sys.exit(1)

    for item in pending:
        try:
            await post_reply(cookies_path, item["tweet_url"], item["reply_text"], account)
            mark_done(slack_client, channel, item["ts"])
            slack_client.chat_postMessage(
                channel=channel,
                thread_ts=item["ts"],
                text=f"✅ リプライ完了しました！\n{item['tweet_url']}",
            )
            print(f"  完了: {item['tweet_url']}")
        except Exception as e:
            print(f"  ERROR [{item['tweet_url']}]: {e}", file=sys.stderr)
            slack_client.chat_postMessage(
                channel=channel,
                thread_ts=item["ts"],
                text=f"❌ リプライ失敗: {e}",
            )

    await update_github_secret(cookies_path, f"X_COOKIES_{account.upper()}")


if __name__ == "__main__":
    asyncio.run(main())
