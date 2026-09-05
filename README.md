# Atlas — multi-agent orchestration for any business

Desktop app (customtkinter, no browser). An orchestrator agent ("Atlas") plans, delegates to
configurable specialist agents, reviews, and saves client-ready deliverables. Ships with a
consultancy setup; switch business model with one click (agency / saas / ecommerce / your own).

## Run

    Desktop\Atlas.bat            # or: cd atlas && py -m atlas
    py -m atlas run "Draft a proposal for ..." --mode auto            # headless
    py -m atlas run "..." --mode new_client_proposal                  # workflow

First run creates `config/*.json`. Set the Anthropic key in **Settings → Providers** (or export
`ANTHROPIC_API_KEY`). Local models: point the OpenAI-compatible provider at Ollama / LM Studio.

## Modes

- **auto** — Atlas decides: delegates (in parallel when independent), reviews, re-delegates, saves, finishes.
- **workflow** — fixed step pipeline from `config/workflows.json`; `{task}` `{previous}` `{all}` placeholders;
  optional Atlas synthesis at the end.

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
| `data/atlas.db` | run history (History page) |

Tools per agent: `delegate`, `list_agents`, `finish` (orchestrator), `save_deliverable`, `read_file`,
`list_files`, `web_fetch`.

## Atlas Desk — client portal + marketing site (web)

    py -m atlas.desk                     # http://localhost:8094/  (site)  ·  /desk (portal)
    DESK_MODE=demo|live|auto  DESK_TEMPLATE=sales_desk  DESK_PASSWORD=...  PORT=8094

Three ways to start a desk: **Let Atlas design it** (Design studio chat: it studies your links, proposes the team and
triggers, you approve), **Ready-made desk** (pick a template, short form), or **Start from scratch** (template `blank`:
just Atlas; add specialists on the **Team** page, cameras under Cameras, connectors under Integrations). Every desk
opens in the **simple view** (Home, Live desk, Leads, Approvals, Team, Cameras); **Show everything** in the sidebar
unlocks CRM, Integrations, Automations, Runs, Audit, Report, Desk setup and the Design studio (`ui_level` on the desk).

The **Team** page edits the roster in place: name, role, prompt, colour, active flag and tool grants per agent.
Saved rosters live in the desk's `config.agents` (`PATCH /api/desks/<id> {agents:[...]}`, `{reset_agents:true}` goes
back to the template). Specialists may only receive tools outside `ORCHESTRATOR_ONLY` plus the safe set in
`SPECIALIST_OK`; `finish` / `assemble_team` are never assignable; approvals still gate everything outbound.

The portal runs the same engine for one *desk* (business-model template): leads → Atlas plans and
delegates (research / writer / CRM in parallel) → `crm_update` → `queue_action` → **approval queue**
→ owner approves → sent (simulated; wire SMTP/WhatsApp in `api_decide`) → audit log → monthly report.

`DESK_MODE=demo` uses a scripted provider (`DemoProvider`) — no API key, same pipeline. Live mode
uses `config/providers.json` / `ANTHROPIC_API_KEY`.

Deploy: `render.yaml` (gunicorn, free plan, demo mode). Site files live in `site/`.

## Integrations (what an approved action actually does)

Every outbound action still goes through the approval queue. Connectors decide what happens when you click **Approve**. Add them under **Integrations** in the portal (or `POST /api/connectors`); each has a **Test** button. Secrets are stored server-side and returned masked.

| Channel / job | Connector kinds (preference order) | Notes |
|---|---|---|
| Email out | `smtp` → `resend` | SMTP for Gmail/Outlook app passwords; **Resend** (HTTPS) for hosts that block SMTP ports, e.g. Render. |
| WhatsApp out | `whatsapp` (Meta Cloud API) → `twilio` (needs `whatsapp_from`) | Outside the 24h customer-service window Meta requires an approved template. |
| SMS out | `twilio` | `from_number` must be a Twilio number you own. |
| Calendar booking | `gcal` | Agents call `calendar_free_slots` (read, immediate) and `calendar_book` (queued as a `booking` action unless the connector is *auto*). Attendee gets a Google invite. |
| CRM mirror | `hubspot`, `pipedrive` | Every `crm_update` and every send (stage → Contacted) is mirrored as a contact/person + note. Failures are logged on the run, never block the desk. |
| Owner pings | `slack` | Incoming-webhook message when something is waiting for approval and when it goes out. |
| Inbox in | `imap` | `inbox_watch` job turns unread mail into leads. |
| Anything else | `http`, `mcp`, `webhook` | Generic REST APIs, MCP tool servers, inbound web forms. |

Inbound URLs (shown on the Integrations page, per desk):

