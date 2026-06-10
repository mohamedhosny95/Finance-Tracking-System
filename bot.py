"""
Finance Tracking Telegram Bot — Step 2
Adds Notion schema validation, account/category cache, fuzzy name
resolution, and /accounts command. Other messages still echo.
"""

import json
import os
import sys
import traceback
from dataclasses import dataclass
from typing import Optional

import requests
from notion_client import Client
from rapidfuzz import fuzz, process as fuzz_process

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

TELEGRAM_TOKEN   = os.environ["TELEGRAM_BOT_TOKEN"]
NOTION_KEY       = os.environ["NOTION_API_KEY"]
ALLOWED_USER_ID  = int(os.environ["ALLOWED_TELEGRAM_USER_ID"])
BOT_STATE_PAGE_ID = os.environ["BOT_STATE_PAGE_ID"]

TELEGRAM_API = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"

notion = Client(auth=NOTION_KEY)

# ---------------------------------------------------------------------------
# Notion Database IDs
# ---------------------------------------------------------------------------

DB_ACCOUNTS   = "23efa9ca-b092-81ed-8e88-000ba0b07ad4"
DB_EXPENSES   = "23efa9ca-b092-813f-8e4b-000bc2057d91"
DB_INCOME     = "23efa9ca-b092-8135-af20-000be6ea046b"
DB_TRANSFER   = "23efa9ca-b092-811c-927f-000b84373de0"
DB_CATEGORIES = "23efa9ca-b092-8196-a31f-000bc455781d"

# Required property names per database (checked at startup)
REQUIRED_PROPS: dict[str, set[str]] = {
    "Accounts":   {"Name", "Initial Amount", "Currency"},
    "Expenses":   {"Expense", "Total Amount", "Date", "Accounts", "Categories", "Notes"},
    "Transfer":   {"Name", "Date", "Amount Out", "Amount In", "From Account", "To Account"},
    "Categories": {"Name", "Monthly Budget"},
    # Income: title + amount discovered dynamically; only these are hard-required:
    "Income":     {"Date", "Accounts"},
}

# ---------------------------------------------------------------------------
# Account alias shortcuts  (case-insensitive keys)
# ---------------------------------------------------------------------------

ACCOUNT_ALIASES: dict[str, str] = {
    "cash":       "Cash Wallet",
    "wallet":     "Cash Wallet",
    "cashwallet": "Cash Wallet",
    "saving":     "Saving Wallet",
    "savings":    "Saving Wallet",
    "fawry":      "MyFawry",
    "myfawry":    "MyFawry",
    "nbe":        "NBE",
    "vodafone":   "Vodafone Cash",
    "voda":       "Vodafone Cash",
    "vcash":      "Vodafone Cash",
    "mashreq":    "Mashreq Neo",
    "neo":        "Mashreq Neo",
    "misr":       "Banque Misr",
    "bm":         "Banque Misr",
    "banquemisr": "Banque Misr",
    "telda":      "Telda",
    "nexta":      "Nexta",
    "klivver":    "Klivver",
    "qnb":        "QNB",
}

CURRENCY_FLAG: dict[str, str] = {
    "EGP": "🇪🇬",
    "USD": "🇺🇸",
    "EUR": "🇪🇺",
    "GBP": "🇬🇧",
}

# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class Account:
    page_id: str
    name: str
    currency: str  # EGP | USD | EUR | GBP


@dataclass
class Category:
    page_id: str
    name: str
    monthly_budget: float


@dataclass
class IncomeSchema:
    """Dynamically discovered Income DB property names."""
    title_prop: str
    amount_prop: str
    notes_prop: Optional[str]


# ---------------------------------------------------------------------------
# Global cache — populated once per run during startup
# ---------------------------------------------------------------------------

ACCOUNTS: list[Account] = []
CATEGORIES: list[Category] = []
INCOME_SCHEMA: Optional[IncomeSchema] = None

# ---------------------------------------------------------------------------
# Notion helpers
# ---------------------------------------------------------------------------


def notion_query_all(database_id: str, **kwargs) -> list[dict]:
    """Query a Notion DB and return ALL pages, handling pagination."""
    results: list[dict] = []
    cursor: Optional[str] = None
    while True:
        params = dict(kwargs)
        if cursor:
            params["start_cursor"] = cursor
        resp = notion.databases.query(database_id=database_id, **params)
        results.extend(resp["results"])
        if not resp.get("has_more"):
            break
        cursor = resp["next_cursor"]
    return results


def _title_text(prop_value: dict) -> str:
    parts = prop_value.get("title") or prop_value.get("rich_text") or []
    return parts[0]["plain_text"] if parts else ""


# ---------------------------------------------------------------------------
# Startup: schema validation + cache loading
# ---------------------------------------------------------------------------


