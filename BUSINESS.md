# Atlas Ops — what the business is, how it makes money, how we find clients

*Written 1 September 2026. Everything in "What exists today" is running code, not a plan.*

## 1. The business in one paragraph

Atlas Ops is an **AI-operations consultancy**. A small business tells us which process eats its week — answering enquiries, writing proposals, triaging an inbox, chasing quotes — and we **install and operate a "desk"**: a team of AI agents, run by an orchestrator called Atlas, that does that work every day. Nothing customer-facing leaves without the owner's approval. We charge a setup fee to design and install the desk and a monthly fee to run, monitor and improve it. We sell an outcome ("your enquiries are answered within the hour, qualified, and in your CRM"), not software.

The customer is an owner-led business with 5–250 staff — estate and lettings agents, clinics and dental groups, trades, agencies, consultancies, accountancies, e-commerce brands. They will never write a prompt or manage an API key. That is the whole point.

## 2. What exists today (the product)

| Layer | What it does | Status |
|---|---|---|
| **Design Studio** | Owner pastes business links → the designer crawls the site, profiles the company (services, channels, tech, structure), proposes what to automate first, and sketches the agent team live on a canvas → *Approve & build* creates the desk | live, real model |
| **Desk engine (Atlas)** | Orchestrator plans, delegates to specialists, reviews, updates CRM, queues outbound actions | live; Atlas on Claude Sonnet 4.5 (Balanced tier) |
| **Hermes Agent engine** | Specialists can run on a real Nous Research Hermes Agent instance — own browser, terminal, web search, persistent memory, skills. Benchmarked 3/3 vs the built-in loop's 1/3 | live, local instance; per-client VPS deployable (Hostinger one-click) |
| **Model landscape** | Per-agent engine + model choice (free MiniMax → Haiku → Sonnet → Hermes-4) through one OpenRouter key | live |
| **Guardrails** | Policy layer (no prices, no invented phone numbers, no placeholders, length, banned phrases), approval queue, full audit log, watchdog, health panel | live |
| **Integrations** | SMTP/IMAP, WhatsApp (Meta / Twilio), SMS, HTTP APIs, MCP tool servers, HubSpot/Pipedrive, Google Calendar, Slack, webhooks | built; each client supplies credentials |
| **Client portal** | Dashboard, live desk (token-by-token), leads, approvals, CRM, automations, monthly report, system health | live |
| **Proof** | Soak test (24/7 simulated company), eval + benchmark harnesses, 46 automated tests, case scenarios on real companies (Pimlico Plumbers, mydentist, We Are Social, Timpson, Crunch) | live |

What is *not* done: Postgres for the hosted portal (SQLite resets on deploy), sandboxing of code execution, a real client's credentials wired in end to end.

## 3. Why anyone would pay us rather than use an AI tool

Zapier agents, Lindy, Relevance, CrewAI and the rest give a technical person the same model calls we use. Our customer is not that person. What they pay for:

1. **Accountability by construction** — approval gate on every outbound action, a deterministic policy layer, a complete audit trail. It is hard for our AI to do something the owner didn't sanction. For a dentist or a plumber this is the buying question.
2. **Design, not configuration** — the studio maps *their* process into a team in ten minutes; we operate it. A managed service with a monthly review, not a SaaS login abandoned in week three.
3. **Evidence** — per-lead cost, response times, failure rate, monthly report. We can put numbers next to "it works".
4. **Any model, any engine** — free models for volume, Sonnet where judgement matters, Hermes Agent where tools matter; swapped without rebuilding anything. Clients are never locked to one vendor's price list.

The software is a well-guarded implementation of a known pattern. **The business is the moat:** scoping → blueprint → install → operate → report, with a person accountable for the outcome.

## 4. Revenue strategy

### Pricing (per desk)
| Plan | Setup | Monthly | Includes |
|---|---|---|---|
| **Starter** | £2,500 | £1,200 | one desk, one workflow, up to 300 handled items/month, monthly report |
| **Growth** | £5,000 | £2,800 | up to three workflows, integrations (CRM, WhatsApp, calendar), weekly review, Hermes Agent engine |
| **Scale** | custom | custom | multi-desk, dedicated instance, data-residency, SLA |

