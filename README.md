# Procure-to-pay workflow

> Accounts payable keys supplier invoices in by hand, matches them against a purchase order and a goods receipt, and argues with suppliers about the differences. This app is the first agent workflow on top of that back office: extract, match, stage, approve, and a supplier disputes desk.

<!-- DRAFT: placeholders below are marked TODO(deploy). Fill them in after 30-apps-infra#97 (18.9) is live. -->

**Live:** TODO(deploy): `https://app18.breakthruventures.ai/health` once 30-apps-infra#97 is applied and the live smoke passes. · **Runs on:** AWS (Lambda, us-east-2) · **Imports:** [app 17](https://github.com/BreakthruDevelopment/17-agent-container), pinned by tag · **Imported by:** app 19, through the MCP gateway contract below · Part of [30 apps in 30 days](https://github.com/BreakthruDevelopment/ai-dev-os).

There is no web UI. The demo is the command line, and the live surface is one health endpoint.

## What this app does

- **Clerk.** `p2p ingest <file>` extracts one invoice into a validated draft. `p2p match <invoice>` runs the same four steps every time: extract, fetch the PO, fetch the goods receipt, decide. It stages a posting or stops with a stated reason. `p2p batch <dir>` does both for every document in a folder (25 at most), isolates each failure, and prints one summary.
- **Approver.** `p2p staged` lists what the approver may decide. `p2p review <change>` shows a staged change and what to check. `p2p approve <change> --yes` or `--reject <reason>` applies or rejects it. An approver can never apply a change they staged.
- **Disputes agent.** `p2p dispute open/step/show <case>` runs the supplier disputes desk one step at a time. It reads, asks one clarifying question when nothing matches, proposes a credit or a payment hold as a staged change, and escalates on five written criteria.
- **Another system (app 19).** `p2p-erp` is an MCP server over stdio. The `service` role sees the six read tools for one company code.
- **Anyone.** `p2p demo` tells the whole story on recorded replies with no network. `--live` sends the same story to Bedrock.

The system of record is a simulated ERP: a SQLite file built from [`erp/schema.sql`](erp/schema.sql) and a deterministic seed ([`erp/seed.sql`](erp/seed.sql)). There is no Oracle and no SOA Suite here. The legacy stack is backdrop only.

## The AI feature

Three model tasks, all through app 17's `ModelProvider`. No code here calls Bedrock directly. The live model is Claude Sonnet 4.6 in us-east-2 through the Global Anthropic inference profile. The profile id is configuration (`P2P_BEDROCK_PROFILE_ID`, read in [`Config.from_env`](p2p/demo/config.py#L41)), and `--live` refuses to start without it ([`live_model_reference`](p2p/demo/config.py#L54)).

