# Hermes — multi-agent orchestration for any business

Desktop app (customtkinter, no browser). An orchestrator agent ("Hermes") plans, delegates to
configurable specialist agents, reviews, and saves client-ready deliverables. Ships with a
consultancy setup; switch business model with one click (agency / saas / ecommerce / your own).

## Run

    Desktop\Hermes.bat            # or: cd hermes && py -m hermes
    py -m hermes run "Draft a proposal for ..." --mode auto            # headless
    py -m hermes run "..." --mode new_client_proposal                  # workflow

First run creates `config/*.json`. Set the Anthropic key in **Settings → Providers** (or export
`ANTHROPIC_API_KEY`). Local models: point the OpenAI-compatible provider at Ollama / LM Studio.

## Modes

- **auto** — Hermes decides: delegates (in parallel when independent), reviews, re-delegates, saves, finishes.
- **workflow** — fixed step pipeline from `config/workflows.json`; `{task}` `{previous}` `{all}` placeholders;
  optional Hermes synthesis at the end.

## Everything is config

| File | What |
|---|---|
| `config/business.json` | name, model, services, tone, pricing, extra context — injected into every agent |
| `config/agents.json` | roster: id, role, provider, model, tools, system prompt, colour |
| `config/workflows.json` | step pipelines |
| `config/providers.json` | Anthropic (effort / adaptive thinking / fallbacks) + OpenAI-compatible endpoint. **API key stored plaintext here** |
| `config/orchestration.json` | iteration + delegation-depth limits |
| `config/ui.json` | theme, accent, fonts, radius, window size, sidebar side/width/compact, panel visibility + order, labels, log colours |
| `templates/*.json` | business-model templates (Business page → Save as template) |
| `workspace/inputs/` | drop files here; agents read via `read_file` |
| `workspace/runs/<id>/` | per-run deliverables, TASK.md, SUMMARY.md |
| `data/hermes.db` | run history (History page) |

Tools per agent: `delegate`, `list_agents`, `finish` (orchestrator), `save_deliverable`, `read_file`,
`list_files`, `web_fetch`.

## Hermes Desk — client portal + marketing site (web)

    py -m hermes.desk                     # http://localhost:8094/  (site)  ·  /desk (portal)
    DESK_MODE=demo|live|auto  DESK_TEMPLATE=sales_desk  DESK_PASSWORD=...  PORT=8094

The portal runs the same engine for one *desk* (business-model template): leads → Hermes plans and
delegates (research / writer / CRM in parallel) → `crm_update` → `queue_action` → **approval queue**
→ owner approves → sent (simulated; wire SMTP/WhatsApp in `api_decide`) → audit log → monthly report.

`DESK_MODE=demo` uses a scripted provider (`DemoProvider`) — no API key, same pipeline. Live mode
uses `config/providers.json` / `ANTHROPIC_API_KEY`.

Deploy: `render.yaml` (gunicorn, free plan, demo mode). Site files live in `site/`.
