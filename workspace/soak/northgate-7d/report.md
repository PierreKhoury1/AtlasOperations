# Soak report - Northgate Plumbing (in progress)

Started 2026-08-31 13:06 · elapsed 24.39 h of 168.0 h · leads/day 40.0 · fault rate 0.05 · server restarts 1 · RSS 60.4 MB

## Traffic
- leads posted: normal 32, duplicate 3, malformed 0 (rejected 0, accepted 0)
- hook errors (non-2xx on valid leads): 26
- owner decisions: approved 27, rejected 5

## Runs (last 24h window at snapshot)
- total 34 · by status {'done': 34} · failure rate 0.0%
- p50 3.9s · p90 5.4s · live now 0 · stalled 0 · zombies 0 (max seen 0)
- tokens 131,431 (≈ £0.51 on Sonnet)

## Queue & automations
- pending approvals 0 · oldest 0 h

## Alerts seen (count of snapshots in which each alert was raised)
- provider: 38

## Detection
- failures detected by the health check: 38; mean time-to-detect 1446s (bounded by the 10.0-min snapshot interval)

Last error: None

Snapshots: workspace\soak\northgate-7d\snapshots.jsonl