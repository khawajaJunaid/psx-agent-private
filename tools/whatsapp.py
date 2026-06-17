"""Send a WhatsApp message via a self-hosted OpenWA gateway.

Required env vars:
  OPENWA_URL      - base URL of your OpenWA instance, e.g. http://your-server:2785
  OPENWA_API_KEY  - API key from the OpenWA dashboard
  OPENWA_SESSION  - session ID created in OpenWA (default: "default")
  OPENWA_CHAT_ID  - recipient in WhatsApp format, e.g. 923001234567@c.us
                    (92 = Pakistan country code, no leading 0)
"""

import os
import urllib.request
import urllib.error
import json


def send_message(text: str) -> bool:
    """Send a text message via OpenWA. Returns True on success, False on failure."""
    base_url = os.getenv("OPENWA_URL", "").rstrip("/")
    api_key = os.getenv("OPENWA_API_KEY", "")
    session = os.getenv("OPENWA_SESSION", "default")
    chat_id = os.getenv("OPENWA_CHAT_ID", "")

    if not base_url or not api_key or not chat_id:
        print("  [whatsapp] skipped — OPENWA_URL / OPENWA_API_KEY / OPENWA_CHAT_ID not set")
        return False

    url = f"{base_url}/api/sessions/{session}/messages/send-text"
    payload = json.dumps({"chatId": chat_id, "text": text}).encode()
    req = urllib.request.Request(
        url,
        data=payload,
        headers={
            "Content-Type": "application/json",
            "X-API-Key": api_key,
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            if resp.status < 300:
                print("  [whatsapp] message sent")
                return True
            print(f"  [whatsapp] unexpected status {resp.status}")
            return False
    except urllib.error.HTTPError as e:
        print(f"  [whatsapp] HTTP {e.code}: {e.reason}")
        return False
    except Exception as e:
        print(f"  [whatsapp] error: {e}")
        return False


def build_summary(results: list, verdict: dict) -> str:
    """Build a concise WhatsApp-friendly report from agent results."""
    lines = ["*PSX Scout Report*"]

    verdict_label = verdict.get("verdict", "")
    verdict_conf = verdict.get("confidence", 0)
    if verdict_label:
        lines.append(f"Verdict: {verdict_label} ({verdict_conf:.0%} conf)")

    lines.append("")
    for r in results:
        ticker = r.get("ticker", "")
        action = r.get("action", "HOLD")
        conf = r.get("confidence", 0)
        price = r.get("current_price")
        emoji = {"ENTER": "🟢", "ADD": "🟢", "HOLD": "🟡", "TRIM": "🟠", "EXIT": "🔴"}.get(action, "⚪")
        price_str = f"Rs {price:.2f}" if price else ""
        lines.append(f"{emoji} {ticker}  {action}  {conf:.0%}  {price_str}".strip())

    return "\n".join(lines)
