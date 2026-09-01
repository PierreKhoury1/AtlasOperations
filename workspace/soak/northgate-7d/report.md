# Soak report - Northgate Plumbing (in progress)

Started 2026-08-31 13:06 · elapsed 16.88 h of 168.0 h · leads/day 40.0 · fault rate 0.05 · server restarts 0 · RSS 32.4 MB

## Traffic
- leads posted: normal 31, duplicate 3, malformed 0 (rejected 0, accepted 0)
- hook errors (non-2xx on valid leads): 0
- owner decisions: approved 26, rejected 4

## Runs (last 24h window at snapshot)
- total 34 · by status {'done': 34} · failure rate 0.0%
- p50 3.9s · p90 5.4s · live now 0 · stalled 0 · zombies 0 (max seen 0)
- tokens 131,440 (≈ £0.51 on Sonnet)

## Queue & automations
- pending approvals None · oldest 0.0 h

## Alerts seen (count of snapshots in which each alert was raised)
- provider: 36

## Detection
- failures detected by the health check: 36; mean time-to-detect 1476s (bounded by the 10.0-min snapshot interval)

Last error: {'run_id': '20260901-055901-dc9207', 'text': 'RuntimeError: HTTP 500: injected fault - upstream error (soak)', 'ts': 1788220744.558534}

Snapshots: workspace\soak\northgate-7d\snapshots.jsonl