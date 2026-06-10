#!/usr/bin/env python3
"""指定ツイートにリプライを実行する（GitHub Actions から呼ばれる）"""

import asyncio
import base64
import os
import random
import sys
from pathlib import Path

import httpx
from nacl import encoding, public as nacl_public
from playwright.async_api import async_playwright


async def post_reply(cookies_path: Path, tweet_url: str, reply_text: str, account: str) -> None:
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            storage_state=str(cookies_path),
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            viewport={"width": 1280, "height": 900},
        )
        page = await context.new_page()

        await page.goto("https://x.com/home", wait_until="domcontentloaded", timeout=60_000)
        await page.wait_for_timeout(2_000)
        if "/login" in page.url:
            raise RuntimeError(f"[{account}] セッション切れ")

        await page.goto(tweet_url, wait_until="domcontentloaded", timeout=60_000)
        await page.wait_for_timeout(random.randint(3_000, 5_000))

        # リプライボタンをクリック
        reply_btn = await page.wait_for_selector('[data-testid="reply"]', timeout=15_000)
        await reply_btn.click()
        await page.wait_for_timeout(random.randint(1_000, 2_000))

        # 入力欄にテキストを入力
        editor = await page.wait_for_selector('[data-testid="tweetTextarea_0"]', timeout=10_000)
        await editor.click()
        await page.wait_for_timeout(500)

        for char in reply_text:
            await editor.type(char)
            await asyncio.sleep(random.uniform(0.03, 0.09))

        await page.wait_for_timeout(random.randint(1_000, 2_000))

        # 送信
        submit_btn = await page.wait_for_selector('[data-testid="tweetButtonInline"]', timeout=10_000)
        await submit_btn.click()
        await page.wait_for_timeout(3_000)

        print(f"[{account}] リプライ完了: {tweet_url}")
        print(f"  内容: {reply_text}")

        await context.storage_state(path=str(cookies_path))
        await browser.close()


async def update_github_secret(cookies_path: Path, secret_name: str) -> None:
    token = os.environ.get("GH_TOKEN")
    repo = os.environ.get("GITHUB_REPOSITORY")
    if not token or not repo:
        return
    try:
        async with httpx.AsyncClient() as client:
            headers = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"}
            r = await client.get(
                f"https://api.github.com/repos/{repo}/actions/secrets/public-key",
                headers=headers, timeout=15,
            )
            r.raise_for_status()
            kd = r.json()
            pub = nacl_public.PublicKey(kd["key"].encode(), encoding.Base64Encoder)
            encrypted = base64.b64encode(
                nacl_public.SealedBox(pub).encrypt(
                    cookies_path.read_text(encoding="utf-8").encode("utf-8")
                )
            ).decode()
            await client.put(
                f"https://api.github.com/repos/{repo}/actions/secrets/{secret_name}",
                headers=headers,
                json={"encrypted_value": encrypted, "key_id": kd["key_id"]},
                timeout=15,
            )
        print(f"GitHub Secret {secret_name} 更新完了")
    except Exception as e:
        print(f"Secret 更新失敗（続行）: {e}", file=sys.stderr)


async def main() -> None:
    tweet_url = os.environ.get("TWEET_URL", "")
    reply_text = os.environ.get("REPLY_TEXT", "")

    if not tweet_url or not reply_text:
        print("ERROR: TWEET_URL と REPLY_TEXT が必要です", file=sys.stderr)
        sys.exit(1)

    account = os.environ.get("ACCOUNT", "knzw")
    cookies_path = Path(f"cookies_{account}.json")

    if not cookies_path.exists():
        print(f"ERROR: {cookies_path} が見つかりません", file=sys.stderr)
        sys.exit(1)

    await post_reply(cookies_path, tweet_url, reply_text, account)
    await update_github_secret(cookies_path, f"X_COOKIES_{account.upper()}")


if __name__ == "__main__":
    asyncio.run(main())