def validate_and_load_schemas() -> None:
    """Fetch all DB schemas, validate required properties, fill global caches."""
    global ACCOUNTS, CATEGORIES, INCOME_SCHEMA

    db_map = {
        "Accounts":   DB_ACCOUNTS,
        "Expenses":   DB_EXPENSES,
        "Income":     DB_INCOME,
        "Transfer":   DB_TRANSFER,
        "Categories": DB_CATEGORIES,
    }

    print("Fetching Notion schemas…")
    schemas: dict[str, dict] = {
        label: notion.databases.retrieve(database_id=db_id)
        for label, db_id in db_map.items()
    }

    # Validate required property names
    errors: list[str] = []
    for label, required_names in REQUIRED_PROPS.items():
        actual_names = set(schemas[label]["properties"].keys())
        missing = required_names - actual_names
        if missing:
            errors.append(f"  [{label}] missing properties: {sorted(missing)}")
    if errors:
        raise RuntimeError("Notion schema validation failed:\n" + "\n".join(errors))

    # Discover Income schema (title + amount property names may vary)
    income_props = schemas["Income"]["properties"]
    title_prop = next(
        (n for n, p in income_props.items() if p["type"] == "title"), None
    )
    amount_prop = next(
        (n for n, p in income_props.items() if p["type"] == "number"), None
    )
    notes_prop = next(
        (n for n, p in income_props.items()
         if p["type"] == "rich_text" and "note" in n.lower()),
        None,
    )
    if not title_prop or not amount_prop:
        raise RuntimeError(
            f"Income DB: could not discover title/amount property. "
            f"Properties found: {sorted(income_props.keys())}"
        )
    INCOME_SCHEMA = IncomeSchema(
        title_prop=title_prop,
        amount_prop=amount_prop,
        notes_prop=notes_prop,
    )
    print(
        f"Income schema: title={title_prop!r}, amount={amount_prop!r}, "
        f"notes={notes_prop!r}"
    )

    # Load accounts
    pages = notion_query_all(DB_ACCOUNTS)
    ACCOUNTS = []
    for page in pages:
        props = page["properties"]
        name_parts = props["Name"]["title"]
        name = name_parts[0]["plain_text"] if name_parts else ""
        currency = (props["Currency"].get("select") or {}).get("name", "EGP")
        if name:
            ACCOUNTS.append(Account(page_id=page["id"], name=name, currency=currency))
    print(f"Loaded {len(ACCOUNTS)} account(s).")

    # Load categories
    pages = notion_query_all(DB_CATEGORIES)
    CATEGORIES = []
    for page in pages:
        props = page["properties"]
        name_parts = props["Name"]["title"]
        name = name_parts[0]["plain_text"] if name_parts else ""
        budget = props["Monthly Budget"].get("number") or 0.0
        if name:
            CATEGORIES.append(
                Category(page_id=page["id"], name=name, monthly_budget=budget)
            )
    print(f"Loaded {len(CATEGORIES)} categor(ies).")


# ---------------------------------------------------------------------------
# Name resolution
# ---------------------------------------------------------------------------


def _accounts_list_str() -> str:
    return " · ".join(a.name for a in sorted(ACCOUNTS, key=lambda a: a.name))


def _categories_list_str() -> str:
    return " · ".join(c.name for c in sorted(CATEGORIES, key=lambda c: c.name))


def resolve_account(query: str) -> tuple[Optional[Account], Optional[str]]:
    """Return (account, None) on success, or (None, error_text) on failure."""
    q = query.strip().lstrip("@").lower()

    # 1. Alias lookup → canonical name
    canonical_lower = ACCOUNT_ALIASES.get(q, q)

    # 2. Exact case-insensitive match
    for acc in ACCOUNTS:
        if acc.name.lower() == canonical_lower:
            return acc, None

    # 3. Fuzzy match against all account names (threshold 72)
    names = [a.name for a in ACCOUNTS]
    match = fuzz_process.extractOne(q, names, scorer=fuzz.WRatio, score_cutoff=72)
    if match:
        for acc in ACCOUNTS:
            if acc.name == match[0]:
                return acc, None

    err = (
        f"❌ Account not found: '{query}'\n\n"
        f"Your accounts: {_accounts_list_str()}\n\n"
        f"Example:\nspent 250 on lunch @nbe"
    )
    return None, err


def resolve_category(query: str) -> tuple[Optional[Category], Optional[str]]:
    """Return (category, None) on success, or (None, error_text) on failure."""
    q = query.strip().lower()

    # Exact case-insensitive match
    for cat in CATEGORIES:
        if cat.name.lower() == q:
            return cat, None

    # Fuzzy match (threshold 68 — slightly lower, categories are short words)
    names = [c.name for c in CATEGORIES]
    match = fuzz_process.extractOne(q, names, scorer=fuzz.WRatio, score_cutoff=68)
    if match:
        for cat in CATEGORIES:
            if cat.name == match[0]:
                return cat, None

    err = (
        f"❌ Category not found: '{query}'\n\n"
        f"Your categories: {_categories_list_str()}\n\n"
        f"Example:\n/e 350 Food lunch with team @cash"
    )
    return None, err


