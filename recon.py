"""ReconPilot core: Braintree sandbox seeding, reconciliation engine and fix actions."""
import csv
import json
import os
import time
from decimal import Decimal

import braintree

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
LEDGER_PATH = os.path.join(DATA_DIR, "ledger.csv")
STATE_PATH = os.path.join(DATA_DIR, "state.json")
PLAN_ID = os.getenv("BT_PLAN_ID", "reconpilot-monthly")
LEDGER_FIELDS = ["subscription_id", "customer_name", "customer_email", "plan", "price", "status", "refunded"]

ACTIVE_BT = {"Active", "Pending"}
DECLINED_TX = {"processor_declined", "gateway_rejected", "failed"}
LIVE_TX = {"authorized", "submitted_for_settlement", "settling", "settled", "settlement_pending"}

BREAK_INFO = {
    "MATCHED": ("Matched", "none"),
    "ACCESS_WITHOUT_PAYMENT": ("Access without payment", "high"),
    "PAST_DUE_STILL_ACTIVE": ("Failed renewal, still has access", "high"),
    "GHOST_SUBSCRIPTION": ("Ghost subscription (not in Braintree)", "high"),
    "PAID_NOT_PROVISIONED": ("Paid but not provisioned", "high"),
    "AMOUNT_MISMATCH": ("Price mismatch", "medium"),
    "DUPLICATE_CHARGE": ("Duplicate charge", "high"),
    "REFUND_NOT_REFLECTED": ("Refund not reflected in ledger", "medium"),
}

# Which fix each break type gets, and whether it touches Braintree or our ledger
FIXES = {
    "ACCESS_WITHOUT_PAYMENT": ("revoke_access", "Ledger: set status to canceled (revoke access)"),
    "PAST_DUE_STILL_ACTIVE": ("suspend_access", "Ledger: set status to past_due (pause access, dunning email)"),
    "GHOST_SUBSCRIPTION": ("revoke_access", "Ledger: set status to canceled (no billing exists)"),
    "PAID_NOT_PROVISIONED": ("provision_access", "Ledger: create active record for paying customer"),
    "AMOUNT_MISMATCH": ("update_bt_price", "Braintree: update subscription price to the ledger price"),
    "DUPLICATE_CHARGE": ("refund_duplicate", "Braintree: refund the duplicate transaction"),
    "REFUND_NOT_REFLECTED": ("mark_refunded", "Ledger: mark refunded and cancel access"),
}


def gateway():
    mid, pub, priv = (os.getenv(k) for k in ("BT_MERCHANT_ID", "BT_PUBLIC_KEY", "BT_PRIVATE_KEY"))
    if not (mid and pub and priv):
        raise RuntimeError("Braintree sandbox keys missing: set BT_MERCHANT_ID, BT_PUBLIC_KEY, BT_PRIVATE_KEY in .env")
    return braintree.BraintreeGateway(braintree.Configuration(
        environment=braintree.Environment.Sandbox, merchant_id=mid, public_key=pub, private_key=priv))


# ---------------------------------------------------------------- storage
def load_ledger():
    if not os.path.exists(LEDGER_PATH):
        return []
    with open(LEDGER_PATH, newline="") as f:
        return list(csv.DictReader(f))


def save_ledger(rows):
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(LEDGER_PATH, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=LEDGER_FIELDS)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in LEDGER_FIELDS})


def load_state():
    if not os.path.exists(STATE_PATH):
        return {"customers": {}}
    with open(STATE_PATH) as f:
        return json.load(f)


def save_state(state):
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(STATE_PATH, "w") as f:
        json.dump(state, f, indent=2)


# ---------------------------------------------------------------- seeding
def _ensure_plan(gw):
    try:
        return gw.plan.find(PLAN_ID)
    except braintree.exceptions.NotFoundError:
        pass
    currency = os.getenv("BT_CURRENCY")
    if not currency:
        try:
            currency = next(iter(gw.merchant_account.all().merchant_accounts)).currency_iso_code
        except Exception:
            currency = "USD"
    res = gw.plan.create({"id": PLAN_ID, "name": "ReconPilot Pro Monthly", "price": "9.99",
                          "billing_frequency": 1, "currency_iso_code": currency})
    if not res.is_success:
        raise RuntimeError(f"Could not create plan '{PLAN_ID}': {res.message}. "
                           f"Create it manually in the Braintree sandbox (Subscriptions > Plans) with ID {PLAN_ID}.")
    return res.plan


