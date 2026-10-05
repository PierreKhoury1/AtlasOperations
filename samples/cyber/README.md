# Cyber desk sample recordings

REPLAY bundles for the cyber page (`atlas/desk/static/cyber.html`), built from **real parser output** by
`scripts/cyber_sample_bundle.py`. The script runs the real portal in-process (scratch data folder, `DESK_MODE=demo`,
`CYBER_OFFLINE=1`), uploads the public-log excerpts in `tests/fixtures/cyber` through `POST /api/cyber/upload` in
original time order, and stores each changed route response as a frame, the same way the page's record mode does.

| File | Data | Shows |
|---|---|---|
| `maccdc-sample.json` | Zeek `notice.log` + `ssh.log` and Snort fast-alert excerpts, MACCDC 2012 | Zeek scans and password guessing, the Zeek-heuristic "likely succeeded" login, Meterpreter C2 detections, two sensor lanes |
| `authlog-sample.json` | `auth.log` excerpt: four sources, one 409-username list | the `e3503990` wordlist campaign, public IPs masked (`mask_public_ips`), times without a year (the log has none) |

Data: Security Repo by Mike Sconzo (https://www.secrepo.com), licensed CC BY 4.0
(https://creativecommons.org/licenses/by/4.0/). MACCDC 2012 captures: National CyberWatch Mid-Atlantic CCDC (via
SecRepo). Each bundle sets `query.credit = "secrepo"`, so the page shows that attribution in its footer.

What is not real-time: frame times `t` follow a fixed schedule rather than a wall clock. The uploads ran with
`trigger=0`, so no agent run started: the incident card is empty and no containment is proposed. These files are for
layout checks and REPLAY captures of the page itself, not footage of a run. Film footage comes from `?record=<name>` on
a live desk. The config frame's hook URL has its token replaced by `<token>`.

Open one without a portal: load `cyber.html?replay=__test` from disk and call `window.__loadBundle(<the JSON>)`, then
`window.__render(t)` for any `t` up to `window.__duration`. With a portal: `POST /api/cyber/recordings` with
`{"name": "maccdc-sample", "bundle": <the JSON>}`, then open `/desk/cyber?replay=maccdc-sample` (add `&play=1` to watch
it play in real time).

Regenerate: `py scripts/cyber_sample_bundle.py maccdc` and `py scripts/cyber_sample_bundle.py authlog --mask --steps 8`.