- `POST /hook/<token>` — web forms / Zapier / Make: JSON `{name, email, phone, company, notes, source}`.
- `GET|POST /hook/<token>/whatsapp` — Meta WhatsApp webhook. GET answers the verification handshake using the connector's `verify_token`; POST turns each text message into a lead + run, and Atlas is told to reply on WhatsApp.
- `POST /hook/<token>/sms` — Twilio inbound (SMS or WhatsApp sandbox). Returns empty TwiML; the reply is drafted and queued for approval.

Set-up crib sheet:

- **Resend**: verify your domain at resend.com → API key → `from_email` on that domain.
- **WhatsApp Cloud API**: Meta for Developers → app → WhatsApp → API setup → Phone number ID + permanent System User token. Webhooks → callback URL = the WhatsApp hook URL, verify token = whatever you typed in the connector, subscribe to `messages`.
- **Twilio**: Console → Account SID / Auth token; buy a number (`from_number`); for WhatsApp use the sandbox sender or your approved sender as `whatsapp_from`; set the number's messaging webhook to the SMS hook URL.
- **HubSpot**: Settings → Integrations → Private apps → scopes `crm.objects.contacts.read`, `crm.objects.contacts.write` → access token.
- **Pipedrive**: Personal preferences → API → token; `company_domain` is the subdomain.
- **Google Calendar**: Google Cloud → enable Calendar API → OAuth client (Desktop) → get a refresh token once (OAuth Playground, scope `https://www.googleapis.com/auth/calendar`) → `client_id`, `client_secret`, `refresh_token`; optional `calendar_id`, `timezone`, `day_start`, `day_end`.
- **Slack**: Slack app → Incoming Webhooks → add to channel → `webhook_url`.

All providers are exercised in `tests/test_integrations.py` against a mock HTTP transport (swap `atlas.integrations._TRANSPORT`), including the portal path approve → real send → CRM mirror → Slack ping and the inbound WhatsApp/SMS hooks.

## Running desk agents on Hermes Agent (Nous Research)

Any agent in a desk can run on a [Hermes Agent](https://hermes-agent.nousresearch.com) instance instead of the
built-in specialist loop. Atlas still orchestrates, applies policy and holds approvals; the Hermes-backed agent
brings its own tools (terminal, browser, web search, memory, skills, MCP servers).

1. Deploy an instance: Hostinger "Hermes Agent" one-click VPS, or on any Linux box
   `curl -fsSL https://hermes-agent.nousresearch.com/install.sh | bash`.
2. In `~/.hermes/.env` set `API_SERVER_ENABLED=true`, `API_SERVER_KEY=<secret>`, `API_SERVER_PORT=8642`,
   `API_SERVER_HOST=0.0.0.0` and a model key (e.g. `OPENROUTER_API_KEY`); pick the model with
   `hermes config set model.provider openrouter` / `hermes config set model.default <model>`; run `hermes gateway`.
   Production: put it behind HTTPS and use a sandboxed terminal backend (Docker), not `local`.
3. In the portal: Integrations -> add connector kind **Hermes Agent instance** (base_url, api_key) -> Test.
4. In the Design Studio inspector (or Desk setup) set an agent's **Engine** to *Hermes Agent instance*.
   Its long-term memory is scoped per desk + agent via `X-Hermes-Session-Key`.

Optional: expose the desk's own tools (CRM, approvals, connectors) to Hermes Agent as an MCP server in its
`~/.hermes/config.yaml` under `mcp_servers:` so it can queue actions through the same approval gate.

## Browser hand (a browser agent for sites with no API)

Any agent with the `browse` tool can operate a real Chromium: read JavaScript-heavy or logged-in pages, fill
forms, search portals, use client systems that have no API. `atlas/browser.py` snapshots the page into a numbered
list of elements plus text, the model acts by number (one action per step), and every action is a deterministic
Playwright call. Screenshots with the numbers drawn on are attached when the model asks (`look`) or gets stuck.

* **Submits are gated.** Anything that sends, pays, posts, books or deletes stops with `needs_approval`; the desk
  queues a `browser_action` for the owner and performs it on approval by replaying the recorded steps.
* **CAPTCHAs stop it.** It never tries to solve a human check.
* **Macros.** Successful runs can be recorded (`record`) and replayed without a model (`macro`), with `{{vars}}`;
  the model only takes over where a step breaks.
* **Budgets.** `BROWSER_MAX_STEPS` (40), `BROWSER_MAX_INPUT_TOKENS` (250k), `BROWSER_MODEL`
  (default `anthropic/claude-sonnet-4.5`), optional allowed-domain list.

```
pip install playwright && playwright install chromium --with-deps
py -m atlas.browser login https://web.whatsapp.com --profile desk1     # log in once; the profile keeps the session
py -m atlas.browser run "Find the price of X" --url https://example.com --record find_x
py -m atlas.browser replay find_x --var q=drain
```
Profiles live in `data/browser/profiles/<name>`; a desk uses `desk<id>`.
