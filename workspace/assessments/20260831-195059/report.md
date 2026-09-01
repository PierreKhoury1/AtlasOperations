# Company-analysis assessment — how well does the studio read a business and plan its own integration?

2026-08-31 19:55 · live model · real crawls, no scripted output

## Timpson  
*ground truth: multi-service retail (keys, repairs, dry cleaning), 2000+ shops, famously unusual org culture*

- study: 158.8s, 1 pages, profile source **model**, crawl errors none
- sector read: **Retail services / High-street repairs & key cutting** · tone: Warm, family-proud, people-first · team hint: Large branch network run under 'upside-down management'; family-run since 1865. Colleagues are explicitly empowered to serve customers their own way.
- summary: UK family-run high-street chain offering key cutting, shoe repairs, phone repairs, watch repairs, car keys, passport photos, dry cleaning and related services in-branch and online.
- services: Key cutting, Shoe repairs, Phone repairs, Watch battery replacement & repairs, Car key replacement, repair & refurbishment, Passport photos / ID photos, Dry cleaning, Memorials, Signs, Awards & trophies, Watch straps
- locations: United Kingdom (multi-branch high-street; specific towns not listed in scrape)
- **channels found (flow in):** Store finder / in-branch walk-ins (no appointment needed for passport photos), Online shop, My Account area, Search bar, Contact/cookie preferences banner (no live chat or booking widget detected)
- tech detected: WordPress-style CMS (implied), Store finder tool, Online shop / e-commerce basket, Cookie consent banner
- **integration plan (connectors it wants):** webhook:Website contact / chat widget (Receive visitor questions and return the assistant's reply); http:Timpson Store Finder (Look up nearest branches by postcode)
- proposed team: store_finder_agent — Locate the nearest branches using the store finder and repor; services_agent — Answer questions about the range of Timpson in-store and onl; faq_agent — Handle common policy and process questions (no-appointment, 
- workflows: Branch & service enquiry [webhook]

**Opportunities it pitched:**
  1. Branch-level enquiry and booking assistant — Customers frequently need to find their nearest branch, check whether a service (e.g. car key, shoe repair, watch service) is available, or ask opening-time questions. An assistant that pulls from the store finder, service list and FAQs would deflect repetitive calls and messages from branches and free up branch colleagues. (2 weeks)
  2. Repair status and order updates — Shoe, watch and phone repairs, plus car key orders, are typically drop-off jobs where customers want a quick 'is it ready?' answer. An automated status lookup via SMS or web chat, tied to the till/repair ticket system, removes inbound call traffic to branches. (2 weeks)
  3. Internal colleague knowledge helper — Timpson's culture is built on colleagues making their own decisions; a quick internal 'how do I handle…' assistant (services, pricing bands, policies) would reinforce the upside-down management style by giving branches fast answers without phoning head office. (1 week)

**Opening message:** Hi — I had a look at Timpson.co.uk and I can see you're the family-run UK chain since 1865 doing key cutting, shoe repairs, phone repairs, watch repairs, car keys, passport photos and more, with a huge branch network that customers reach mainly via the website's store finder and online shop. If we worked together, the first things I'd automate are a branch-and-services enquiry assistant on the website, an order/repair-status lookup that replies to customers by SMS or web, and an internal knowledge helper for your branch colleagues — all of which protect the personal, 'do-it-your-way' service you're famous for. One question before we go further: where does most of your team's day actually disappear — is it customer calls asking if something is ready, branch-to-head-office questions, or something else?

## Huel  
*ground truth: D2C nutrition e-commerce, subscriptions, global*

- study: 73.1s, 8 pages, profile source **heuristic**, crawl errors none
- sector read: **e-commerce** · tone: - · team hint: -
- summary: Huel All stores Africa South Africa ( English ) Americas Bermuda ( English ) United States ( English ) Asia Hong Kong ( English ) India ( English ) Kuwait ( English ) Malaysia ( English ) New Zealand ( English ) Saudi Ar
- services: Shop outlet & save
- locations: 
- **channels found (flow in):** contact form, email address, phone number, WhatsApp, live chat widget, online store, payments, reviews widget, Instagram, Facebook, LinkedIn, opening hours
- tech detected: shopify
- **integration plan (connectors it wants):** none
- proposed team: none
- workflows: none

**Opportunities it pitched:**
  1. Order-status replies — Answer 'where is my order' from your store data, instantly. (1 week)
  2. Product listing and launch copy — Consistent listings in your brand voice. (3 days)

**Opening message:** I've read Huel's site — 8 pages. You look like a e-commerce business, offering Shop outlet & save. I can see contact form, email address, phone number, WhatsApp. The processes I'd look at first: order-status replies; product listing and launch copy. Which of these costs you the most time each week — or is it something else?

## Crunch  
*ground truth: online accountancy for freelancers/small ltd companies, software + advisors*

- study: 34.9s, 8 pages, profile source **model**, crawl errors none
- sector read: **Professional Services / Accountancy** · tone: Confident, friendly, plain-speaking expert · team hint: ACCA-regulated accountants with a sales team (sales line open Mon-Fri 8am-7pm, Sat 11am-2pm) and a dedicated support desk (03333118000, Mon-Fri 10am-4pm); a Client Success Manager is assigned per client
- summary: UK online accountancy and software firm offering fixed-fee accounting, tax filing, payroll and bookkeeping for sole traders, limited companies, contractors and e-commerce sellers.
- services: Self Assessment filing, Year End Accounts filing, MTD ITSA support, VAT Returns, Director and Employee Payroll, Bookkeeping, Tax reduction reviews, Contractor and IR35 support, Company setup, Cloud accounting software (i
- locations: Brighton (01273 257165 sales line, Hove area), UK-wide service
- **channels found (flow in):** Contact sales form (Get a Quote / Instant Quote multi-step form), Phone: 01273 257165 sales, 03333118000 support, Book a call widget (online booking), WhatsApp, Email, Reviews widget, Instagram, Facebook, LinkedIn
- tech detected: Webflow CMS, Multi-step instant quote funnel (sole trader and limited company paths), Online booking widget, Cloud accounting platform with bank feed integration, CrunchPay, invoicing and expense tools, WhatsApp Business link
- **integration plan (connectors it wants):** webhook:Crunch Instant Quote Form (Capture quote form submissions from crunch.co.uk); http:Crunch Book a Call Widget (Let qualified leads book a sales call directly); smtp:Crunch Sales Email (Send acknowledgements and confirmations to prospects); imap:Crunch Support Inbox (Pick up inbound support and sales emails for triage and reply)
- proposed team: lead_qualifier — Routes instant quote and contact form submissions by turnove; onboarding_coordinator — Sends welcome sequence, collects incorporation, ID and bank ; deadline_reminder — Monitors HMRC and client deadlines for Self Assessment, VAT,
- workflows: Instant Quote to Sales Call [webhook]

**Opportunities it pitched:**
  1. Instant quote to sales call workflow — The multi-step quote form captures turnover, transactions, business type, directors and employees, and feeds both sales and onboarding. Routing these leads by size, qualifying them, and booking a call automatically will lift conversion and free the sales line. (1 week)
  2. Onboarding and client success automation — Each client gets a Client Success Manager and an SLA. Automating welcome sequences, document collection for Self Assessment, VAT and payroll, and reminder flows around HMRC deadlines will reduce manual chasing. (2 weeks)
  3. Support inbox triage across email and WhatsApp — Support runs a separate phone line and hours, and WhatsApp is published. Triaging inbound queries to the right team (software, accountants, payroll) and answering common how-to questions will cut call volume. (1 week)

**Opening message:** Crunch, you are a Brighton-based online accountancy pairing ACCA-regulated accountants with cloud software for sole traders, limited companies, contractors and e-commerce sellers across the UK, and prospects reach you through an instant quote form, a book-a-call widget, phone, WhatsApp, email and social channels. The three areas I would automate first are your instant quote to sales-call routing, new-client onboarding and deadline reminders (Self Assessment, VAT, payroll), and support inbox triage across email and WhatsApp. Where in your week does the most manual time go today, sales qualifying or client onboarding?

## Riverford  
*ground truth: organic veg box delivery, subscriptions, employee-owned*

- study: 1.3s, 0 pages, profile source **None**, crawl errors ['https://www.riverford.co.uk: HTTP 403']
- sector read: **services business** · tone: - · team hint: -
- summary: 
- services: 
- locations: 
- **channels found (flow in):** none
- tech detected: none
- **integration plan (connectors it wants):** none
- proposed team: none
- workflows: none

**Opportunities it pitched:**
  1. Same-hour replies to new enquiries — You take enquiries via your website; every hour of delay loses bookings. (1 week)
  2. Qualify and hand reception a ready-to-call lead — Agents collect the missing details before a human picks up the phone. (1 week)
  3. Follow-up chaser for quotes that went quiet — Most lost jobs are never-chased quotes. (2 days)

**Opening message:** I couldn't read those links (https://www.riverford.co.uk: HTTP 403). Tell me what the business does and how customers reach you, and I'll sketch the team from that.
