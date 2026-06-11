"""
Finance Tracking Telegram Bot — Step 3
Adds /e, /i, /t commands with full receipts and error messages.
"""

import html
import json
import os
import sys
import traceback
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

import requests
from notion_client import Client
from rapidfuzz import fuzz, process as fuzz_process

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def _env(name: str) -> str:
    """Read env var, tolerating accidental quotes/whitespace from copy-paste."""
    return os.environ[name].strip().strip("'\"").strip()


def _env_int(name: str) -> int:
    raw = _env(name)
    try:
        return int(raw)
    except ValueError:
        raise SystemExit(
            f"CONFIG ERROR: {name} must be a plain numeric Telegram user ID.\n"
            f"Get yours by messaging @userinfobot on Telegram — it replies\n"
            f"with a number like 123456789. Put exactly that number in the\n"
            f"GitHub Secret (no quotes, no @username).\n"
            f"Current value is not a valid integer (length {len(raw)})."
        )


TELEGRAM_TOKEN    = _env("TELEGRAM_BOT_TOKEN")
NOTION_KEY        = _env("NOTION_API_KEY")
ALLOWED_USER_ID   = _env_int("ALLOWED_TELEGRAM_USER_ID")
BOT_STATE_PAGE_ID = _env("BOT_STATE_PAGE_ID")

TELEGRAM_API = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"

notion = Client(auth=NOTION_KEY)

# ---------------------------------------------------------------------------
# Notion Database IDs
# ---------------------------------------------------------------------------

DB_ACCOUNTS   = "23efa9ca-b092-813a-94a0-c93d9ce4e2d9"
DB_EXPENSES   = "23efa9ca-b092-8179-b11f-d5bcb8c3c589"
DB_INCOME     = "23efa9ca-b092-816d-951e-d8d5eaf2fc3e"
DB_TRANSFER   = "23efa9ca-b092-814e-9816-e451c38b5aac"
DB_CATEGORIES = "23efa9ca-b092-81dd-ae1c-d6f19bf425e4"

REQUIRED_PROPS: dict[str, set[str]] = {
    "Accounts":   {"Name", "Initial Amount", "Currency"},
    "Expenses":   {"Expense", "Total Amount", "Date", "Accounts", "Categories", "Notes"},
    "Transfer":   {"Name", "Date", "Amount Out", "Amount In", "From Account", "To Account"},
    "Categories": {"Name", "Monthly Budget"},
    "Income":     {"Date", "Accounts"},
}

# ---------------------------------------------------------------------------
# Account alias shortcuts
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
    "EGP": "🇪🇬", "USD": "🇺🇸", "EUR": "🇪🇺", "GBP": "🇬🇧",
}

# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class Account:
    page_id: str
    name: str
    currency: str
    initial_amount: float = 0.0


@dataclass
class Category:
    page_id: str
    name: str
    monthly_budget: float


@dataclass
class IncomeSchema:
    title_prop: str
    amount_prop: str
    notes_prop: Optional[str]


# ---------------------------------------------------------------------------
# Global cache
# ---------------------------------------------------------------------------

ACCOUNTS: list[Account] = []
CATEGORIES: list[Category] = []
INCOME_SCHEMA: Optional[IncomeSchema] = None

# ---------------------------------------------------------------------------
# Notion helpers
# ---------------------------------------------------------------------------


def notion_query_all(database_id: str, **kwargs) -> list[dict]:
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


# ---------------------------------------------------------------------------
# Startup: schema validation + cache loading
# ---------------------------------------------------------------------------


