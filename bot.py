"""
Finance Tracking Telegram Bot — Step 1
Batch-processes Telegram updates, echoes back to prove round-trip.
State (last_update_id, last_page_id, last_database) stored in a
Notion page whose ID comes from BOT_STATE_PAGE_ID env var.
"""

import json
import os
import sys
import traceback

import requests
from notion_client import Client

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

TELEGRAM_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
NOTION_KEY = os.environ["NOTION_API_KEY"]
ALLOWED_USER_ID = int(os.environ["ALLOWED_TELEGRAM_USER_ID"])
BOT_STATE_PAGE_ID = os.environ["BOT_STATE_PAGE_ID"]

TELEGRAM_API = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"

notion = Client(auth=NOTION_KEY)

# ---------------------------------------------------------------------------
# Notion Bot State helpers
# ---------------------------------------------------------------------------

DEFAULT_STATE: dict = {
    "last_update_id": 0,
    "last_page_id": None,
    "last_database": None,
}


def _find_state_block(blocks: list) -> dict | None:
    """Return the first block that looks like our JSON state store."""
    for block in blocks:
        btype = block["type"]
        if btype == "code":
            parts = block["code"].get("rich_text", [])
            if parts:
                raw = parts[0]["text"]["content"].strip()
                if raw.startswith("{"):
                    return block
        elif btype == "paragraph":
            parts = block["paragraph"].get("rich_text", [])
            if parts:
                raw = parts[0]["text"]["content"].strip()
                if raw.startswith("{"):
                    return block
    return None


def load_bot_state() -> dict:
    """Read the JSON state from the Bot State Notion page."""
    response = notion.blocks.children.list(block_id=BOT_STATE_PAGE_ID)
    block = _find_state_block(response["results"])
    if block is None:
        return dict(DEFAULT_STATE)
    btype = block["type"]
    raw = block[btype]["rich_text"][0]["text"]["content"].strip()
    try:
        state = json.loads(raw)
        # Ensure all expected keys exist
        for k, v in DEFAULT_STATE.items():
            state.setdefault(k, v)
        return state
    except json.JSONDecodeError:
        return dict(DEFAULT_STATE)


def save_bot_state(state: dict) -> None:
    """Write JSON state back to the Bot State Notion page."""
    payload = json.dumps(state, ensure_ascii=False)
    response = notion.blocks.children.list(block_id=BOT_STATE_PAGE_ID)
    block = _find_state_block(response["results"])

    if block is not None:
        btype = block["type"]
        rich_text = [{"type": "text", "text": {"content": payload}}]
        if btype == "code":
            notion.blocks.update(
                block_id=block["id"],
                code={"language": "json", "rich_text": rich_text},
            )
        else:
            notion.blocks.update(
                block_id=block["id"],
                paragraph={"rich_text": rich_text},
            )
    else:
        # First run — create the state block
        notion.blocks.children.append(
            block_id=BOT_STATE_PAGE_ID,
            children=[
                {
                    "object": "block",
                    "type": "code",
                    "code": {
                        "language": "json",
                        "rich_text": [
                            {"type": "text", "text": {"content": payload}}
                        ],
                    },
                }
            ],
        )


# ---------------------------------------------------------------------------
# Telegram helpers
# ---------------------------------------------------------------------------


def tg_get(method: str, **params) -> dict:
    resp = requests.get(
        f"{TELEGRAM_API}/{method}",
        params=params,
        timeout=25,
    )
    resp.raise_for_status()
    data = resp.json()
    if not data.get("ok"):
        raise RuntimeError(f"Telegram error: {data}")
    return data


def tg_send(chat_id: int, text: str) -> None:
    resp = requests.post(
        f"{TELEGRAM_API}/sendMessage",
        json={"chat_id": chat_id, "text": text},
        timeout=20,
    )
    resp.raise_for_status()
    data = resp.json()
    if not data.get("ok"):
        raise RuntimeError(f"Telegram sendMessage error: {data}")


# ---------------------------------------------------------------------------
# Message handlers  (Step 1: echo only — expanded in later steps)
# ---------------------------------------------------------------------------


def handle_message(msg: dict) -> str:
    """Return the reply string for a single Telegram message dict."""
    text = msg.get("text", "").strip()
    return f"✅ Bot received: {text}"


# ---------------------------------------------------------------------------
# Main batch loop
# ---------------------------------------------------------------------------


def main() -> None:
    # 1. Load persisted state
    state = load_bot_state()
    offset = state["last_update_id"]
    if offset:
        offset += 1  # Tell Telegram to skip everything up to and including last seen

    # 2. Fetch pending updates (timeout=0 → return immediately, no long poll)
    result = tg_get("getUpdates", offset=offset, timeout=0)
    updates = result.get("result", [])

    if not updates:
        print("No new updates.")
        return

    print(f"Fetched {len(updates)} update(s), starting at offset {offset}.")

    new_offset = state["last_update_id"]
    failures: list[tuple[int, str]] = []

    for update in updates:
        update_id: int = update["update_id"]
        new_offset = max(new_offset, update_id)

        msg = update.get("message") or update.get("edited_message")
        if not msg:
            # Non-message update (inline query etc.) — just advance offset
            continue

        chat_id: int = msg["chat"]["id"]
        from_user: dict = msg.get("from", {})
        sender_id: int = from_user.get("id", 0)

        # Silently ignore anyone who isn't the authorised user
        if sender_id != ALLOWED_USER_ID:
            print(f"Ignoring update {update_id} from user {sender_id}.")
            continue

        try:
            reply = handle_message(msg)
            tg_send(chat_id, reply)
            print(f"Replied to update {update_id}.")
        except Exception:
            err = traceback.format_exc()
            failures.append((update_id, err))
            print(f"ERROR processing update {update_id}:\n{err}")
            try:
                tg_send(chat_id, "⚠️ Internal error processing your message. Please try again.")
            except Exception:
                pass  # Best-effort error report

    # 3. Persist new offset AFTER processing the full batch
    state["last_update_id"] = new_offset
    save_bot_state(state)
    print(f"State saved. last_update_id={new_offset}")

    if failures:
        print(f"\n{len(failures)} message(s) failed:")
        for uid, err in failures:
            print(f"  update_id={uid}: {err}")
        sys.exit(1)


if __name__ == "__main__":
    main()