# ---------------------------------------------------------------------------
# Notion Bot State  (unchanged from Step 1)
# ---------------------------------------------------------------------------

DEFAULT_STATE: dict = {
    "last_update_id": 0,
    "last_page_id": None,
    "last_database": None,
}


def _find_state_block(blocks: list) -> Optional[dict]:
    for block in blocks:
        btype = block["type"]
        if btype in ("code", "paragraph"):
            parts = block[btype].get("rich_text", [])
            if parts and parts[0]["text"]["content"].strip().startswith("{"):
                return block
    return None


def load_bot_state() -> dict:
    response = notion.blocks.children.list(block_id=BOT_STATE_PAGE_ID)
    block = _find_state_block(response["results"])
    if block is None:
        return dict(DEFAULT_STATE)
    btype = block["type"]
    raw = block[btype]["rich_text"][0]["text"]["content"].strip()
    try:
        state = json.loads(raw)
        for k, v in DEFAULT_STATE.items():
            state.setdefault(k, v)
        return state
    except json.JSONDecodeError:
        return dict(DEFAULT_STATE)


def save_bot_state(state: dict) -> None:
    payload = json.dumps(state, ensure_ascii=False)
    response = notion.blocks.children.list(block_id=BOT_STATE_PAGE_ID)
    block = _find_state_block(response["results"])
    rich_text = [{"type": "text", "text": {"content": payload}}]
    if block is not None:
        btype = block["type"]
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
        notion.blocks.children.append(
            block_id=BOT_STATE_PAGE_ID,
            children=[{
                "object": "block",
                "type": "code",
                "code": {"language": "json", "rich_text": rich_text},
            }],
        )


# ---------------------------------------------------------------------------
# Telegram helpers
# ---------------------------------------------------------------------------


def tg_get(method: str, **params) -> dict:
    resp = requests.get(f"{TELEGRAM_API}/{method}", params=params, timeout=25)
    resp.raise_for_status()
    data = resp.json()
    if not data.get("ok"):
        raise RuntimeError(f"Telegram error: {data}")
    return data


def tg_send(chat_id: int, text: str, parse_mode: str = "Markdown") -> None:
    resp = requests.post(
        f"{TELEGRAM_API}/sendMessage",
        json={"chat_id": chat_id, "text": text, "parse_mode": parse_mode},
        timeout=20,
    )
    resp.raise_for_status()
    data = resp.json()
    if not data.get("ok"):
        raise RuntimeError(f"sendMessage error: {data}")


# ---------------------------------------------------------------------------
# Command handlers
# ---------------------------------------------------------------------------


def cmd_accounts() -> str:
    from collections import defaultdict

    by_currency: dict[str, list[str]] = defaultdict(list)
    for acc in sorted(ACCOUNTS, key=lambda a: a.name):
        by_currency[acc.currency].append(acc.name)

    lines = ["📊 *Accounts*\n"]
    for currency in sorted(by_currency.keys()):
        flag = CURRENCY_FLAG.get(currency, "💰")
        lines.append(f"{flag} *{currency}*")
        for name in by_currency[currency]:
            lines.append(f"  · {name}")
        lines.append("")
    return "\n".join(lines).strip()


def cmd_help() -> str:
    return (
        "*Available commands*\n\n"
        "`/accounts` — list all accounts with currency\n"
        "`/help` — this message\n\n"
        "_More commands coming soon…_"
    )


def handle_message(msg: dict) -> str:
    text = msg.get("text", "").strip()

    if text == "/accounts":
        return cmd_accounts()

    if text == "/help":
        return cmd_help()

    # Fallback echo (replaced in Steps 3–6)
    return f"✅ Bot received: {text}"


# ---------------------------------------------------------------------------
# Main batch loop
# ---------------------------------------------------------------------------


def main() -> None:
    # Startup: validate schemas and populate caches
    validate_and_load_schemas()

    state = load_bot_state()
    offset = state["last_update_id"]
    if offset:
        offset += 1

    result = tg_get("getUpdates", offset=offset, timeout=0)
    updates = result.get("result", [])

    if not updates:
        print("No new updates.")
        return

    print(f"Fetched {len(updates)} update(s).")

    new_offset = state["last_update_id"]
    failures: list[tuple[int, str]] = []

    for update in updates:
        update_id: int = update["update_id"]
        new_offset = max(new_offset, update_id)

        msg = update.get("message") or update.get("edited_message")
        if not msg:
            continue

        chat_id: int = msg["chat"]["id"]
        sender_id: int = msg.get("from", {}).get("id", 0)

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
            print(f"ERROR on update {update_id}:\n{err}")
            try:
                tg_send(
                    chat_id,
                    "⚠️ Internal error processing your message. Please try again.",
                    parse_mode="",
                )
            except Exception:
                pass

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