def validate_and_load_schemas() -> None:
    global ACCOUNTS, CATEGORIES, INCOME_SCHEMA

    db_map = {
        "Accounts":   DB_ACCOUNTS,
        "Expenses":   DB_EXPENSES,
        "Income":     DB_INCOME,
        "Transfer":   DB_TRANSFER,
        "Categories": DB_CATEGORIES,
    }

    # notion-client v3 databases.retrieve() returns data_sources[id,name] only for
    # inline databases — no properties/schema. Use query(page_size=1) instead,
    # which always includes full property info in each result page.
    print("Fetching Notion schemas…")
    schemas: dict[str, dict] = {}
    access_errors: list[str] = []
    for label, db_id in db_map.items():
        try:
            result = notion.databases.query(database_id=db_id, page_size=1)
            pages = result.get("results", [])
            schemas[label] = pages[0]["properties"] if pages else {}
        except Exception as exc:
            access_errors.append(f"  [{label}] {exc}")
    if access_errors:
        raise RuntimeError("Cannot access databases:\n" + "\n".join(access_errors))

    errors: list[str] = []
    for label, required in REQUIRED_PROPS.items():
        props = schemas.get(label, {})
        if not props:
            print(f"  [{label}] empty — skipping property check")
            continue
        missing = required - set(props.keys())
        if missing:
            errors.append(f"  [{label}] missing: {sorted(missing)}")
    if errors:
        raise RuntimeError("Schema validation failed:\n" + "\n".join(errors))

    income_props = schemas.get("Income", {})
    title_prop  = next((n for n, p in income_props.items() if p.get("type") == "title"), None)
    amount_prop = next((n for n, p in income_props.items() if p.get("type") == "number"), None)
    notes_prop  = next(
        (n for n, p in income_props.items()
         if p.get("type") in ("rich_text", "text") and "note" in n.lower()), None
    )
    if not title_prop or not amount_prop:
        raise RuntimeError(
            f"Income DB: can't discover title/amount. "
            f"Found: {sorted(income_props.keys())}"
        )
    INCOME_SCHEMA = IncomeSchema(title_prop=title_prop, amount_prop=amount_prop,
                                 notes_prop=notes_prop)
    print(f"Income schema: title={title_prop!r} amount={amount_prop!r} notes={notes_prop!r}")

    pages = notion_query_all(DB_ACCOUNTS)
    ACCOUNTS = []
    for page in pages:
        props = page["properties"]
        parts = props["Name"]["title"]
        name = parts[0]["plain_text"].strip() if parts else ""
        currency = (props["Currency"].get("select") or {}).get("name", "EGP")
        initial = props["Initial Amount"].get("number") or 0.0
        if name:
            ACCOUNTS.append(Account(page_id=page["id"], name=name,
                                    currency=currency, initial_amount=initial))
    print(f"Loaded {len(ACCOUNTS)} account(s).")

    pages = notion_query_all(DB_CATEGORIES)
    CATEGORIES = []
    for page in pages:
        props = page["properties"]
        parts = props["Name"]["title"]
        name = parts[0]["plain_text"].strip() if parts else ""
        budget = props["Monthly Budget"].get("number") or 0.0
        if name:
            CATEGORIES.append(Category(page_id=page["id"], name=name, monthly_budget=budget))
    print(f"Loaded {len(CATEGORIES)} categor(ies).")


# ---------------------------------------------------------------------------
# Name resolution
# ---------------------------------------------------------------------------


def _accounts_list_str() -> str:
    return " · ".join(a.name for a in sorted(ACCOUNTS, key=lambda a: a.name))


def _categories_list_str() -> str:
    return " · ".join(c.name for c in sorted(CATEGORIES, key=lambda c: c.name))


def resolve_account(query: str) -> tuple[Optional[Account], Optional[str]]:
    q = query.strip().lstrip("@").lower()
    canonical_lower = ACCOUNT_ALIASES.get(q, q)
    for acc in ACCOUNTS:
        if acc.name.lower() == canonical_lower:
            return acc, None
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
    q = query.strip().lower()
    for cat in CATEGORIES:
        if cat.name.lower() == q:
            return cat, None
    names = [c.name for c in CATEGORIES]
    match = fuzz_process.extractOne(q, names, scorer=fuzz.WRatio, score_cutoff=68)
    if match:
        for cat in CATEGORIES:
            if cat.name == match[0]:
                return cat, None
    err = (
        f"❌ Category not found: '{query}'\n\n"
        f"Your categories: {_categories_list_str()}\n\n"
        f"Example:\n/e 350 Food&Groceries groceries @cash"
    )
    return None, err


# ---------------------------------------------------------------------------
# Formatters
# ---------------------------------------------------------------------------


def fmt_amount(amount: float, currency: str) -> str:
    n: int | float = int(amount) if amount == int(amount) else amount
    return f"{n:,} {currency}"


def _h(text: str) -> str:
    """HTML-escape user-provided text."""
    return html.escape(str(text))


# ---------------------------------------------------------------------------
# Parsers
# ---------------------------------------------------------------------------


