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