CUSTOMERS = [
    # name, scenario
    ("Ava Patel", "clean"), ("Ben Okafor", "clean"), ("Chloe Evans", "clean"),
    ("Dev Sharma", "clean"), ("Ella Jones", "clean"), ("Finn Murphy", "clean"),
    ("Grace Liu", "canceled_in_bt"), ("Harry Price", "canceled_in_bt"),
    ("Isla Brown", "past_due"), ("Jack Wilson", "missing_in_ledger"),
    ("Kara Singh", "price_mismatch"), ("Leo Martin", "duplicate"),
    ("Maya Rossi", "refunded"),
]

# Braintree sandbox Visa/Mastercard test cards (many sandbox merchant accounts reject Amex/Discover/JCB).
# The duplicate-transaction check keys on card + expiry + amount, so every customer gets a card/expiry
# combination that is unique within a seed run and shifts between runs ("Gateway Rejected: duplicate" otherwise).
TEST_CARDS = [
    "4111111111111111", "4005519200000004", "4009348888881881", "4012000033330026",
    "4012000077777777", "4012888888881881", "4217651111111119", "4500600000000061",
    "4023898493988028", "5555555555554444", "2223000048400011",
]


def _card(i, run):
    r = int(run)
    return {"number": TEST_CARDS[i % len(TEST_CARDS)],
            "expiration_date": f"{(i + r) % 12 + 1:02d}/{2030 + (r // 12 + i // 12) % 10}"}


DUP_HINT = (" Braintree's duplicate-transaction check blocked it. Wait a minute and seed again, or turn off "
            "Settings > Processing > Duplicate Transaction Checking in the sandbox Control Panel.")


def _fail(what, result):
    msg = f"{what} failed: {result.message}"
    if "duplicate" in (result.message or "").lower():
        msg += DUP_HINT
    raise RuntimeError(msg)


def seed(progress=print):
    """Create realistic sandbox data in Braintree plus an internal ledger with planted errors."""
    gw = gateway()
    _ensure_plan(gw)
    run = str(int(time.time()))[-5:]
    state = {"customers": {}, "seeded_at": time.strftime("%Y-%m-%d %H:%M")}
    ledger = []
    for i, (name, scenario) in enumerate(CUSTOMERS):
        first, last = name.split()
        email = f"{first.lower()}.{last.lower()}+{run}@example.com"
        progress(f"Creating {name} ({scenario})")
        c = gw.customer.create({"first_name": first, "last_name": last, "email": email,
                                "credit_card": _card(i, run)})
        if not c.is_success:
            _fail(f"Customer create ({name})", c)
        token = c.customer.payment_methods[0].token
        price = "9.99"
        s = gw.subscription.create({"payment_method_token": token, "plan_id": PLAN_ID, "price": price})
        if not s.is_success:
            _fail(f"Subscription create ({name})", s)
        sub = s.subscription
        first_tx = sub.transactions[0].id if sub.transactions else None
        ledger_row = {"subscription_id": sub.id, "customer_name": name, "customer_email": email,
                      "plan": "Pro Monthly", "price": price, "status": "active", "refunded": "no"}

        if scenario == "canceled_in_bt":
            gw.subscription.cancel(sub.id)              # customer canceled at the bank side, app never heard
        elif scenario == "past_due":
            try:
                gw.testing.make_past_due(sub.id, 5)     # renewal failed 5 days ago (dev gateways only)
            except braintree.exceptions.NotFoundError:
                # Public sandbox has no make_past_due: plant a declined renewal charge instead.
                # Sandbox amounts 2000.00-2999.99 return "Processor Declined".
                gw.transaction.sale({"amount": "2001.00", "payment_method_token": token})
        elif scenario == "price_mismatch":
            ledger_row["price"] = "14.99"               # price rise applied in app, never pushed to Braintree
        elif scenario == "duplicate":
            # double charge (retry bug) -- charged to a second vaulted card so the gateway's
            # duplicate check doesn't reject it; the ledger/recon still see two 9.99 charges same day
            backup = _card(i + 5, run)
            backup["expiration_date"] = backup["expiration_date"][:3] + "2040"   # never collides with primaries
            cc = gw.credit_card.create({"customer_id": c.customer.id, **backup})
            if not cc.is_success:
                _fail(f"Backup card create ({name})", cc)
            d = gw.transaction.sale({"amount": price, "payment_method_token": cc.credit_card.token,
                                     "options": {"submit_for_settlement": True}})
            if not d.is_success:
                _fail(f"Duplicate charge ({name})", d)
        elif scenario == "refunded" and first_tx:
            gw.testing.settle_transaction(first_tx)
            r = gw.transaction.refund(first_tx)
            if not r.is_success:
                gw.transaction.void(first_tx)
            gw.subscription.cancel(sub.id)

        state["customers"][sub.id] = {"name": name, "email": email, "customer_id": c.customer.id,
                                      "token": token, "scenario": scenario}
        if scenario != "missing_in_ledger":           # webhook lost -> never provisioned
            ledger.append(ledger_row)

    # A ledger record with no Braintree subscription at all (manual comp / migration leftover)
    ledger.append({"subscription_id": f"legacy-{run}", "customer_name": "Noah Clarke",
                   "customer_email": f"noah.clarke+{run}@example.com", "plan": "Pro Monthly",
                   "price": "9.99", "status": "active", "refunded": "no"})
    save_ledger(ledger)
    save_state(state)
    progress("Done")
    return {"customers": len(CUSTOMERS), "ledger_rows": len(ledger)}