Model usage is passed through at cost with a monthly cap the client sets (typical: £5–60/month per desk). Three-month minimum. Setup fee is non-refundable and covers the scoping call, design, install and first-week tuning.

### Unit economics
- Delivery cost per Starter desk ≈ 6–10 hours setup + 1–2 hours/month operations + £5–40 model cost. Gross margin on the monthly fee is above 80% once the desk is stable.
- Ten Starter clients = £12,000 MRR + £25,000 setup revenue. That is a real business for one person plus tooling.
- The monthly fee is the asset; setup fees fund acquisition.

### Sequence
1. **Pilot phase (now → first 3 clients):** 30-day paid pilots at the Starter setup fee only (£2,500, monthly waived), one function each, with a written before/after report. Goal: three case studies with numbers, three testimonials, three references.
2. **Standardise (clients 4–10):** pick the two desk types the pilots proved (most likely *inbound enquiry desk* for trades/clinics and *brief-to-proposal desk* for agencies/consultancies) and sell only those. Templates already exist; every install gets faster.
3. **Expand inside accounts:** a client with a working enquiry desk is the easiest buyer of a follow-up desk (quotes chaser, reviews, onboarding). Second desks at Starter pricing with no discovery cost.
4. **Productise the engine (month 6+):** self-serve tier for technical SMEs at £300–500/month — the same portal, no operations, no promises. Only after the managed tier is proven; it must not distract from it.

### Revenue guardrails
- Never sell a desk we haven't run in the soak/eval harness for that workflow type.
- Never quote model costs without the cap; never absorb usage silently.
- One function per client at a time. Scope creep is where margin dies.

## 5. Finding clients

### Who first
Businesses where **response time visibly converts to money** and enquiries arrive through a web form or inbox we can hook:
- **Trades** (plumbers, electricians, heating engineers) — emergency jobs go to whoever answers first.
- **Dental / aesthetics / physio clinics** — new-patient enquiries, nervous patients, NHS/private questions.
- **Independent estate & lettings agents** — valuation requests and applicant enquiries.
- **Small agencies & consultancies** — inbound briefs and proposals (our own category; we understand it).

Signals the studio already detects from a website: a contact form, a published WhatsApp number, no live chat, no booking widget, reviews mentioning slow replies. Those are the prospects.

### How
1. **Let the product prospect.** A "Growth desk" for Atlas Ops itself: agents research target businesses (Hermes Agent browses), score fit from the same signals the studio uses, and draft a personalised first email — every email waits in the approval queue for Pierre. Same machine we sell; first real workload. (Started 1 Sep 2026.)
2. **Show, don't pitch.** The email offers one thing: *"Send me a link, I'll send back a one-page blueprint of what we'd automate first — free, no call."* The studio produces that page in under a minute. Reply rate on a concrete artefact beats any cold pitch.
3. **Scoping call → paid pilot.** 30 minutes, share the blueprint, agree one workflow and the success number (e.g. "every web enquiry answered inside an hour, 7 days a week"). Invoice the pilot setup fee before install.
4. **Channels in order of expected yield:** warm network and existing contacts (BOOKLI outreach lists in Jerusalem/Ramallah are a second market); LinkedIn direct to owners with the blueprint offer; local business groups and trade bodies (Checkatrade-listed firms, BDA/dental groups, ARLA agents); partnerships with bookkeepers and web agencies who already serve these SMEs and can refer.
5. **Numbers to expect:** 100 well-targeted emails → 8–15 blueprint requests → 4–6 calls → 1–2 pilots. Ten pilots need roughly 600–800 emails or a handful of referrals. Track it in the CRM on the Growth desk.

### What to say (the whole pitch)
> We install and operate an AI team for one job in your business — for most people that's answering and qualifying enquiries within the hour, every day. You approve every message before it goes out; nothing touches money or customers without you. Setup takes a week. Send me your website and I'll send back what we'd automate first.

## 6. The next 30 days
- [ ] Growth desk running: 100 prospects researched, 100 drafts approved and sent, replies tracked
- [ ] Three pilots signed at £2,500 each
- [ ] Postgres on the hosted portal; client accounts on; Render live with the paid key
- [ ] One Hermes Agent instance per pilot client (Hostinger VPS, £7/mo each)
- [ ] Before/after report template produced from the monthly report page
