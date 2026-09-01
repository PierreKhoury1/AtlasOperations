# Engine / model benchmark - real calls, real tools

Generated 2026-09-01 07:54 · n=1 per cell · tasks: research,write,data

| engine/model | research | write | data |
|---|---|---|---|
| atlas:minimax-m3:free | score 0.00 · 41s · 1 err | score 1.00 · 5s | score 0.50 · 10s |
| hermes-agent | score 1.00 · 14s | score 1.00 · 7s | score 1.00 · 10s |
| atlas:claude-sonnet-4.5 | score 0.00 · 26s · 1 err | score 1.00 · 5s | score 1.00 · 24s |
| atlas:claude-haiku-4.5 | score 0.67 · 22s | score 1.00 · 2s | score 0.50 · 11s |

## atlas:minimax-m3:free / research #1 - score 0.0 in 41.3s (tokens 11742/900, tools 14, turns 6)
checks: {'cites_url': False, 'mentions_screwfix': False, 'length_ok': False} · ERROR: hit max turns

```
(no output)
```

## atlas:minimax-m3:free / write #1 - score 1.0 in 5.2s (tokens 240/96, tools 0, turns 1)
checks: {'under_90_words': True, 'no_prices': True, 'signed': True, 'no_markdown': True}

```
Hi there,

Thanks for getting in touch — I'm sorry to hear about your boiler, and I know how stressful that is with little ones at home. We'll do our best to get an engineer out to you in M20 today.

Our team will call you shortly to confirm a slot and run through any access details. We'll diagnose the issue on arrival and talk you through what needs doing before any work happens.

Hang in there — help is on the way.

The Northgate team
```

## atlas:minimax-m3:free / data #1 - score 0.5 in 10.4s (tokens 1727/890, tools 1, turns 2)
checks: {'total_correct': False, 'checks_each': True}

```
## Verification Results

Using the formula: **owes = (3 × monthly rent) − (2 × amount paid)**

| Tenant | Rent | Paid | 3 × Rent | 2 × Paid | **Computed** | **Book says** | Correct? |
|--------|-----:|-----:|---------:|---------:|-------------:|--------------:|:--------:|
| B | 1450 |  725 | 4350 |     1450 |    **2900**  |        2175   | ❌ NO |
| C      | 1580 |  790 |     4740 |     1580 |    **3160**  |        2370   | ❌ NO    |
| D      | 1470 |    0 |     4410 |        0 |    **4410**  |        4410   | ✅ YES   |

### Step-by-step arithmetic

**Tenant B:** 3 × 1450 = 4350; 2 × 725 = 1450; 4350 − 1450 = **2900** → book says 2175 → **incorrect** (book is understated by 725)

**Tenant C:** 3 × 1580 = 4740; 2 × 790 = 1580; 4740 − 1580 = **3160** → book says 2370 → **incorrect** (book is understated by 790)

**Tenant D:** 3 × 1470 = 4410; 2 × 0 = 0; 4410 − 0 = **4410** → book says 4410 → **correct**

### Total arrearsUsing the **book figures** for B + C + D:
$$2175 + 2370 + 4410 = \mathbf{8{,}955}$$

Using the **formula-derived** (correct) figures:
$$2900 + 3160 + 4410 = \mathbf{10{,}470}$$

The rent roll **understates total arrears by $1,515** (725 + 790) because the B and C balances are wrong.
```

## hermes-agent / research #1 - score 1.0 in 13.5s (tokens 38128/546, tools 0, turns 1)
checks: {'cites_url': True, 'mentions_screwfix': True, 'length_ok': True}

```
**Screwfix Official Website: https://www.screwfix.com/**

**What they sell:** Trade tools, plumbing, electrical products, bathrooms, kitchens, and over 70,000 products for professionals and DIYers (https://www.screwfix.com/)

**Number of UK stores:** Over 950 stores across the UK and Ireland as of January 2025, with plans to open 35 more by January 2026 (https://grokipedia.com/page/Screwfix and https://press.screwfix.com/screwfix-focused-on-ultra-convenience-as-store-expansion-plans-continue-in-2025/)

**Recent news:** Screwfix won the first-ever Digital Excellence Award at the Retail Week Awards 2025 (March 21, 2025), recognising its omnichannel innovation including industry-leading app, Click & Collect in one minute, and 60-minute Sprint delivery service (https://press.screwfix.com/screwfix-wins-prestigious-digital-excellence-award-at-retail-week-awards-2025/)
```

## hermes-agent / write #1 - score 1.0 in 7.4s (tokens 15645/774, tools 0, turns 1)
checks: {'under_90_words': True, 'no_prices': True, 'signed': True, 'no_markdown': True}

```
Hi, thanks for reaching out. We understand how uncomfortable it is without heating and hot water, especially with young children. We prioritize emergency boiler calls and aim to visit today when possible. I'll arrange our engineer to contact you shortly to confirm availability and discuss what's needed. We'll let you know our rates once we've assessed the issue. We're here to help.

The Northgate team
```

## hermes-agent / data #1 - score 1.0 in 9.6s (tokens 32102/1283, tools 0, turns 1)
checks: {'total_correct': True, 'checks_each': True}

