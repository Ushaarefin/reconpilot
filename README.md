# ReconPilot: an AI reconciliation agent for subscription billing on Braintree

Subscription businesses keep two sets of truth: **Braintree**, which actually moves the money, and **their own app database**, which decides who gets access. When the two drift apart, money leaks quietly. Failed renewals keep their access, webhooks get lost, retries double-charge customers, price rises never reach the billing system, and refunds made in the control panel never reach the app.

**ReconPilot** pulls live subscription and transaction data from Braintree, reconciles it against the company's ledger, and works out how much revenue is at risk. An AI agent then investigates each break: it explains the root cause, estimates the impact, drafts the customer email and proposes a fix. Once a human approves, it **executes the fix through the Braintree API** (refund, price update) or in the app ledger.

## What it detects

| Break | Example | Fix the agent executes |
|---|---|---|
| Access without payment | Braintree subscription **Canceled**, app still **active** | Revoke access in ledger |
| Failed renewal, still has access | Braintree **Past Due**, app still **active** | Pause access, dunning email |
| Ghost subscription | Active in app, no Braintree subscription at all | Revoke access |
| Paid but not provisioned | Braintree charging, app has no record (lost webhook) | Provision access |
| Price mismatch | App says $14.99, Braintree bills $9.99 | **Braintree:** `subscription.update` to the correct price |
| Duplicate charge | Two identical charges on the same day | **Braintree:** `transaction.refund` |
| Refund not reflected | Refunded in Braintree, app still shows paid | Mark refunded and cancel access |

## PayPal / Braintree usage
- **Braintree Python SDK, Sandbox environment**
- `plan.create` / `plan.find`: creates the subscription plan
- `customer.create` with vaulted payment methods (`fake-valid-visa-nonce`)
- `subscription.create`, `subscription.cancel`, `subscription.find`, `subscription.update`
- `transaction.sale`, `transaction.search`, `transaction.refund`, `transaction.void`
- Sandbox testing APIs: `testing.make_past_due`, `testing.settle_transaction`, used to build realistic scenarios such as failed renewals and settled refunds

## AI usage
- **Anthropic Claude** (default `claude-sonnet-5-5`; OpenAI is supported as an alternative)
  1. **Break investigation:** given the ledger and Braintree record, the model returns structured JSON with root cause, impact, recommendation, confidence and a draft customer email.
  2. **Agentic remediation:** the AI recommends one of a fixed set of safe actions. A human clicks *Approve*, and the agent calls Braintree or updates the ledger.
  3. **"Ask your books":** natural-language questions over the live reconciliation, e.g. *"How much are we losing to failed renewals?"*
- With no AI key set, a rule-based fallback keeps the app usable.
- **AG Grid Community** powers the reconciliation grid (sorting, filtering, severity highlighting).
- Built with help from Claude as a coding assistant.

## Run it locally (about 5 minutes)

1. Create a free Braintree sandbox account at https://www.braintreepayments.com/sandbox and copy your **Merchant ID, Public Key, Private Key** (gear icon > API > API Keys).
2. Install and run:
   ```bash
   git clone <this repo> && cd reconpilot
   python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
   pip install -r requirements.txt
   python app.py
   ```
   Before running, create a `.env` file in the project folder:
   ```bash
   BT_MERCHANT_ID=your_merchant_id
   BT_PUBLIC_KEY=your_public_key
   BT_PRIVATE_KEY=your_private_key
   # Optional
   BT_CURRENCY=                # auto-detected if blank
   ANTHROPIC_API_KEY=          # or OPENAI_API_KEY; without either, a rule-based fallback is used
   AI_MODEL=                   # e.g. claude-sonnet-5-5 or gpt-4o-mini
   ```
3. Open http://localhost:5000
4. Click **1 · Seed Braintree sandbox**. This creates 13 customers, a plan, subscriptions, charges, a duplicate charge, a refund and a past-due renewal in *your* sandbox, plus an internal ledger with realistic errors (takes about 30–60s).
5. Click a red row, then **Investigate with AI**, then **Approve & execute fix**. Watch the match rate climb to 100%.

If plan creation fails on your sandbox, create a plan manually in the sandbox control panel (Subscriptions > Plans) with ID `reconpilot-monthly`, price 9.99, monthly.

## Deploy on Render
`render.yaml` is included: create a new Blueprint from the repo and add the environment variables.

## Architecture
```
Braintree Sandbox  <──SDK──  recon.py (seed · reconcile · fix) ──  app.py (Flask API)  ──  index.html (AG Grid UI)
                                                ai.py (Claude: explain · recommend · ask)
```

## What's next
- Braintree webhooks for continuous, real-time reconciliation
- Connectors for Stripe-style app databases, Postgres and accounting tools (Xero, QuickBooks)
- Auto-approve policies for low-risk fixes, plus an audit trail for finance teams

## License
MIT
