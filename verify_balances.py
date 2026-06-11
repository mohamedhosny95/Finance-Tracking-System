"""
One-off verification script — runs in GitHub Actions where secrets live.

1. Validates all 5 Notion DB schemas (bot's own startup path).
2. Computes every account balance with the bot's own logic, two ways
   (batch scan + per-account filtered queries) — they must agree.
3. Cross-checks against Notion's Current Balance formula property,
   flagging accounts whose relation count exceeds 25 (where the API
   value is known to be unreliable).
4. Test-writes one Expense, one Income, one Transfer via the bot's
   write functions, then archives all three immediately.
5. Prints Bot State page content (read-only).

Never touches Telegram — safe to run alongside the bot.
"""

import sys
import traceback

import bot
from bot import (
    notion, notion_query_all, validate_and_load_schemas,
    compute_balance, compute_all_balances, fmt_amount,
    load_bot_state,
    DB_INCOME, DB_EXPENSES, DB_TRANSFER,
    notion_create_expense, notion_create_income, notion_create_transfer,
)

FAILED = False


def fail(msg: str) -> None:
    global FAILED
    FAILED = True
    print(f"  ❌ {msg}")


def section(title: str) -> None:
    print(f"\n{'=' * 60}\n{title}\n{'=' * 60}")


def main() -> None:
    section("1. SCHEMA VALIDATION + CACHE LOAD")
    validate_and_load_schemas()
    print(f"  ✅ Schemas valid. {len(bot.ACCOUNTS)} accounts, "
          f"{len(bot.CATEGORIES)} categories.")
    for acc in sorted(bot.ACCOUNTS, key=lambda a: a.name):
        print(f"     · {acc.name}  [{acc.currency}]  initial={acc.initial_amount}")

    section("2. BALANCE COMPUTATION — batch scan vs per-account queries")
    batch = compute_all_balances()

    # Count per-account transaction rows while we're at it
    counts: dict[str, int] = {a.page_id: 0 for a in bot.ACCOUNTS}
    for db, props in [
        (DB_INCOME,   ["Accounts"]),
        (DB_EXPENSES, ["Accounts"]),
        (DB_TRANSFER, ["From Account", "To Account"]),
    ]:
        for page in notion_query_all(db):
            for prop in props:
                for rel in page["properties"][prop]["relation"]:
                    if rel["id"] in counts:
                        counts[rel["id"]] += 1

    for acc in sorted(bot.ACCOUNTS, key=lambda a: a.name):
        single = compute_balance(acc)
        b = batch[acc.page_id]
        status = "✅" if abs(single - b) < 0.005 else "❌"
        if status == "❌":
            fail(f"{acc.name}: batch={b} != single={single}")
        print(f"  {status} {acc.name}: {fmt_amount(b, acc.currency)} "
              f"({counts[acc.page_id]} transactions)")

    section("3. CROSS-CHECK vs Notion 'Current Balance' formula")
    for acc in sorted(bot.ACCOUNTS, key=lambda a: a.name):
        page = notion.pages.retrieve(page_id=acc.page_id)
        formula = page["properties"].get("Current Balance", {})
        formula_val = (formula.get("formula") or {}).get("number")
        computed = batch[acc.page_id]
        n_tx = counts[acc.page_id]
        if formula_val is None:
            print(f"  ⚠️ {acc.name}: formula returned None (computed: {computed})")
        elif abs(formula_val - computed) < 0.005:
            print(f"  ✅ {acc.name}: {computed} == formula {formula_val}")
        elif n_tx > 25:
            print(f"  ⚠️ {acc.name}: computed {computed} != formula {formula_val} "
                  f"— account has {n_tx} relations (>25), API formula value "
                  f"is unreliable here; computed value is authoritative")
        else:
            fail(f"{acc.name}: computed {computed} != formula {formula_val} "
                 f"with only {n_tx} relations — needs investigation")

    section("4. WRITE TEST — create + archive one of each record type")
    egp_accounts = [a for a in bot.ACCOUNTS if a.currency == "EGP"]
    acc1 = egp_accounts[0] if egp_accounts else bot.ACCOUNTS[0]
    acc2 = egp_accounts[1] if len(egp_accounts) > 1 else bot.ACCOUNTS[-1]
    cat = bot.CATEGORIES[0]
    created: list[tuple[str, str]] = []

    try:
        p = notion_create_expense(1.0, acc1, cat, "BOT-VERIFY-TEST (auto-archived)")
        created.append(("Expense", p["id"]))
        print(f"  ✅ Expense created: {p['id']}")

        p = notion_create_income(1.0, acc1, "BOT-VERIFY-TEST (auto-archived)")
        created.append(("Income", p["id"]))
        print(f"  ✅ Income created: {p['id']}")

        p = notion_create_transfer(1.0, 1.0, acc1, acc2,
                                   "BOT-VERIFY-TEST (auto-archived)")
        created.append(("Transfer", p["id"]))
        print(f"  ✅ Transfer created: {p['id']}")
    except Exception:
        fail(f"Write test failed:\n{traceback.format_exc()}")
    finally:
        for label, page_id in created:
            try:
                resp = notion.pages.update(page_id=page_id, archived=True)
                ok = resp.get("archived") is True
                print(f"  {'✅' if ok else '❌'} {label} archived: {page_id}")
                if not ok:
                    fail(f"{label} {page_id} did not archive!")
            except Exception:
                fail(f"Could not archive {label} {page_id} — "
                     f"DELETE IT MANUALLY:\n{traceback.format_exc()}")

    section("5. BOT STATE (read-only)")
    state = load_bot_state()
    print(f"  {state}")

    section("RESULT")
    if FAILED:
        print("  ❌ VERIFICATION FAILED — see errors above")
        sys.exit(1)
    print("  ✅ ALL CHECKS PASSED")


if __name__ == "__main__":
    main()
# re-trigger verify 21