def _parse_amount(token: str) -> tuple[Optional[float], Optional[str]]:
    try:
        v = float(token.replace(",", ""))
        if v <= 0:
            return None, f"Amount must be positive, got '{token}'"
        return v, None
    except ValueError:
        return None, f"Invalid amount: '{token}'"


def parse_expense(args: str) -> tuple[Optional[dict], Optional[str]]:
    """
    /e <amount> <category> <note> @account
    Returns (parsed_dict, None) or (None, error_str).
    """
    tokens = args.split()
    at_indices = [i for i, t in enumerate(tokens) if t.startswith("@")]
    if not at_indices:
        return None, (
            "❌ Missing @account\n\n"
            "Example:\n/e 350 Transportation uber @cash"
        )

    acc_idx = at_indices[-1]
    account_str = tokens[acc_idx].lstrip("@")
    before = tokens[:acc_idx]

    if len(before) < 2:
        return None, (
            "❌ Need at least: amount + category + @account\n\n"
            "Example:\n/e 350 Transportation uber @cash"
        )

    amount, err = _parse_amount(before[0])
    if err:
        return None, f"❌ {err}\n\nExample:\n/e 350 Transportation uber @cash"

    category_str = before[1]
    note = " ".join(before[2:]).strip()

    return {"amount": amount, "category": category_str,
            "note": note, "account": account_str}, None


def parse_income(args: str) -> tuple[Optional[dict], Optional[str]]:
    """
    /i <amount> <note> @account
    Returns (parsed_dict, None) or (None, error_str).
    """
    tokens = args.split()
    at_indices = [i for i, t in enumerate(tokens) if t.startswith("@")]
    if not at_indices:
        return None, (
            "❌ Missing @account\n\n"
            "Example:\n/i 15000 salary april @nbe"
        )

    acc_idx = at_indices[-1]
    account_str = tokens[acc_idx].lstrip("@")
    before = tokens[:acc_idx]

    if not before:
        return None, (
            "❌ Missing amount\n\n"
            "Example:\n/i 15000 salary april @nbe"
        )

    amount, err = _parse_amount(before[0])
    if err:
        return None, f"❌ {err}\n\nExample:\n/i 15000 salary april @nbe"

    note = " ".join(before[1:]).strip()
    return {"amount": amount, "note": note, "account": account_str}, None


def parse_transfer(args: str) -> tuple[Optional[dict], Optional[str]]:
    """
    Same-currency:  /t <amount> @from @to [note]
    Cross-currency: /t <amount_out> @from <amount_in> @to [note]
    Returns (parsed_dict, None) or (None, error_str).
    """
    tokens = args.split()
    at_indices = [i for i, t in enumerate(tokens) if t.startswith("@")]

    if len(at_indices) < 2:
        return None, (
            "❌ Need two accounts: @from and @to\n\n"
            "Examples:\n"
            "/t 2000 @nbe @cash ATM withdrawal\n"
            "/t 100 @mashreq 4950 @cash USD to EGP"
        )

    from_idx = at_indices[0]
    to_idx   = at_indices[1]
    from_str = tokens[from_idx].lstrip("@")
    to_str   = tokens[to_idx].lstrip("@")

    before_from = tokens[:from_idx]
    between     = tokens[from_idx + 1 : to_idx]
    after_to    = tokens[to_idx + 1 :]

    if not before_from:
        return None, (
            "❌ Missing amount before @from\n\n"
            "Examples:\n"
            "/t 2000 @nbe @cash\n"
            "/t 100 @mashreq 4950 @cash USD to EGP"
        )

    amount_out, err = _parse_amount(before_from[0])
    if err:
        return None, f"❌ {err}\n\nExample:\n/t 2000 @nbe @cash"

    # Cross-currency: first token between @from and @to is a number
    amount_in: Optional[float] = None
    note_tokens: list[str] = list(after_to)

    if between:
        candidate, _ = _parse_amount(between[0])
        if candidate is not None:
            amount_in = candidate
            note_tokens = list(after_to)
        else:
            note_tokens = list(between) + list(after_to)

    return {
        "amount_out": amount_out,
        "amount_in":  amount_in,   # None → same-currency
        "from_account": from_str,
        "to_account":   to_str,
        "note": " ".join(note_tokens).strip(),
    }, None


# ---------------------------------------------------------------------------
# Notion writes
# ---------------------------------------------------------------------------

