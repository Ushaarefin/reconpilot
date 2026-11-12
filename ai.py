"""AI layer: explains reconciliation breaks, drafts customer emails, answers questions about the books.
Uses Anthropic Claude if ANTHROPIC_API_KEY is set, else OpenAI if OPENAI_API_KEY is set,
else a rule-based fallback so the app still works offline."""
import json
import os
import re

import requests

SYSTEM = ("You are ReconPilot, a senior payments operations analyst for a subscription business that bills "
          "through Braintree (PayPal). You reconcile the company's internal subscription ledger against Braintree. "
          "Be precise, concise and practical. Money amounts are per month unless stated.")


def provider():
    if os.getenv("ANTHROPIC_API_KEY"):
        return "claude"
    if os.getenv("OPENAI_API_KEY"):
        return "openai"
    return "rules"


def _llm(prompt, max_tokens=700):
    p = provider()
    if p == "claude":
        r = requests.post("https://api.anthropic.com/v1/messages", timeout=60, headers={
            "x-api-key": os.environ["ANTHROPIC_API_KEY"], "anthropic-version": "2023-06-01",
            "content-type": "application/json"}, json={
            "model": os.getenv("AI_MODEL", "claude-sonnet-5-5"), "max_tokens": max_tokens,
            "system": SYSTEM, "messages": [{"role": "user", "content": prompt}]})
        r.raise_for_status()
        return "".join(b.get("text", "") for b in r.json()["content"])
    if p == "openai":
        r = requests.post("https://api.openai.com/v1/chat/completions", timeout=60, headers={
            "Authorization": f"Bearer {os.environ['OPENAI_API_KEY']}"}, json={
            "model": os.getenv("AI_MODEL", "gpt-4o-mini"), "max_tokens": max_tokens,
            "messages": [{"role": "system", "content": SYSTEM}, {"role": "user", "content": prompt}]})
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"]
    return None


def _json(text):
    m = re.search(r"\{.*\}", text or "", re.S)
    return json.loads(m.group(0)) if m else None


def explain(row):
    """Root cause, business impact, recommended fix and a customer email for one break."""
    prompt = f"""A reconciliation break was found between our ledger and Braintree.

Break record (JSON):
{json.dumps(row, default=str, indent=2)}

The only fix the system can execute for this break type is: "{row.get('fix_label')}".

Return ONLY a JSON object with these keys:
- "root_cause": 1-2 sentences on the most likely operational cause (e.g. lost webhook, retry bug, price change not synced)
- "impact": 1 sentence on revenue / customer / compliance impact, with the amount
- "recommendation": 1-2 sentences, should the system's fix be approved, and anything a human should also do
- "confidence": "high" | "medium" | "low"
- "customer_email": a short, warm email to the customer (subject line first, then body; sign off as "The Billing Team"), or "" if no email should be sent
"""
    try:
        out = _json(_llm(prompt))
        if out:
            out["provider"] = provider()
            return out
    except Exception as e:
        err = str(e)
    else:
        err = None
    fb = _rules_explain(row)
    if err:
        fb["note"] = f"AI unavailable ({err[:120]}), rule-based explanation shown."
    return fb


def ask(question, recon):
    """Natural-language question answered over the current reconciliation result."""
    slim = [{k: r.get(k) for k in ("customer", "label", "severity", "amount_at_risk", "ledger_status",
                                   "bt_status", "ledger_price", "bt_price", "detail")} for r in recon["rows"]]
    prompt = f"""Current reconciliation summary: {json.dumps(recon['summary'])}
Rows: {json.dumps(slim)}

Question from the finance team: {question}

Answer in at most 5 short sentences or bullets. Quote exact numbers from the data. If the data can't answer it, say so."""
    try:
        ans = _llm(prompt, 500)
        if ans:
            return {"answer": ans.strip(), "provider": provider()}
    except Exception as e:
        return {"answer": f"AI unavailable: {e}", "provider": "error"}
    return _rules_ask(question, recon)


# question keywords -> break types they are about (used when no AI key is configured)
TOPICS = [
    (("renewal", "past due", "past-due", "dunning", "declin", "failed"), ["PAST_DUE_STILL_ACTIVE"]),
    (("refund", "void"), ["REFUND_NOT_REFLECTED"]),
    (("duplicate", "double", "twice", "retry"), ["DUPLICATE_CHARGE"]),
    (("price", "mismatch", "amount", "undercharg", "overcharg"), ["AMOUNT_MISMATCH"]),
    (("ghost", "legacy", "comp", "migration", "free"), ["GHOST_SUBSCRIPTION"]),
    (("cancel", "churn", "without pay", "unbilled", "leak"), ["ACCESS_WITHOUT_PAYMENT", "GHOST_SUBSCRIPTION"]),
    (("provision", "not provisioned", "paid but", "chargeback", "webhook"), ["PAID_NOT_PROVISIONED"]),
]


def _rules_ask(question, recon):
    """Keyword-routed answer built straight from the reconciliation rows."""
    q = question.lower()
    s = recon["summary"]
    breaks = [r for r in recon["rows"] if r.get("break_type") not in (None, "MATCHED")]
    wanted = [t for keys, types in TOPICS if any(k in q for k in keys) for t in types]
    hits = [r for r in breaks if r["break_type"] in wanted] if wanted else breaks

    groups = {}
    for r in hits:
        g = groups.setdefault(r["label"], {"label": r["label"], "severity": r.get("severity"),
                                           "count": 0, "amount": 0.0, "customers": []})
        g["count"] += 1
        g["amount"] = round(g["amount"] + float(r.get("amount_at_risk") or 0), 2)
        g["customers"].append(r["customer"])
    items = sorted(groups.values(), key=lambda g: -g["amount"])
    total = round(sum(g["amount"] for g in items), 2)

    if wanted and not hits:
        headline = "Nothing in the current books matches that. No open breaks of this type."
    elif wanted:
        n = sum(g["count"] for g in items)
        headline = (f"${total:,.2f}/month at risk across {n} subscription{'s' if n != 1 else ''}: "
                    + ", ".join(c for g in items for c in g["customers"]) + ".")
    else:
        headline = (f"{s['breaks']} breaks across {s['subscriptions']} subscriptions, "
                    f"${s['amount_at_risk']:,.2f}/month at risk ({s['match_rate']}% match rate).")
    return {"answer": headline, "items": items, "provider": "rules"}


def _rules_explain(row):
    t = row["break_type"]
    causes = {
        "ACCESS_WITHOUT_PAYMENT": "The cancellation webhook from Braintree was likely missed, so the app never revoked access.",
        "PAST_DUE_STILL_ACTIVE": "Dunning is not wired to the app: Braintree marked the renewal failed but access was never paused.",
        "GHOST_SUBSCRIPTION": "Likely a manual comp or data-migration leftover with no billing behind it.",
        "PAID_NOT_PROVISIONED": "The subscription-created webhook was likely lost, so the customer was never provisioned.",
        "AMOUNT_MISMATCH": "A price change was applied in the app but never pushed to the Braintree subscription.",
        "DUPLICATE_CHARGE": "A checkout retry or double-click created a second charge.",
        "REFUND_NOT_REFLECTED": "Support refunded in the Braintree control panel without updating the app.",
    }
    return {"root_cause": causes.get(t, ""), "impact": f"{row['amount_at_risk']} at risk.",
            "recommendation": f"Approve: {row.get('fix_label')}.", "confidence": "medium",
            "customer_email": "", "provider": "rules"}
