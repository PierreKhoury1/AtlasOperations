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

## Run it in 5 minutes (portal + cameras, from a clean clone)

    pip install -r requirements.txt -r requirements-vision.txt   # torch is ~2 GB; CPU is fine for 5 cameras
    # Debian/Ubuntu: the system `cryptography` and `blinker` packages break pip / Flask threads - take the PyPI ones
    pip install --ignore-installed blinker "cryptography>=43"
    # ffmpeg must be on PATH: video-file cameras and one test need it (apt install ffmpeg / winget install ffmpeg)
    OPENROUTER_API_KEY=sk-or-...  DESK_OPEN=1  DESK_PROVIDER=openrouter  py -m atlas.desk
    # -> http://localhost:8094/desk  ->  Cameras  ->  add a camera with source  sample:kitchen-overview  (or any .mp4 / rtsp://)
    #    journal on, Watch, then Live: detections on the picture, the vision model writing beside it

YOLO weights (`yolo11n.pt`, `yolo11s.pt`) download to `data/models/` on first use. Sample footage is in `samples/videos/`
(`sample:<name>` sources; `python scripts/get_demo_videos.py` fetches the larger originals). `DESK_OPEN=1` skips
accounts - local only. Eyes: the free default `nvidia/nemotron-3-nano-omni:free` works with no card; set
`VISION_MODEL=google/gemini-3.1-flash-lite` (≈ $0.001 per note through OpenRouter) for the best honesty per dollar we
have measured, see [Cameras](#cameras-agents-that-see-the-room).

Environment knobs you will meet: `LIVE_FPS` (live loop cap per camera, 15), `VISION_YOLO` (detector weights),
`VISION_RUNTIME` (live detector: auto / openvino / torch), `ATLAS_SAMPLE_VIDEOS` (folder of sample clips), `PORT`. Tests: `python -m pytest -q tests/test_vision.py
tests/test_journal.py tests/test_live.py` (one process per file: the auth rate limiter is process-wide and trips when
several API suites share one run).

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

## Cameras (agents that see the room)

Add a camera on the **Cameras** page (webcam index, `rtsp://...`, or an `http://.../capture` snapshot URL). Each camera
carries one rule; **Watch** polls it (5 s minimum), runs YOLO locally, and wakes the desk when the rule fires. The run
gets the counts, the analyst's answer to the standing question and the snapshot; whatever it decides to send waits
in the approval queue.

| Field | Meaning |
|---|---|
| `watch_for` | labels to count (`person`, `car`, `person, dog`) |
| `min_count` | fire when at least this many are in frame |
| `hours` | only inside this window, e.g. `23:00-06:00` (blank = always) |
| `dwell_min` | **0** = fire as soon as the count is reached; **2** = only once they have been there 2 minutes ("3+ guests waiting over 2 min"). One missed detection does not reset the timer. |
| `cooldown_min` | minimum gap between alerts |
| `repeat` | after the cooldown: `changes` (default) only if the count changed or the scene moved, so a parked car never pages the owner twice; `always` every cooldown while present; `once` only when the count first crosses `min_count` |
| `question` | what the vision model answers on every alert ("Is anyone at the door?") |
| `task` | what the desk should do when it fires |
| `alerts` | `0` = document-only camera: the rule is logged but never wakes the agents |


**Live (watch it happen).** Press **Live** on a camera card: the picture plays as real video (`/api/cameras/<id>/live.mjpg`,
motion JPEG, one decode loop per source shared by every viewer, webcam / RTSP / a recording that plays at real speed
and loops) with every frame through the local detector, boxes and track ids drawn, fps and detector time in the
corner. Next to it the journal is written in real time: `/api/vision/journal/stream` (server-sent events) carries
every scheduler tick and each note as `note_start`, one `note_delta` per token, `note_done`, plus summaries. While
anyone is watching, the vision model is streamed; with no viewer the journal runs exactly as before. The journal and
the rules read the live loop's frame while it runs, so what is written is what was on screen. `LIVE_MAX_W` (1920),
`LIVE_JPEG_Q` (76), `LIVE_FPS` (15, webcam/RTSP cap) and `LIVE_IDLE_S` (45, stop after the last viewer leaves) tune it.

**Live detector runtime.** `VISION_RUNTIME` = `auto` (default) | `openvino` | `torch`. On `auto` the live detector
exports its weights once to OpenVINO at a fixed 384x640 input (`data/models/yolo11n_384x640_openvino_model/`, a few
seconds on first use) and runs every camera on it with one inference thread each (`VISION_OV_THREADS`); ByteTrack ids,
labels and box coordinates are unchanged. Without `openvino` installed, or if the export fails, it logs once and runs
on torch. The camera status shows which one runs (`yolo11n (openvino)`). Measured on WILDTRACK on a 4-core Xeon
(`python scripts/bench_detector.py live-speed` / `live-accuracy`): the live `.track` call went from 83 to 23 CPU-ms per
frame with 4 cameras and from 89 to 24 with 7 (33 -> 140 and 35 -> 135 frames a second in total), with the same person
detection on 100 held-out instants x 7 cameras (precision / recall / F1 0.785 / 0.495 / 0.607 on torch, 0.783 / 0.496 /
0.607 on OpenVINO). The gain relies partly on this CPU's bf16 (AMX); older x86 CPUs gain less. The record detector
(object catalogue, audits; yolo11s on 2x2 tiles plus the whole frame) now drops every tile box that touches an
interior tile edge and merges at NMS 0.6, conf 0.25 (`VISION_PRECISE_CONF`, `VISION_TILES=1x1` turns tiles off): F1
0.635 -> 0.699 on WILDTRACK (C1 C2 C3 C6, full-res, 20 held-out instants; `bench_detector.py precise`).

**Journal (document everything).** With `journal` on, the camera keeps a detailed written record of what it sees - the
content "ask the cameras" searches. The vision model writes a note from two frames (the one at the previous note and
now) plus the previous note, so each note says what changed: who arrived or left, what they wear and do, how long
someone has been waiting, doors, deliveries, anything out of place.

| Field | Default | Meaning |
|---|---|---|
| `journal` | off (on in the portal form) | write notes for this camera |
| `journal_min_gap_s` | 8 | fastest note rate while the scene keeps changing |
| `journal_every_s` | 60 | a note at least this often even when nothing changes |
| `journal_motion` | 0.03 | how much movement counts as a change |
| `journal_rollup_min` | 15 | condense the notes into one summary (timeline, peak counts, open questions) this often |
| `journal_focus` | | what to pay special attention to ("tables occupied or cleared, staff at the pass") |

Notes and summaries are vision events (`source` journal / digest), embedded and cited like everything else, and are
also appended to a readable diary per desk per day (`data/journal/desk<id>/<date>.md`, `GET /api/vision/journal`,
"today's journal" on the Cameras page). A video file (`.mp4` etc.) as the source plays as a live, looping camera, for
demos and for testing on recorded footage. Cost: each note is one vision call with two images. On the free OpenRouter
model that is $0 but the free daily request limit covers roughly an hour of two busy cameras; for all-day use set a
paid `vlm_model` on the camera or add `GEMINI_API_KEY` / `GROQ_API_KEY` (each adds its own free quota).

**Alerts go to a person.** On the Cameras page, *Alerts go to* names a channel (WhatsApp / SMS / email / Slack) and a
recipient (`PATCH /api/desks/<id> {notify: {...}}`). Every rule firing then becomes one message in the approval queue
with the reason, the counts, who is known in view, the analyst's answer, the journal note and a snapshot link; **send
at once** skips the approval. Per camera, `per_hour` (6) firings an hour are sent one by one and the rest of that hour
becomes one digest; inside `quiet` hours (`23:00-07:00`) firings are logged, not sent. `run: true` also wakes the agents
for every alert (the behaviour when no channel is set). With a channel set, a `daily_report` job queues the report by
name every morning at `report_time` (`GET /api/report/day?format=md` is the same report on demand).

**Honesty (what makes a note trustworthy).** A wrong detail in the journal is worse than a missing one, because the
owner searches it later. Three things guard against it, all on by default:

| Guard | What it does | Off switch |
|---|---|---|
| close-up | the largest detected person is cut out of the frame and sent as a third image, so hands and what they hold are not read from a few pixels ("smartphone" that was a receipt) | `journal_closeup=0` |
| confidence language | the note says "appears to" / "unclear" for what it cannot confirm; the rollup keeps those out of the headline and peak numbers ("Unconfirmed:" at the end) | prompt |
| verify pass | a second call gets the draft + the current frame(s) and must delete every claim the frame does not show and soften the plausible ones; the stream shows `note_verify` with what was removed. If its reply cannot be parsed the draft stands | `journal_verify=0` |

Measured with `py -m atlas vision-eval --sites kitchen [--verify]` (5 kitchen cameras × 3 frames, human ground truth,
judge `claude-sonnet-5`; `workspace/vision-eval/kitchen-verify-ab/report.md`):

| eyes | hallucinations / answer | judge accuracy 0-5 | hazard recall | cost / note |
|---|---|---|---|---|
| `gemini-3.1-flash-lite` | 1.53 | 3.2 | 100% | $0.0011 |
| `gemini-3.1-flash-lite` **+ verify** | **0.87** | **3.6** | 100% | $0.0019 |
| `nemotron-3-nano-omni:free` | 1.13 | 3.13 | 67% | $0 |
| `nemotron-3-nano-omni:free` **+ verify** | **0.87** | 3.33 | **100%** | $0 |

The verify pass removes 23-43% of invented details and raises accuracy on both, for 1.7× the vision calls. What it
strips is telling: "likely preparing food", "wearing a headset", "potential cross-contamination risk" - the guesses a
model adds to sound complete. Model choice matters less than the audit: every model in the wider 11-model eval
(`workspace/vision-eval/final-20260927`) invents 1-3 details per answer unaudited, the expensive ones included.

Agents get `camera_look` (fresh frame now), `camera_events` (what the rule logged) and `camera_ask` (retrieval over the
event log, hybrid text + CLIP image search, the vision model re-looks at the best frames). The team architect grants
them to any agent whose job is to watch feeds, even before the first camera is added.

For every-frame tracking (queues, dwell times, entered/left per object) run the **Vision Node** (`node/`) next to the
cameras and point it at the desk's sensor hook URL; the desk then reacts to tracked events instead of polling.

## Multi-camera accuracy (many people, cameras in sync)

One camera and one person proves little. `py -m atlas mcam-eval` scores the whole vision stack against human labels on
**WILDTRACK** (EPFL, CVPR 2018): 7 synchronised, calibrated cameras over one square, 13-40 people in the scene at
once (mean 23.8), 313 identities, every person boxed in every camera at 400 instants (2 a second, 200 s).

    pip install remotezip httpx pillow motmetrics
    python scripts/get_wildtrack.py        # ~310 MB kept (540p frames + labels + calibration), streamed out of the 6.8 GB archive
    py -m atlas mcam-eval [--vlm google/gemini-3.1-flash-lite]   # -> workspace/vision-eval/wildtrack/report.md, ~25 min on 4 CPU cores

WILDTRACK is licensed for non-commercial research: the frames and the per-box files derived from them stay out of git.
Results (`workspace/vision-eval/wildtrack/report.md`, CPU, 960x540):

| what | measured | what it means |
|---|---|---|
| person detection, `yolo11n` | precision 0.696, recall 0.55 (IoU 0.3: 0.759 / 0.6) | about half the labelled people in a crowd are found on each camera |
| person detection, `yolo11s` | precision 0.714, recall 0.531 | 2x the compute buys nothing on this footage |
| tracking, live view (ByteTrack) | IDF1 0.357, MOTA 0.212, 2633 identity switches | at 2 frames a second, IDs in a crowd do not survive |
| tracking, catalogue tracker | IDF1 0.428, MOTA 0.278, 1256 switches | half the switches of ByteTrack at this frame rate |
| tracking, ground tracker (all cameras) | IDF1 0.538, MOTA 0.308, 1482 switches; held out: **IDF1 0.574** vs 0.470 catalogue, 0.451 ByteTrack | one tracker for every camera, on the ground: identities last longer, at the cost of more switches |
| one id per person on every camera | IDF1 across cameras 0.523; held out **0.560** vs 0.447 for catalogue tracks linked by foot position afterwards | the ground tracker gives a person the same id on every camera itself |
| people on the ground plane (ground tracker, 50 cm) | MODA 0.501, IDF1 0.645; held out MODA 0.589, IDF1 0.712 | where people stand and who they are, from the cameras together |
| same person across cameras, by appearance (CLIP) | rank-1 11% (chance 1%), AUC 0.503 | appearance cannot tell people apart across viewpoints |
| same person across cameras, by where the feet land | **rank-1 ≥ 99.9%** (160,054 queries) | with calibrated cameras, geometry solves it |
| foot point vs the labelled ground position | median 8.9 cm, p90 19.8 cm | the calibration is sound |

How many people are there? (mean absolute error against the labelled count per instant)

| estimate | error (people) | bias | within 20% |
|---|---|---|---|
| add up every camera's detections | 58.2 | +58.2 | 0% |
| the busiest single camera | 7.3 | -7.1 | 30% |
| detections' feet merged on the ground within 50 cm | 22.7 | +22.7 | 0% |
| detections' feet merged on the ground within 75 cm | 16.8 | +16.8 | 0% |
| detections' feet merged on the ground within 100 cm | 14.4 | +14.4 | 1% |
| detections' feet merged on the ground within 150 cm | 11.4 | +11.3 | 7% |
| **multi-camera vote**: merged within 150 cm, kept when 3 cameras agree or one detection is >= 0.7 sure, inside the area | **2.7** | -0.2 | 83% |
| `gemini-3.1-flash-lite`, all 7 frames at once | 75.9 | +75.9 | 0% (8 instants) |
| `nemotron-3-nano-omni-30b-a3b-reasoning:free`, all 7 frames at once | 82.3 | +82.3 | 0% (6 instants) |

**The ground tracker** (`atlas/ground.py`) treats the calibrated cameras as one sensor: every detection's foot point goes
to the ground, sightings within 120 cm (never two from one camera) become one person, and people are followed on the
ground (constant-velocity Kalman, Hungarian assignment within 120 cm, 3 missed instants allowed, a new person only when
two cameras see them, confirmed after 3 instants and then labelled back to their first). Every sighting of a person
gets the same id on every camera. **The vote** counts a merged person only when 3 cameras agree or one detection is
sure, and only inside the area. Both were tuned on instants 0-199; the "held out" numbers are instants 200-399 alone
(trackers started fresh there), and the full-run numbers include the tuning half. Honest limits: the ground tracker
makes more identity switches than the catalogue tracker (802 vs 620 held out) while holding people longer; the vote's
0.7 confidence bar is sensitive (0.6 or 0.8 cost about a person of error), and a plain 800 cm merge counted inside the
area came within 0.4 people of it.

What that says, plainly: finding people and matching them across calibrated cameras work; identities through a crowd
at 2 frames a second are better with all cameras tracked together but still far from solved (IDF1 under 0.6). Adding
the cameras up double-counts. Merging sightings whose feet land in the same spot over-counts: a half-hidden person's box
stops above the feet, so the foot point lands somewhere else and becomes an extra person, and every false detection
adds one. Asking that several cameras agree removes most of those, which is why the vote errs by under 3 people where
the busiest single camera, low by whoever it cannot see, errs by 7. A vision model shown all seven frames answers with round
guesses (85, 115, 120) that do not follow the crowd; it also sees people outside the labelled area, a harder test, but
not one that explains the gap. Naming people by appearance (the Objects page) works for a few regulars in one room; in a
crowd CLIP would put the wrong name on someone, so there a name needs position or a second cue. The next gains are a
detector trained on crowds, more frames a second for tracking, and full-body boxes (or head points) for the ground
plane.

**Review workspace (`/desk/review`).** The run above, played back: all seven cameras on one playhead (space, arrows,
drag the timeline), human labels solid, the ground tracker's boxes dashed (one id per person on every camera), a
labelled person we missed shaded red. Click anyone to light them up on every camera that sees them, with their crops and
how many ids the tracker split them into (ideal: one); the timeline plots the true head count against every estimate,
the multi-camera vote in green. The Scores tab adds identity across cameras, the ground plane and the held-out half. `ATLAS_REVIEW_DATA` / `ATLAS_REVIEW_OUT` point it at another dataset or run.

**Are the camera agents' notes true?** `py -m atlas mcam-notes` runs the real camera agent (journal note with close-up
and verify pass, as a live camera writes it) on C1, C2, C3 and C6 at the same 6 moments, 4 s apart, in the busiest
stretch. A stronger judge model (`claude-sonnet-5`) then checks every claim against the camera's own frame and against the
other three cameras at that instant, which see the same people from other sides; the count is checked against the labels.
With `gemini-3.1-flash-lite` as the agent (`workspace/vision-eval/wildtrack/notes-report.md`, about $0.37 a run):

| claims checked | true in own frame | invented or wrong | unclear | wrong per note | other cameras agree | removed by the agent's verify pass |
|---|---|---|---|---|---|---|
| 197 (24 notes) | 81% | 5% | 14% | 0.4 | 99% of the 100 they could see | 52 |

The mistakes are wrong specifics (an olive jacket that is dark, a hood that is not there, a "group of five" that is not
a group), "no vehicles" when a truck stands at the back, and a person reported gone who is still in view. Only 3 of 24
notes state a total head count, so the notes are not a counter: counts come from the detector and the geometry above.
The judge is a model too, a second opinion rather than ground truth; rerunning only the checks (`--rejudge`) moved the
wrong-claim rate between 4% and 5%. The Review page's **Agents** tab shows every note at its instant with each claim
marked true, wrong or unclear, and the timeline marks where notes were written.

**Recordings on one clock.** A video-file camera plays at the position `(now - VIDEO_SYNC_EPOCH) mod duration`, so any
number of recordings of the same moment stay in step: across viewers, restarts and the live loop (checked in
`tests/test_live.py`: two feeds opened 0.6 s apart stay within 0.35 s). Recordings of different lengths each loop on
their own length.

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
