# Engine / model benchmark - real calls, real tools

Generated 2026-08-31 19:54 · n=1 per cell · tasks: research,write,data

| engine/model | research | write | data |
|---|---|---|---|
| atlas:minimax-m3:free | score 0.00 · 55s · 1 err | score 1.00 · 4s | score 0.00 · 69s · 1 err |
| hermes-agent | score 1.00 · 14s | score 1.00 · 6s | score 1.00 · 83s |

## atlas:minimax-m3:free / research #1 - score 0.0 in 55.4s (tokens 7597/402, tools 9, turns 6)
checks: {'cites_url': False, 'mentions_screwfix': False, 'length_ok': False} · ERROR: hit max turns

```
(no output)
```

## atlas:minimax-m3:free / write #1 - score 1.0 in 4.2s (tokens 240/105, tools 0, turns 1)
checks: {'under_90_words': True, 'no_prices': True, 'signed': True, 'no_markdown': True}

```
Hi there, I'm really sorry to hear about your boiler — no hot water with two little ones at home is stressful, and we'll do our best to help today.

A Gas Safe engineer can prioritise urgent no-hot-water calls, and we cover your M20 area. I can't share pricing here, but once we know more on site you'll get a clear quote before any work begins.

I just need a quick callback number and a good time to reach you so we can get an engineer scheduled straight away.

The Northgate team
```

## atlas:minimax-m3:free / data #1 - score 0.0 in 68.6s (tokens 0/0, tools 0, turns 0)
checks: {'total_correct': False, 'checks_each': False} · ERROR: KeyError: 'choices'

```
(no output)
```

## hermes-agent / research #1 - score 1.0 in 13.8s (tokens 39229/504, tools 0, turns 1)
checks: {'cites_url': True, 'mentions_screwfix': True, 'length_ok': True}

```
Screwfix — official website: https://www.screwfix.com

What they sell: Trade tools, hardware, plumbing fittings, electricals, workwear and DIY supplies — over 35,000 products available for Click & Collect or delivery (https://www.screwfix.com).

UK store count: Over 900 stores across the UK, up from 790 in 2021/22. Screwfix announced plans to open up to 35 more by end of January 2026 (https://press.screwfix.com/screwfix-focused-on-ultra-convenience-as-store-expansion-plans-continue-in-2025/).

Recent news: On2 December 2025, the Screwfix Foundation announced a new charity partnership with Hospice UK to fund improvement grants helping hospices maintain their end-of-life care facilities (https://press.screwfix.com/the-screwfix-foundation-partners-with-hospice-uk-to-improve-end-of-life-care-facilities/).
```

## hermes-agent / write #1 - score 1.0 in 6.1s (tokens 14536/1029, tools 0, turns 1)
checks: {'under_90_words': True, 'no_prices': True, 'signed': True, 'no_markdown': True}

```
Hello, thanks for getting in touch — I'm really sorry your boiler's gone off, especially with two little ones at home.

For an M20 call-out today we can prioritise this as an emergency. A qualified engineer will be in touch shortly to confirm the earliest arrival window and run through any access details. We'll diagnose the fault first, explain clearly what's needed, and only proceed once you're happy with the plan.

Warm regards,
The Northgate team
```

## hermes-agent / data #1 - score 1.0 in 83.4s (tokens 29745/2602, tools 0, turns 1)
checks: {'total_correct': True, 'checks_each': True}

```
Results (no markdown — plain text as the rendering layer expects):

Row-by-row check using owes = 3 × rent − 2 × paid:

A — rent 1500, paid not given, book0. No verification performed (not asked for A).
B — 3 × 1450 − 2 × 725 = 4350 − 1450 = 2900. Book says 2175. INCORRECT (book is short by 725).
C — 3 × 1580 − 2 × 790 = 4740 − 1580 = 3160. Book says 2370. INCORRECT (book is short by 790).
D — 3 × 1470 − 2 × 0 = 4410. Book says 4410. CORRECT.

Total arrears per the book figures: 0 + 2175 + 2370 + 4410 = 8955.
Total arrears per the 3R − 2P formula: 2900 + 3160 + 4410 = 10470.
The book is understated by 1515 in total — exactly equal to B's missing725 plus C's missing 790.
```