CAIRO_TZ = timezone(timedelta(hours=2))
today = datetime.now(CAIRO_TZ).date().isoformat()  # Set once per run, Cairo time


def _notion_rich_text(text: str) -> list[dict]:
    return [{"type": "text", "text": {"content": text}}] if text else []


def notion_create_expense(
    amount: float, account: Account, category: Category, note: str
) -> dict:
    title = note or f"{fmt_amount(amount, account.currency)} {category.name}"
    props: dict = {
        "Expense":      {"title": [{"text": {"content": title}}]},
        "Total Amount": {"number": amount},
        "Date":         {"date": {"start": today}},
        "Accounts":     {"relation": [{"id": account.page_id}]},
        "Categories":   {"relation": [{"id": category.page_id}]},
    }
    if note:
        props["Notes"] = {"rich_text": _notion_rich_text(note)}
    page = notion.pages.create(parent={"database_id": DB_EXPENSES}, properties=props)
    return page


def notion_create_income(amount: float, account: Account, note: str) -> dict:
    title = note or fmt_amount(amount, account.currency)
    props: dict = {
        INCOME_SCHEMA.title_prop:  {"title": [{"text": {"content": title}}]},
        INCOME_SCHEMA.amount_prop: {"number": amount},
        "Date":     {"date": {"start": today}},
        "Accounts": {"relation": [{"id": account.page_id}]},
    }
    if note and INCOME_SCHEMA.notes_prop:
        props[INCOME_SCHEMA.notes_prop] = {"rich_text": _notion_rich_text(note)}
    page = notion.pages.create(parent={"database_id": DB_INCOME}, properties=props)
    return page


def notion_create_transfer(
    amount_out: float, amount_in: float,
    from_acc: Account, to_acc: Account, note: str
) -> dict:
    title = note or f"{from_acc.name} → {to_acc.name}"
    page = notion.pages.create(
        parent={"database_id": DB_TRANSFER},
        properties={
            "Name":         {"title": [{"text": {"content": title}}]},
            "Date":         {"date": {"start": today}},
            "Amount Out":   {"number": amount_out},
            "Amount In":    {"number": amount_in},
            "From Account": {"relation": [{"id": from_acc.page_id}]},
            "To Account":   {"relation": [{"id": to_acc.page_id}]},
        },
    )
    return page


# ---------------------------------------------------------------------------
# Receipt builders
# ---------------------------------------------------------------------------


def receipt_expense(amount: float, account: Account, category: Category,
                    note: str, page_url: str) -> str:
    rows = [
        ("💰", "Amount",   fmt_amount(amount, account.currency)),
        ("🏷", "Category", category.name),
        ("🏦", "Account",  account.name),
    ]
    if note:
        rows.append(("📝", "Note", note))
    rows.append(("📅", "Date", today))
    lines = ["✅ <b>Expense logged</b>\n"]
    for emoji, label, value in rows:
        lines.append(f"{emoji} {label}:  {_h(value)}")
    lines += ["\n↩️ /undo to reverse", f'🔗 <a href="{page_url}">View in Notion</a>']
    return "\n".join(lines)


def receipt_income(amount: float, account: Account, note: str, page_url: str) -> str:
    rows = [
        ("💰", "Amount",  fmt_amount(amount, account.currency)),
        ("🏦", "Account", account.name),
    ]
    if note:
        rows.append(("📝", "Note", note))
    rows.append(("📅", "Date", today))
    lines = ["✅ <b>Income logged</b>\n"]
    for emoji, label, value in rows:
        lines.append(f"{emoji} {label}:  {_h(value)}")
    lines += ["\n↩️ /undo to reverse", f'🔗 <a href="{page_url}">View in Notion</a>']
    return "\n".join(lines)


