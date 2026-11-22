# 18-procure-to-pay

Procure-to-pay on the agent container: a simulated ERP served over MCP, invoice extraction, a three-way match and
staged, separately approved postings. Part of the **30 apps in 30 days** AWS demo track (apps 17 to 20).

## What this app does

A simulated ERP — suppliers, purchase orders, goods receipts, invoices, and staged postings, credits and
holds — served over the `p2p-erp` MCP gateway. Read tools are scoped to the session's company code and come
back capped and paged. Write tools record a staged change and return its `change_key`; they do not apply it.
An approver applies a staged change as a second principal. `p2p ingest` extracts one invoice, `p2p match`
runs the fixed four-step match and may stage a posting, and `p2p staged`, `p2p review` and `p2p approve`
list, show and decide a posting, a credit memo or a payment hold. `p2p dispute` runs the supplier desk.
Batch runs and the recorded demo arrive in later slices.

```bash
p2p-erp --database erp.sqlite3 --subject svc-app19 --role service --company-code 1000
```

## Develop

```bash
uv sync                      # Python 3.12; app 17 comes from its git tag
uv run ruff check . && uv run ruff format --check .
uv run mypy --strict
uv run pytest
```