| Task | Input cap | Output | Where |
| --- | --- | --- | --- |
| `invoice-extract` | 20,000 characters ([`DOCUMENT_CAP`](p2p/extract/schema.py#L19)) | [`InvoiceDraft`](p2p/extract/schema.py#L69), with per-field confidence and `unreadable[]` | [`extract_draft`](p2p/extract/extract.py#L82) |
| `invoice-match` | the draft plus tool results | `MatchOutcome`, then a confidence band computed in code | [`match_invoice`](p2p/match/workflow.py#L120), [`decide`](p2p/match/decide.py#L91) |
| `dispute-triage` | rolling summary plus the last 10 case events, 12,000 characters | `DisputeStep`: one next action from a closed set | [`take_step`](p2p/disputes/triage.py#L118) |

How it is kept safe:

- **Output is validated before use.** Schemas are pydantic models. The extraction schema is sent as a tool the model must call ([`schema_tool_spec`](p2p/extract/extract.py#L66), forced with `tool_choice` at [line 94](p2p/extract/extract.py#L94)). A free-text answer counts as a failed attempt.
- **One repair, then a human.** A draft that fails validation is sent back once with the failures named. Two attempts is the cap ([`for attempt in (1, 2)`](p2p/extract/extract.py#L106)).
- **Document text is data.** Every prompt says so, for example [`p2p/extract/prompt.py`](p2p/extract/prompt.py#L51) and [`p2p/disputes/prompt.py`](p2p/disputes/prompt.py#L32).
- **No write acts on its own.** Every write tool stages a row and returns its `change_key`. Applying it needs a second principal ([`Erp.apply_change`](p2p/core/erp.py#L1679)), and the self-approval refusal is in [`assert_can`](p2p/core/access.py#L201).
- **Demo fallback.** `p2p demo` runs entirely on committed recordings in [`p2p/demo/fixtures/`](p2p/demo/fixtures/). It needs no network and no AWS credentials.

## Pattern map

| Pattern | Where | What it does here |
| --- | --- | --- |
| Adapter | [`ErpOperation`](p2p/core/adapter.py#L93), [`ErpTool`](p2p/core/adapter.py#L183) | One ERP operation, presented as an app-17 `Tool`. The gateway and the agent loop share one implementation and one `effect`. |
| Command | [`StagedErpChange`](p2p/core/adapter.py#L140) | A staged posting, credit or hold is an object whose `apply()` runs later, through the single apply path, by a different principal. |
| State | [`Phase`](p2p/disputes/state.py#L31) and its five subclasses, [`next_state`](p2p/disputes/state.py#L145) | The dispute case lifecycle: open, investigating, credit proposed, escalated, resolved. |
| Proxy | [`StagedWriteGate`](p2p/mcp/gate.py#L30) | App 17's `PermissionGate` in front of each write tool. It checks the denial, then lets the tool record a staged row. |
| Chain of Responsibility | [`escalation_chain`](p2p/disputes/escalate.py#L159) | A hold guard, then the five escalation criteria as app-17 interceptors. The first criterion that matches wins. |
| Template Method | [`TriageLoop`](p2p/disputes/triage.py#L86) | Subclasses app 17's `AgentLoop` and overrides only the hooks `before_model` and `should_stop`. |
| Strategy | [`replay_from`](p2p/demo/provider.py#L22) / [`live_provider`](p2p/demo/provider.py#L37) | The same story runs on a recorded `ModelProvider` or a live one. Nothing else changes. |

## Exam coverage

Twelve statements owned here. Each row points to the code that shows it.

| Statement | Where |
| --- | --- |
| 1.4 Enforced workflows and handoffs | [`match_invoice`](p2p/match/workflow.py#L120) (the four steps), [`Erp.apply_change`](p2p/core/erp.py#L1679) (the gate between staging and applying) |
| 1.6 Task decomposition (fixed) | [`match_invoice`](p2p/match/workflow.py#L120): the same four steps in code; the model decides only inside a step |
| 2.1 Tool interface design | [`ErpOperation`](p2p/core/adapter.py#L93); argument and field contract in [`p2p/mcp/contract.py`](p2p/mcp/contract.py#L54) |
| 2.3 Tool distribution and `tool_choice` | per-step manifests in [`manifest_for`](p2p/mcp/manifest.py#L76) and [`tool_choice_for`](p2p/mcp/manifest.py#L84); the forced schema tool in [`extract_draft`](p2p/extract/extract.py#L94) |
| 2.4 MCP server integration | [`p2p/mcp/server.py`](p2p/mcp/server.py#L56), the `p2p-erp` gateway app 19 connects to |
| 4.2 Few-shot prompting | three committed examples in [`p2p/extract/examples/`](p2p/extract/examples/), loaded by name in [`p2p/extract/prompt.py`](p2p/extract/prompt.py#L18) |
| 4.3 Structured output via tool use | [`schema_tool_spec`](p2p/extract/extract.py#L66) |
| 4.4 Validation, retry and feedback | [`extract_draft`](p2p/extract/extract.py#L106): one repair with the failures named, two attempts at most |
| 4.5 Batch processing | [`run_batch`](p2p/batch/run.py#L79): isolated failures, one summary, 25 documents at most ([`MAX_DOCUMENTS`](p2p/batch/run.py#L38)) |
| 5.1 Preserving context in long interactions | [`render_transcript`](p2p/disputes/context.py#L122) and [`roll_summary`](p2p/disputes/context.py#L190): a rolling summary plus the last 10 events ([`EVENT_WINDOW`](p2p/disputes/context.py#L34)) |
| 5.2 Escalation and ambiguity | the five criteria, verbatim, in [`Criterion`](p2p/disputes/schema.py#L35), applied by [`escalation_chain`](p2p/disputes/escalate.py#L159); one clarifying question via [`ask_question`](p2p/disputes/schema.py#L30) |
| 5.5 Human review and confidence | [`band_for`](p2p/match/decide.py#L56); `p2p review` and `p2p approve` in [`p2p/match/commands.py`](p2p/match/commands.py#L117); separation of duties in [`assert_can`](p2p/core/access.py#L201) |
| 1.5 (exercised; app 17 owns it) | interceptors on gateway calls, [`p2p/mcp/interceptors.py`](p2p/mcp/interceptors.py#L30) |
| 2.2 (exercised; app 17 owns it) | structured error categories in [`p2p/core/errors.py`](p2p/core/errors.py#L20) |

### Exam scenarios 1 and 6, checked against the code

- **Scenario 1, customer support agent (high-ambiguity requests with backend MCP tools, escalating to humans).** The disputes desk answers it. The closed action set ([`NextAction`](p2p/disputes/schema.py#L22)) and the single clarifying question are the ambiguity half. The five written criteria are the escalation half. A staged credit memo stands in for a refund, and `get_payment_status` for an account issue. **The exam says *customer* support and this desk serves *suppliers*. Everything the scenario exercises is present and only the counterparty differs, so the substitution stands. The exam did not ask about suppliers.**
  - **Gap found while checking:** the spec credits "the five written criteria and the confidence band" with the escalation half. The confidence band lives in the match flow ([`band_for`](p2p/match/decide.py#L56)), not in the disputes desk. The desk escalates on criteria only. That is recorded here rather than claimed.
- **Scenario 6, structured data extraction (validated JSON, no hallucinated or missing fields).** [`extract_draft`](p2p/extract/extract.py#L82) answers it: few-shot examples, the schema as a forced tool, and one repair with the failures named. Missing fields are reported through per-field confidence and [`unreadable[]`](p2p/extract/schema.py#L82). Ids in a model answer are re-checked against the ERP before use. The batch runs the same task at folder scale, which the scenario does not require.

## The gateway contract, for app 19

`p2p-erp` is an MCP server over stdio, protocol `2025-03-26` (also accepts `2024-11-05`). The constants are frozen in [`p2p/mcp/contract.py`](p2p/mcp/contract.py), and `tests/test_mcp_contract.py` asserts the running server against them.

| Tool | Effect | Roles |
| --- | --- | --- |
| `find_supplier`, `get_purchase_order`, `get_goods_receipt`, `get_invoice`, `list_open_invoices`, `get_payment_status` | read, company-code scoped, capped and paged | all, including `service` |
| `post_invoice` | write (stages a posting, returns `change_key` `pst_…`) | `clerk` |
| `issue_credit_memo`, `hold_payment` | write (stage a credit `crd_…` or a hold `hld_…`) | `disputes` |

- A failure is `{error_category, is_retryable, message}` and nothing else ([`ERROR_FIELDS`](p2p/mcp/contract.py#L80)). The five categories are app 17's: `transient`, `validation`, `permission`, `business`, `internal`. Only `transient` is retryable ([`TransientFailure`](p2p/core/errors.py#L51)).
- A staged write returns `{change_key, description}` ([`WRITE_FIELDS`](p2p/mcp/contract.py#L86)). A write tool never applies anything.
- A paged read returns `{count, items, limit, offset}`. `count` is the size of this page, not a total.

```bash
p2p-erp --database erp.sqlite3 --subject svc-app19 --role service --company-code 1000
```

## Architecture

```
invoice text ─> p2p ingest ─> InvoiceDraft (validated, repaired at most once)
                                  │
p2p match ─> extract ─> fetch PO ─> fetch goods receipt ─> decide (band in code)
                 │           │             │                    │
                 └───── MCP gateway session (Proxy + interceptors) ─┘
                                  │                     stage posting
                               Adapter                         │
                                  │                            v
                           simulated ERP (SQLite) <── p2p approve (second principal)

p2p dispute step ─> TriageLoop (Template Method) ─> closed action set
                         │                         escalation chain (5 criteria)
                         └─ State: open → investigating → credit_proposed → escalated → resolved
```

All model calls go through app 17's `ModelProvider`: replay by default, Bedrock with `--live`. The Lambda image serves only [`GET /health`](p2p/demo/health.py#L79), which reports the version and a replay round-trip and opens no ERP connection.

## Security

| Role | May |
| --- | --- |
| `clerk` | ingest, match, stage a posting, run a batch; never apply |
| `approver` | read its company code, apply or reject a staged change; never one it staged |
| `disputes` | read supplier, invoice and payment state, run a case, stage a credit or a hold, escalate |
| `service` | the six read tools, one company code |

Every accessor calls [`assert_can`](p2p/core/access.py#L168), and the company-code filter sits inside the ERP module, not in callers. A second apply of the same change is a stated no-op, enforced by the schema ([`erp/schema.sql`](erp/schema.sql#L139)). Integration tests per rule are in `tests/test_access.py` and `tests/test_erp_integration.py`. No AWS credential is committed.

## Deploy it yourself

TODO(deploy): fill in from 30-apps-infra#97 (18.9) once it merges: the `apps.yaml` entry, the Lambda image, and the IAM policy the deploy uses. The container image is built from the [`Dockerfile`](Dockerfile). After a deploy, `scripts/smoke.sh --url https://app18.breakthruventures.ai --version sha-<commit>` checks that the expected build answered.

## Develop

```bash
uv sync                      # Python 3.12; app 17 comes from its git tag
uv run ruff check . && uv run ruff format --check .
uv run mypy --strict
uv run pytest
scripts/smoke.sh             # the replay demo, asserted; no network
```

## 2-minute demo script

1. Run `uv run p2p demo`. It builds a private database in a temp directory and tells the whole story on recorded replies, ending in `demo ok (replay, no network)`. This proves the demo costs nothing and touches no real data.
2. In the `== ingest ==` block, `attempts 2` and `repaired true` show an extraction repaired on its second attempt. Open [`extract_draft`](p2p/extract/extract.py#L106). This proves validate-and-repair is capped at two attempts, with the failures named.
3. Find the `== variance ==` block: 50 received against 38 ordered, `band review`, `needs_human true`, "the variance exceeds 5%", and no `change_key`, so nothing was staged. Open [`band_for`](p2p/match/decide.py#L56) and [`decide`](p2p/match/decide.py#L91). This proves the band and the stop are computed in code, not by the model.
4. In `== review ==` and `== approve ==`, the change staged by `clerk@demo` is applied by the approver. Open [`assert_can`](p2p/core/access.py#L201). This proves the person who staged a change cannot apply it.
5. In `== batch ==`: 12 documents, 9 `ok`, 1 `failed` and 2 `skipped` (already advanced earlier in the story), in one summary. Open [`run_batch`](p2p/batch/run.py#L79). This proves one bad file does not stop the run.
6. In `== dispute ==` and `== escalation ==`: the desk reads the invoice, asks one clarifying question, reads the supplier, then proposes a credit, which is staged (`crd_000001`) and escalated because "the credit exceeds 2,500". Open [`escalation_chain`](p2p/disputes/escalate.py#L159). This proves escalation follows written criteria.
7. TODO(deploy): open `https://app18.breakthruventures.ai/health` and show the version and `replay: ok`.