def receipt_transfer(
    amount_out: float, amount_in: float,
    from_acc: Account, to_acc: Account,
    note: str, page_url: str,
) -> str:
    cross = from_acc.currency != to_acc.currency
    lines = ["✅ <b>Transfer logged</b>\n"]
    lines.append(f"🏦 From:  {_h(from_acc.name)} ({from_acc.currency})")
    lines.append(f"🏦 To:    {_h(to_acc.name)} ({to_acc.currency})")
    if cross:
        lines.append(f"💸 Out:   {_h(fmt_amount(amount_out, from_acc.currency))}")
        lines.append(f"💸 In:    {_h(fmt_amount(amount_in,  to_acc.currency))}")
    else:
        lines.append(f"💸 Amount: {_h(fmt_amount(amount_out, from_acc.currency))}")
    if note:
        lines.append(f"📝 Note:  {_h(note)}")
    lines.append(f"📅 Date:  {today}")
    lines += ["\n↩️ /undo to reverse", f'🔗 <a href="{page_url}">View in Notion</a>']
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Command handlers  — return (reply, page_id_or_None, db_name_or_None)
# ---------------------------------------------------------------------------

PageInfo = tuple[str, str]   # (page_id, database_name)
CmdResult = tuple[str, Optional[PageInfo]]


def cmd_expense(args: str) -> CmdResult:
    parsed, err = parse_expense(args)
    if err:
        return err, None

    account, err = resolve_account(parsed["account"])
    if err:
        return err, None

    category, err = resolve_category(parsed["category"])
    if err:
        return err, None

    try:
        page = notion_create_expense(
            parsed["amount"], account, category, parsed["note"]
        )
    except Exception as e:
        return f"❌ Notion write failed: {_h(str(e))}", None

    url = page.get("url", "")
    receipt = receipt_expense(
        parsed["amount"], account, category, parsed["note"], url
    )
    return receipt, (page["id"], "expenses")


def cmd_income(args: str) -> CmdResult:
    parsed, err = parse_income(args)
    if err:
        return err, None

    account, err = resolve_account(parsed["account"])
    if err:
        return err, None

    try:
        page = notion_create_income(parsed["amount"], account, parsed["note"])
    except Exception as e:
        return f"❌ Notion write failed: {_h(str(e))}", None

    url = page.get("url", "")
    receipt = receipt_income(parsed["amount"], account, parsed["note"], url)
    return receipt, (page["id"], "income")


def cmd_transfer(args: str) -> CmdResult:
    parsed, err = parse_transfer(args)
    if err:
        return err, None

    from_acc, err = resolve_account(parsed["from_account"])
    if err:
        return err, None

    to_acc, err = resolve_account(parsed["to_account"])
    if err:
        return err, None

    amount_out: float = parsed["amount_out"]
    amount_in_raw: Optional[float] = parsed["amount_in"]

    cross_currency = from_acc.currency != to_acc.currency

    if cross_currency:
        if amount_in_raw is None:
            return (
                f"❌ {_h(from_acc.name)} ({from_acc.currency}) → "
                f"{_h(to_acc.name)} ({to_acc.currency}) is a cross-currency transfer.\n"
                f"Both amounts are required.\n\n"
                f"Example:\n"
                f"/t 100 @mashreq 4950 @cash USD to EGP"
            ), None
        amount_in = amount_in_raw
    else:
        amount_in = amount_out   # same-currency: amounts are equal

    try:
        page = notion_create_transfer(
            amount_out, amount_in, from_acc, to_acc, parsed["note"]
        )
    except Exception as e:
        return f"❌ Notion write failed: {_h(str(e))}", None

    url = page.get("url", "")
    receipt = receipt_transfer(
        amount_out, amount_in, from_acc, to_acc, parsed["note"], url
    )
    return receipt, (page["id"], "transfer")


# ---------------------------------------------------------------------------
# Balance computation  (spec: never read rollup/formula — compute ourselves)
# ---------------------------------------------------------------------------


def _sum_number_prop(pages: list[dict], prop: str) -> float:
    total = 0.0
    for page in pages:
        total += page["properties"][prop].get("number") or 0.0
    return total


def _rel_filter(prop: str, page_id: str) -> dict:
    return {"property": prop, "relation": {"contains": page_id}}


def compute_balance(account: Account) -> float:
    """Compute balance for a single account via four filtered DB queries."""
    acc_id = account.page_id
    balance = account.initial_amount

    balance += _sum_number_prop(
        notion_query_all(DB_INCOME, filter=_rel_filter("Accounts", acc_id)),
        INCOME_SCHEMA.amount_prop,
    )
    balance -= _sum_number_prop(
        notion_query_all(DB_EXPENSES, filter=_rel_filter("Accounts", acc_id)),
        "Total Amount",
    )
    balance -= _sum_number_prop(
        notion_query_all(DB_TRANSFER, filter=_rel_filter("From Account", acc_id)),
        "Amount Out",
    )
    balance += _sum_number_prop(
        notion_query_all(DB_TRANSFER, filter=_rel_filter("To Account", acc_id)),
        "Amount In",
    )
    return balance