# ---------------------------------------------------------------- reconciliation
def _money(x):
    return float(Decimal(str(x or 0)))


def _customer_txs(gw, customer_id):
    try:
        return list(gw.transaction.search(braintree.TransactionSearch.customer_id == customer_id).items)
    except Exception:
        return []


def reconcile():
    gw = gateway()
    state = load_state()
    ledger = {r["subscription_id"]: r for r in load_ledger()}
    ids = list(dict.fromkeys(list(state["customers"].keys()) + list(ledger.keys())))
    rows = []
    for sid in ids:
        meta = state["customers"].get(sid, {})
        led = ledger.get(sid)
        try:
            sub = gw.subscription.find(sid)
        except braintree.exceptions.NotFoundError:
            sub = None
        txs = _customer_txs(gw, meta["customer_id"]) if meta.get("customer_id") else []
        sales = [t for t in txs if t.type == "sale"]
        live = [t for t in sales if t.status in LIVE_TX and not t.refund_ids]
        handled = set(state.get("handled_tx", []))   # duplicates we refunded ourselves
        refunded = [t for t in sales if (t.refund_ids or t.status == "voided") and t.id not in handled]

        base = {
            "subscription_id": sid,
            "customer": (led or {}).get("customer_name") or meta.get("name", "?"),
            "email": (led or {}).get("customer_email") or meta.get("email", ""),
            "ledger_status": led["status"] if led else "(missing)",
            "ledger_price": _money(led["price"]) if led else None,
            "bt_status": sub.status if sub else "(not found)",
            "bt_price": _money(sub.price) if sub else None,
            "bt_balance": _money(getattr(sub, "balance", 0)) if sub else None,
            "charges": len(live),
            "tx_id": None,
        }
        found = []
        led_active = bool(led) and led["status"] == "active"

        if led and not sub:
            if led_active:
                found.append(("GHOST_SUBSCRIPTION", _money(led["price"]),
                              "Customer has active access in our app but no Braintree subscription exists, so nobody is billing them."))
        elif sub and not led:
            if sub.status in ACTIVE_BT:
                found.append(("PAID_NOT_PROVISIONED", _money(sub.price),
                              "Braintree is charging this customer but our app has no record, so they paid and got nothing. Chargeback risk."))
        elif sub and led:
            if led_active and sub.status in ("Canceled", "Expired") and led.get("refunded") != "yes" and not refunded:
                found.append(("ACCESS_WITHOUT_PAYMENT", _money(led["price"]),
                              f"Braintree subscription is {sub.status} but our app still grants access. Unbilled usage every month."))
            declined = [t for t in sales if t.status in DECLINED_TX]
            if led_active and sub.status == "Past Due":
                found.append(("PAST_DUE_STILL_ACTIVE", _money(sub.balance or sub.price),
                              f"Renewal failed ({sub.days_past_due or '?'} days past due, balance {sub.balance}) but our app still shows active."))
            elif led_active and declined and sub.status in ACTIVE_BT:
                found.append(("PAST_DUE_STILL_ACTIVE", _money(sub.price),
                              f"Renewal charge {declined[-1].id} was declined ({declined[-1].processor_response_text or declined[-1].status}) but our app still shows active."))
            if sub.status in ACTIVE_BT and abs(_money(sub.price) - _money(led["price"])) > 0.001:
                found.append(("AMOUNT_MISMATCH", round(abs(_money(led["price"]) - _money(sub.price)), 2),
                              f"Ledger price {led['price']} but Braintree bills {sub.price} per month."))
            if refunded and led.get("refunded") != "yes":
                found.append(("REFUND_NOT_REFLECTED", sum(_money(t.amount) for t in refunded),
                              f"Braintree shows a refund/void ({refunded[0].id}) but our ledger still shows the customer as paid and {led['status']}."))
        # duplicate charges: more than one live charge of the same amount on the same day
        by_key = {}
        for t in live:
            by_key.setdefault((str(t.amount), t.created_at.date()), []).append(t)
        for (amt, day), group in by_key.items():
            if len(group) > 1:
                group.sort(key=lambda t: t.created_at)
                dup = next((t for t in group[1:] if not t.subscription_id), group[-1])
                base["tx_id"] = dup.id
                found.append(("DUPLICATE_CHARGE", _money(amt),
                              f"{len(group)} charges of {amt} on {day}. Transaction {dup.id} looks like a retry duplicate."))

        if not found:
            rows.append({**base, "break_type": "MATCHED", "label": "Matched", "severity": "none",
                         "amount_at_risk": 0.0, "detail": "Ledger and Braintree agree."})
        for btype, amt, detail in found:
            label, sev = BREAK_INFO[btype]
            rows.append({**base, "break_type": btype, "label": label, "severity": sev,
                         "amount_at_risk": round(amt, 2), "detail": detail,
                         "fix_action": FIXES[btype][0], "fix_label": FIXES[btype][1]})
    for i, r in enumerate(rows):
        r["id"] = i
    breaks = [r for r in rows if r["break_type"] != "MATCHED"]
    subs_total = len(ids)
    clean = len({r["subscription_id"] for r in rows} - {r["subscription_id"] for r in breaks})
    summary = {
        "subscriptions": subs_total,
        "breaks": len(breaks),
        "match_rate": round(100 * clean / subs_total, 1) if subs_total else 0,
        "amount_at_risk": round(sum(r["amount_at_risk"] for r in breaks), 2),
        "by_type": {},
        "seeded_at": state.get("seeded_at"),
    }
    for b in breaks:
        summary["by_type"][b["label"]] = summary["by_type"].get(b["label"], 0) + 1
    return {"summary": summary, "rows": rows}


