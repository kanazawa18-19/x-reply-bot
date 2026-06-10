#!/usr/bin/env python3
"""Slack インタラクションを受けて GitHub Actions でリプライを実行するサーバー"""

import hashlib
import hmac
import json
import os
import time

import requests
from flask import Flask, jsonify, request
from slack_sdk import WebClient

app = Flask(__name__)

SLACK_SIGNING_SECRET = os.environ["SLACK_SIGNING_SECRET"]
SLACK_BOT_TOKEN = os.environ["SLACK_BOT_TOKEN"]
GH_TOKEN = os.environ["GH_TOKEN"]
GH_REPO = os.environ["GH_REPO"]  # 例: kanazawa18-19/x-reply-bot


def verify_slack_signature(req) -> bool:
    ts = req.headers.get("X-Slack-Request-Timestamp", "")
    sig = req.headers.get("X-Slack-Signature", "")
    if not ts or not sig:
        return False
    if abs(time.time() - int(ts)) > 300:
        return False
    base = f"v0:{ts}:{req.get_data(as_text=True)}"
    expected = "v0=" + hmac.new(
        SLACK_SIGNING_SECRET.encode(), base.encode(), hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, sig)


def trigger_reply_workflow(tweet_url: str, reply_text: str) -> bool:
    r = requests.post(
        f"https://api.github.com/repos/{GH_REPO}/actions/workflows/reply.yml/dispatches",
        headers={
            "Authorization": f"Bearer {GH_TOKEN}",
            "Accept": "application/vnd.github+json",
        },
        json={"ref": "main", "inputs": {"tweet_url": tweet_url, "reply_text": reply_text}},
        timeout=10,
    )
    return r.status_code == 204


def update_slack_message(response_url: str, text: str) -> None:
    requests.post(
        response_url,
        json={"text": text, "replace_original": False},
        timeout=10,
    )


def open_edit_modal(trigger_id: str, data: dict) -> None:
    slack = WebClient(token=SLACK_BOT_TOKEN)
    slack.views_open(
        trigger_id=trigger_id,
        view={
            "type": "modal",
            "callback_id": "reply_modal",
            "title": {"type": "plain_text", "text": "リプライ編集"},
            "submit": {"type": "plain_text", "text": "送信"},
            "close": {"type": "plain_text", "text": "キャンセル"},
            "private_metadata": json.dumps({
                "tweet_url": data["tweet_url"],
                "response_url": data.get("response_url", ""),
            }),
            "blocks": [
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": f"<{data['tweet_url']}|元ツイートを見る>",
                    },
                },
                {
                    "type": "input",
                    "block_id": "reply_block",
                    "label": {"type": "plain_text", "text": "リプライ内容"},
                    "element": {
                        "type": "plain_text_input",
                        "action_id": "reply_input",
                        "multiline": True,
                        "initial_value": data.get("reply_text", ""),
                        "max_length": 140,
                    },
                },
            ],
        },
    )


@app.route("/slack/actions", methods=["POST"])
def slack_actions():
    if not verify_slack_signature(request):
        return jsonify({"error": "invalid signature"}), 403

    payload = json.loads(request.form["payload"])
    payload_type = payload.get("type")

    if payload_type == "block_actions":
        action = payload["actions"][0]
        action_id = action["action_id"]
        data = json.loads(action["value"])
        data["response_url"] = payload.get("response_url", "")

        if action_id == "reply_direct":
            ok = trigger_reply_workflow(data["tweet_url"], data["reply_text"])
            msg = "✅ リプライを実行中..." if ok else "❌ 実行失敗"
            if data["response_url"]:
                update_slack_message(data["response_url"], msg)

        elif action_id == "reply_edit":
            open_edit_modal(payload["trigger_id"], data)

    elif payload_type == "view_submission":
        meta = json.loads(payload["view"]["private_metadata"])
        reply_text = (
            payload["view"]["state"]["values"]["reply_block"]["reply_input"]["value"]
        )
        ok = trigger_reply_workflow(meta["tweet_url"], reply_text)
        if meta.get("response_url"):
            msg = "✅ リプライを実行中..." if ok else "❌ 実行失敗"
            update_slack_message(meta["response_url"], msg)

    return jsonify({})


@app.route("/health", methods=["GET"])
def health():
    return "ok"


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    app.run(host="0.0.0.0", port=port)