def compute_all_balances() -> dict[str, float]:
    """
    Compute balances for all accounts in 3 DB scans instead of 4×N queries.
    Returns {page_id: balance}.
    """
    balances = {acc.page_id: acc.initial_amount for acc in ACCOUNTS}

    for page in notion_query_all(DB_INCOME):
        amt = page["properties"][INCOME_SCHEMA.amount_prop].get("number") or 0.0
        for rel in page["properties"]["Accounts"]["relation"]:
            if rel["id"] in balances:
                balances[rel["id"]] += amt

    for page in notion_query_all(DB_EXPENSES):
        amt = page["properties"]["Total Amount"].get("number") or 0.0
        for rel in page["properties"]["Accounts"]["relation"]:
            if rel["id"] in balances:
                balances[rel["id"]] -= amt

    for page in notion_query_all(DB_TRANSFER):
        amt_out = page["properties"]["Amount Out"].get("number") or 0.0
        amt_in  = page["properties"]["Amount In"].get("number") or 0.0
        for rel in page["properties"]["From Account"]["relation"]:
            if rel["id"] in balances:
                balances[rel["id"]] -= amt_out
        for rel in page["properties"]["To Account"]["relation"]:
            if rel["id"] in balances:
                balances[rel["id"]] += amt_in

    return balances


def cmd_balance(args: str) -> str:
    """
    /b           → all accounts grouped by currency
    /b @account  → single account
    """
    args = args.strip()

    if args:
        # Single account
        account, err = resolve_account(args)
        if err:
            return err
        balance = compute_balance(account)
        flag = CURRENCY_FLAG.get(account.currency, "💰")
        return (
            f"🏦 <b>{_h(account.name)}</b> ({account.currency}) {flag}\n\n"
            f"💰 Balance: <b>{_h(fmt_amount(balance, account.currency))}</b>"
        )

    # All accounts — batch scan
    from collections import defaultdict
    all_bal = compute_all_balances()

    by_currency: dict[str, list[tuple[str, float]]] = defaultdict(list)
    for acc in sorted(ACCOUNTS, key=lambda a: a.name):
        bal = all_bal.get(acc.page_id, acc.initial_amount)
        by_currency[acc.currency].append((acc.name, bal))

    lines = ["📊 <b>Balances</b>\n"]
    for currency in sorted(by_currency.keys()):
        flag = CURRENCY_FLAG.get(currency, "💰")
        lines.append(f"{flag} <b>{currency}</b>")
        for name, bal in by_currency[currency]:
            lines.append(f"  · {_h(name)}: {_h(fmt_amount(bal, currency))}")
        lines.append("")
    return "\n".join(lines).strip()


# ---------------------------------------------------------------------------
# /s — monthly spending vs budget
# ---------------------------------------------------------------------------


def fmt_num(n: float) -> str:
    v: int | float = int(n) if n == int(n) else round(n, 2)
    return f"{v:,}"