# ---------------------------------------------------------------- fixes
def apply_fix(row):
    """Execute the approved fix. Returns a human-readable result string."""
    gw = gateway()
    action = row.get("fix_action")
    sid = row["subscription_id"]
    ledger = load_ledger()
    led = next((r for r in ledger if r["subscription_id"] == sid), None)

    if action == "refund_duplicate":
        tx_id = row["tx_id"]
        st = load_state()
        st.setdefault("handled_tx", []).append(tx_id)
        save_state(st)
        try:
            gw.testing.settle_transaction(tx_id)   # sandbox only: refunds need a settled transaction
        except Exception:
            pass
        r = gw.transaction.refund(tx_id)
        if r.is_success:
            return f"Braintree refund {r.transaction.id} issued for duplicate {tx_id}."
        v = gw.transaction.void(tx_id)
        if v.is_success:
            return f"Braintree transaction {tx_id} voided."
        raise RuntimeError(f"Refund failed: {r.message}")

    if action == "update_bt_price":
        r = gw.subscription.update(sid, {"price": f"{row['ledger_price']:.2f}"})
        if not r.is_success:
            raise RuntimeError(r.message)
        return f"Braintree subscription {sid} now bills {r.subscription.price}."

    if action == "provision_access":
        meta = load_state()["customers"].get(sid, {})
        ledger.append({"subscription_id": sid, "customer_name": meta.get("name", row["customer"]),
                       "customer_email": meta.get("email", row["email"]), "plan": "Pro Monthly",
                       "price": f"{row['bt_price']:.2f}", "status": "active", "refunded": "no"})
        save_ledger(ledger)
        return f"Access provisioned in app for {row['customer']}."

    if not led:
        raise RuntimeError("No ledger row to update")
    if action == "revoke_access":
        led["status"] = "canceled"
        msg = f"Access revoked for {row['customer']} in app ledger."
    elif action == "suspend_access":
        led["status"] = "past_due"
        msg = f"{row['customer']} moved to past_due; access paused pending payment."
    elif action == "mark_refunded":
        led["refunded"], led["status"] = "yes", "canceled"
        msg = f"Ledger updated: {row['customer']} marked refunded and canceled."
    else:
        raise RuntimeError(f"Unknown action {action}")
    save_ledger(ledger)
    return msg
