# 18-procure-to-pay

Procure-to-pay on the agent container: a simulated ERP served over MCP, invoice extraction, a three-way match and
staged, separately approved postings. Part of the **30 apps in 30 days** AWS demo track (apps 17 to 20).

## What this app does

_Scaffolding only so far (slice 18.1): the Python package, CI and the pin of app 17 (`v0.1.0`). The full description
arrives with the README slice (18.10)._

## Develop

```bash
uv sync                      # Python 3.12; app 17 comes from its git tag
uv run ruff check . && uv run ruff format --check .
uv run mypy --strict
uv run pytest
```