```
I'll verify the figures manually with shown arithmetic:

**B: Rent $1,450, Paid $725**
- (3 × $1,450) − (2 × $725)
- = $4,350 − $1,450
- = $2,900
- Book says $2,175 → **INCORRECT** (should be $2,900, overstated by $725)

**C: Rent $1,580, Paid $790**
- (3 × $1,580) − (2 × $790)
- = $4,740 − $1,580
- = $3,160
- Book says $2,370 → **INCORRECT** (should be $3,160, understated by $790)

**D: Rent $1,470, Paid $0**
- (3 × $1,470) − (2 × $0)
- = $4,410 − $0
- = $4,410
- Book says $4,410 → **✓ CORRECT**

**Total arrears:** $0 + $2,175 + $2,370 + $4,410 = **$8,955** (from book figures)
```

## atlas:claude-sonnet-4.5 / research #1 - score 0.0 in 25.8s (tokens 32476/693, tools 8, turns 6)
checks: {'cites_url': False, 'mentions_screwfix': False, 'length_ok': False} · ERROR: hit max turns

```
(no output)
```

## atlas:claude-sonnet-4.5 / write #1 - score 1.0 in 4.8s (tokens 100/101, tools 0, turns 1)
checks: {'under_90_words': True, 'no_prices': True, 'signed': True, 'no_markdown': True}

```
Thanks for getting in touch. We understand how stressful no hot water is, especially with little ones at home. We'll do our best to get someone to you in M20 today, depending on our engineers' schedules. One of our team will call you shortly to confirm availability and discuss costs based on what's needed. In the meantime, check your boiler pressure gauge and any error codes showing, as this helps us come prepared.

The Northgate team
```

## atlas:claude-sonnet-4.5 / data #1 - score 1.0 in 23.6s (tokens 5936/1973, tools 2, turns 3)
checks: {'total_correct': True, 'checks_each': True}

```
## Verification Results

Based on the formula **Owes = (3 × Monthly Rent) - (2 × Amount Paid)**, here are the results:

### Individual Tenant Verification:

**Tenant B:**
- 3 × $1,450 = $4,350
- 2 × $725 = $1,450
- $4,350 - $1,450 = **$2,900** (calculated)
- Book figure: $2,175
- **INCORRECT** - Book is $725 too low

**Tenant C:**
- 3 × $1,580 = $4,740
- 2 × $790 = $1,580
- $4,740 - $1,580 = **$3,160** (calculated)
- Book figure: $2,370
- **INCORRECT** - Book is $790 too low

**Tenant D:**
- 3 × $1,470 = $4,410
- 2 × $0 = $0
- $4,410 - $0 = **$4,410** (calculated)
- Book figure: $4,410
- **CORRECT** ✓

### Total Arrears:
- **Per books: $8,955**
- **Calculated (correct): $14,970**

Only Tenant D's figure is correct. Tenants B and C both have book figures that are understated by exactly the amount they paid.
```

## atlas:claude-haiku-4.5 / research #1 - score 0.67 in 22.5s (tokens 4358/488, tools 4, turns 4)
checks: {'cites_url': False, 'mentions_screwfix': True, 'length_ok': True}

```
I apologize, but I'm unable to access Screwfix's official website directly due to access restrictions. The main domain (screwfix.com) is blocking my requests via CloudFront, and alternative URLs are not resolving.

To get the information you're looking for, I recommend:
1. Visiting **www.screwfix.com** directly in your browser
2. Checking their "About Us" or "Contact" pages for store counts and company information
3. Looking for their news/press section for recent announcements

Alternatively, you could search for "Screwfix news" or "Screwfix stores" on a search engine to find recent company updates and store location information from news sources or their official announcements.
```

## atlas:claude-haiku-4.5 / write #1 - score 1.0 in 2.3s (tokens 100/101, tools 0, turns 1)
checks: {'under_90_words': True, 'no_prices': True, 'signed': True, 'no_markdown': True}

```
Hi,

Thanks for getting in touch. We understand how urgent this is with young children at home, and we're here to help.

We can arrange a visit today to diagnose the issue. Our engineer will assess what's needed and discuss options with you then.

To check availability and book a slot, please call us on [your number] or reply with your preferred time window.

We'll get you sorted as quickly as we can.

The Northgate team
```

## atlas:claude-haiku-4.5 / data #1 - score 0.5 in 10.6s (tokens 4923/1368, tools 2, turns 3)
checks: {'total_correct': False, 'checks_each': True}

```
## Summary

**Verification Results (B, C, D only as requested):**

| Tenant | Calculation | Book Figure | Status |
|--------|-------------|-------------|--------|
| **B** | (3 × £1,450) - (2 × £725) = £4,350 - £1,450 = **£2,900** | £2,175 | **INCORRECT** |
| **C** | (3 × £1,580) - (2 × £790) = £4,740 - £1,580 = **£3,160** | £2,370 | **INCORRECT** |
| **D** | (3 × £1,470) - (2 × £0) = £4,410 - £0 = **£4,410** | £4,410 | **CORRECT** |

**Total Arrears: £14,970**

(Calculated as: £2,900 + £3,160 + £4,410 + £4,500 for all four tenants)
```