def cmd_spending(args: str) -> str:
    """
    /s          → current month (Cairo time)
    /s 2026-05  → specific month
    """
    args = args.strip()
    if args:
        try:
            parts = args.split("-")
            if len(parts) != 2:
                raise ValueError
            year, month = int(parts[0]), int(parts[1])
            datetime(year, month, 1)
        except (ValueError, TypeError):
            return (
                f"❌ Invalid month: '{_h(args)}'\n\n"
                f"Format is YYYY-MM.\n\n"
                f"Example:\n/s 2026-05"
            )
    else:
        now = datetime.now(CAIRO_TZ)
        year, month = now.year, now.month

    import calendar
    last_day = calendar.monthrange(year, month)[1]
    start = f"{year:04d}-{month:02d}-01"
    end = f"{year:04d}-{month:02d}-{last_day:02d}"

    pages = notion_query_all(DB_EXPENSES, filter={"and": [
        {"property": "Date", "date": {"on_or_after": start}},
        {"property": "Date", "date": {"on_or_before": end}},
    ]})

    spent: dict[str, float] = {c.page_id: 0.0 for c in CATEGORIES}
    uncategorized = 0.0
    total = 0.0
    for page in pages:
        amt = page["properties"]["Total Amount"].get("number") or 0.0
        total += amt
        rels = page["properties"]["Categories"]["relation"]
        if not rels:
            uncategorized += amt
        for rel in rels:
            if rel["id"] in spent:
                spent[rel["id"]] += amt

    month_label = f"{year:04d}-{month:02d}"
    lines = [f"📈 <b>Spending — {month_label}</b>\n"]

    rows = [(c, spent[c.page_id]) for c in CATEGORIES
            if spent[c.page_id] > 0 or c.monthly_budget > 0]
    rows.sort(key=lambda r: r[1], reverse=True)

    if not rows and uncategorized == 0:
        return (
            f"📈 <b>Spending — {month_label}</b>\n\n"
            f"No expenses recorded this month.\n\n"
            f"Example:\n/e 350 Transportation uber @cash"
        )

    for cat, amount in rows:
        if cat.monthly_budget > 0:
            pct = amount / cat.monthly_budget * 100
            mark = "🔴" if pct > 100 else ("⚠️" if pct >= 80 else "✅")
            lines.append(
                f"{mark} {_h(cat.name)}: "
                f"{fmt_num(amount)} / {fmt_num(cat.monthly_budget)} ({pct:.0f}%)"
            )
        else:
            lines.append(f"▫️ {_h(cat.name)}: {fmt_num(amount)} (no budget)")

    if uncategorized > 0:
        lines.append(f"▫️ Uncategorized: {fmt_num(uncategorized)}")

    lines.append(f"\n💸 <b>Total: {fmt_num(total)}</b>")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# /undo
# ---------------------------------------------------------------------------

DB_LABELS = {"expenses": "Expense", "income": "Income", "transfer": "Transfer"}


def cmd_undo(state: dict) -> str:
    page_id = state.get("last_page_id")
    db = state.get("last_database")

    if not page_id:
        return (
            "🤷 Nothing to undo — no record created recently.\n\n"
            "Example:\n/e 350 Transportation uber @cash\n"
            "then /undo to reverse it"
        )

    try:
        resp = notion.pages.update(page_id=page_id, archived=True)
    except Exception as e:
        return (
            f"❌ Undo failed: {_h(str(e))}\n\n"
            f"The record may have been deleted manually already."
        )

    if resp.get("archived") is not True:
        return "❌ Undo failed: Notion did not archive the record."

    state["last_page_id"] = None
    state["last_database"] = None
    label = DB_LABELS.get(db, db or "Record")
    return f"↩️ <b>{label} deleted</b> — moved to Notion trash."


def cmd_accounts() -> str:
    from collections import defaultdict
    by_currency: dict[str, list[str]] = defaultdict(list)
    for acc in sorted(ACCOUNTS, key=lambda a: a.name):
        by_currency[acc.currency].append(acc.name)
    lines = ["<b>📊 Accounts</b>\n"]
    for currency in sorted(by_currency.keys()):
        flag = CURRENCY_FLAG.get(currency, "💰")
        lines.append(f"{flag} <b>{currency}</b>")
        for name in by_currency[currency]:
            lines.append(f"  · {_h(name)}")
        lines.append("")
    return "\n".join(lines).strip()


def cmd_help() -> str:
    return (
        "<b>Finance Bot — Commands</b>\n\n"
        "<b>Logging</b>\n"
        "<code>/e 350 Transportation uber @cash</code> — expense\n"
        "<code>/i 15000 salary june @nbe</code> — income\n"
        "<code>/t 2000 @nbe @cash</code> — transfer (same currency)\n"
        "<code>/t 100 @mashreq 4950 @cash</code> — transfer (cross-currency)\n\n"
        "<b>Balances</b>\n"
        "<code>/b</code> — all accounts\n"
        "<code>/b @nbe</code> — single account\n\n"
        "<b>Spending</b>\n"
        "<code>/s</code> — this month vs budget\n"
        "<code>/s 2026-05</code> — specific month\n\n"
        "<b>Other</b>\n"
        "<code>/accounts</code> — full account list\n"
        "<code>/undo</code> — archive last bot entry\n"
        "<code>/help</code> — this message\n\n"
        "<i>Free text also works:</i>\n"
        "<code>spent 250 on groceries @cash</code>\n"
        "<code>got paid 15000 salary @nbe</code>\n"
        "<code>transferred 2000 from nbe to cash</code>"
    )


