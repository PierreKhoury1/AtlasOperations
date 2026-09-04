# Atlas Desks — business one-pager

**What it is.** An AI-operations consultancy. Clients bring a business and a process; we install and *operate* an AI desk (a team of specialised agents + approval queue) for one function, then improve it monthly. Not "AI runs your company" — one function, done properly, human approval on anything sensitive.

## Offer

| Desk | Job | First target |
|---|---|---|
| AI Sales Desk | lead sourcing → research → personalised outreach → CRM → follow-ups | estate agents, B2B services |
| Support & Booking Desk | triage → answer from docs → book/reschedule → remind → escalate | clinics, salons, trades |
| Content & Outreach Desk | calendar → drafts → email/promo → report | e-commerce, agencies |
| Research & Documents Desk | brief → research → strategy → pricing → proposal → QA | consultancies, professional services |

Lead offer to open with: **AI Sales Desk for estate agents** — easiest to demo, price and trust ("finds leads, researches, drafts outreach, updates CRM, you approve before send").

## Pricing (ex VAT)

| Plan | Monthly | Setup | Scope |
|---|---|---|---|
| Starter | £1,200 | £2,500 | 1 desk, ≤3 tools, £150 usage included |
| Growth | £2,800 | £5,000 | 2–3 desks, ≤8 tools, fortnightly ops call, £400 usage included |
| Scale | custom | custom | multi-site/regulated, dedicated ops lead, SLA/DPA, private deploy |

3-month minimum then rolling; usage passed through at cost above the included cap.
Unit economics target: ~£150–400 model spend per desk/month → 70–85% gross margin on subscription after ops time (~4–6 h/desk/month at steady state).

## Delivery model (repeatable)

1. Scoping call (30 min) → one-page desk proposal within 48 h.
2. Week 1: map process, connect accounts, configure agents on real templates/tone, set approval rules.
3. Weeks 2–4: supervised launch — everything through approval queue; tune on client edits.
4. Month 2+: routine work autonomous, sensitive still approved; monthly report (volume, approval rate, time saved, changes) + roadmap.

## Stack (hybrid, per the architecture decision)

- Agent runtime: OpenAI Agents SDK (tools, handoffs, guardrails, tracing) — or the in-house **Atlas** orchestrator (`C:\Users\pierr\atlas`) for internal delivery of the Research & Documents Desk.
- Durable workflows: LangGraph / Temporal when steps need checkpoints and retries.
- Integrations: n8n (email, CRM, calendar, forms, WhatsApp, Slack).
- System of record: Postgres — tasks, permissions, approvals, audit.
- Human approval gate before pay / sign / publish / delete / sensitive messaging.

## Defensibility

Not the framework. Industry-specific workflows, integrations, accumulated operating data, and human QC.

## First 90 days

- Wk 1–2: finish site, pick 1 industry (estate agents), build the Sales Desk demo on Atlas/n8n with fake data, record a 3-min walkthrough.
- Wk 3–6: 3 pilot clients at Starter (discount setup to £1,000 for a case study). Reuse BOOKLI outreach batch machinery for cold email.
- Wk 7–12: case study + pricing validation; second desk (Support & Booking for clinics); systemise onboarding checklist.

## Before launch — replace placeholders

- Legal entity, address, company number in `privacy.html`
- Real inbox for `BRAND.email` in `script.js` (currently `hello@atlasdesks.com`, domain not registered)
- Decide final brand name (change `BRAND.name` + `<title>`/OG tags)
- Host: Render static site (same flow as BOOKLI) or Netlify — no backend needed
