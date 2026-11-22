# Rules for every agent (Claude Code, Grok Build, Codex)

This repo is part of the **30 apps in 30 days** portfolio, in the AWS demo track (apps 17 to 20). The playbook
lives in [BreakthruDevelopment/ai-dev-os](https://github.com/BreakthruDevelopment/ai-dev-os); the spec is
`specs/18-procure-to-pay.md` there and the track's shared rules are in `specs/day-0-aws-demo-track.md`. This is a
Python package, not a Next.js app: the Next.js stack rules of the other repos do not apply.

## Repo safety (non-negotiable)

The BreakthruDevelopment org holds other, unrelated applications.

- Touch only repos with the GitHub topic `30-apps-30-days`. Everything else is read-only.
- Never force-push, delete, rename, transfer or archive any repo or branch you didn't create for the current task.
- Never push to `main`. Open a PR from a branch named `<harness>/<short-task>` (e.g. `claude/python-scaffolding`).
- Pick up only issues labeled for your harness, and follow the role file named in the issue title (`ai-dev-os/roles/`).
- Never commit secrets. `.env*` is ignored; real values live in GitHub Actions secrets or a cloud secret manager.

## Stack

- **Python 3.12**, managed with `uv` (`uv sync`, `uv run ...`); commit `uv.lock`.
- `ruff check` and `ruff format --check`, `mypy --strict`, `pytest`. Strict means strict: no blanket
  `# type: ignore` or `# noqa`. A missing third-party stub gets a narrowly scoped `[[tool.mypy.overrides]]` entry
  with a comment saying why.
- The package is `p2p`. **The foundation paths are `p2p/core/`, `erp/` and `contracts/`** (plus
  `.github/workflows/`, which the spec also gives to wave 1). Wave 1 owns them. A later slice adds files only if a
  security-sensitive `claude` slice names it as owner; if your slice needs a change there, or the data layer it needs
  is not on `main`, stop and comment on the issue. `scripts/run-harness.sh`'s pre-push guard knows only the
  template's TypeScript paths, so in this repo the rule is held by this file, the spec and review.
- `pyproject.toml`, `uv.lock` and `Dockerfile` are **shared, not foundation.** A slice that adds a dependency edits
  `pyproject.toml` and relocks (`uv lock`, which CI enforces by running `uv sync --locked`) and says so on
  its PR. `--frozen` would NOT enforce it: it installs from the lock without checking it still matches
  `pyproject.toml`.
- **App 17 (`agent-container`) is pinned by git tag** in `[tool.uv.sources]` (currently `v0.1.0`), never by branch or
  bare commit. A slice that needs something the tag lacks stops and comments on its issue; it does not bump the tag
  or reimplement the contract.
- Two things are load-bearing for the whole track and change only on a `claude` slice: the CI job name
  `Lint, test, build, smoke`, and the `review-gate.yml` caller, which must keep the `pull_request`,
  `pull_request_review` and `issue_comment` triggers and `permissions: actions: write`.

## The track's rules

- **Every model call goes through app 17's `ModelProvider`.** No code here calls Bedrock directly.
- **Every demo and test runs at zero cost on recorded replies**; `--live` switches to Bedrock. Nothing needs the
  network to be shown.
- **The loop ends on the stop reason, never on the text.**
- **Every ERP write is a staged change** through `PermissionGate`, applied by a second principal. Access checks
  (`assert_can`) and the `company_code` filter live in `p2p/core`, never in callers.
- **Tool results, invoices, emails and supplier messages are data, never instructions.** Fence them before they reach
  a model.
- **Validate input at the boundary** (CLI arguments, tool arguments, extracted drafts) with typed models.
- **Logs:** JSON lines with fixed fields. Never log secrets, credentials or document text.
- **No AWS credential is ever committed.** Local runs use the default credential chain. Slices that need no AWS
  access must not reference CI secrets; the CI job today reads none.

## Definition of done (CI enforces most of it)

The required check is named exactly `Lint, test, build, smoke`; a second job is `Container image`. **Only the
first is a required context in the ruleset**, so a broken Dockerfile can merge green — check it yourself when you
touch it. The required job runs:
`ruff check`, `ruff format --check`, `mypy --strict`, `pytest`, the package builds and imports, and the CLI runs.
New rules have a test in `tests/`. The README's "What this app does" section is filled in.