# ---------------------------------------------------------------------------
# Message routing
# ---------------------------------------------------------------------------


def handle_message(msg: dict, state: dict) -> str:
    text = msg.get("text", "").strip()

    if text == "/accounts":
        return cmd_accounts()

    if text == "/help":
        return cmd_help()

    if text.startswith("/e ") or text == "/e":
        reply, page_info = cmd_expense(text[2:].strip())
        if page_info:
            state["last_page_id"], state["last_database"] = page_info
        return reply

    if text.startswith("/i ") or text == "/i":
        reply, page_info = cmd_income(text[2:].strip())
        if page_info:
            state["last_page_id"], state["last_database"] = page_info
        return reply

    if text.startswith("/t ") or text == "/t":
        reply, page_info = cmd_transfer(text[2:].strip())
        if page_info:
            state["last_page_id"], state["last_database"] = page_info
        return reply

    if text.startswith("/b ") or text == "/b":
        return cmd_balance(text[2:].strip())

    if text.startswith("/s ") or text == "/s":
        return cmd_spending(text[2:].strip())

    if text == "/undo":
        return cmd_undo(state)

    if text == "/start":
        return cmd_help()

    # Fallback — free text handled in Step 6
    return (
        "🤔 I didn't understand that.\n\n"
        "Try:\n"
        "<code>/e 350 Transportation uber @cash</code>\n"
        "<code>/i 15000 salary june @nbe</code>\n"
        "<code>/t 2000 @nbe @cash</code>\n"
        "or /help"
    )


# ---------------------------------------------------------------------------
# Notion Bot State
# ---------------------------------------------------------------------------

DEFAULT_STATE: dict = {
    "last_update_id": 0,
    "last_page_id":   None,
    "last_database":  None,
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
# Telegram command menu
# ---------------------------------------------------------------------------

BOT_COMMANDS = [
    {"command": "e",        "description": "💸 Log expense  →  /e 350 Food lunch @cash"},
    {"command": "i",        "description": "💰 Log income   →  /i 15000 salary @nbe"},
    {"command": "t",        "description": "🔄 Transfer     →  /t 2000 @nbe @cash"},
    {"command": "b",        "description": "📊 Balances     →  /b  or  /b @nbe"},
    {"command": "s",        "description": "📈 Spending     →  /s  or  /s 2026-05"},
    {"command": "accounts", "description": "🏦 All accounts with currency"},
    {"command": "undo",     "description": "↩️ Undo the last entry the bot created"},
    {"command": "help",     "description": "❓ All commands with examples"},
]


def register_commands() -> None:
    resp = requests.post(
        f"{TELEGRAM_API}/setMyCommands",
        json={"commands": BOT_COMMANDS},
        timeout=15,
    )
    resp.raise_for_status()
    if not resp.json().get("ok"):
        raise RuntimeError(f"setMyCommands error: {resp.json()}")
    print("Command menu registered.")


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


def tg_send(chat_id: int, text: str, parse_mode: str = "HTML") -> None:
    resp = requests.post(
        f"{TELEGRAM_API}/sendMessage",
        json={"chat_id": chat_id, "text": text, "parse_mode": parse_mode,
              "disable_web_page_preview": True},
        timeout=20,
    )
    resp.raise_for_status()
    if not resp.json().get("ok"):
        raise RuntimeError(f"sendMessage error: {resp.json()}")


# ---------------------------------------------------------------------------
# Main batch loop
# ---------------------------------------------------------------------------


def main() -> None:
    register_commands()
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
            reply = handle_message(msg, state)
            tg_send(chat_id, reply)
            print(f"Replied to update {update_id}.")
        except Exception:
            err = traceback.format_exc()
            failures.append((update_id, err))
            print(f"ERROR on update {update_id}:\n{err}")
            try:
                tg_send(chat_id, "⚠️ Internal error. Please try again.", parse_mode="")
            except Exception:
                pass

    state["last_update_id"] = new_offset
    save_bot_state(state)
    print(f"State saved. last_update_id={new_offset}")

    if failures:
        print(f"\n{len(failures)} failure(s):")
        for uid, err in failures:
            print(f"  update_id={uid}: {err}")
        sys.exit(1)


if __name__ == "__main__":
    main()
