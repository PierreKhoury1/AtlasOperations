# Multi-camera restaurant eval: vision models on real feeds

Run: full-20260927. Sites: crownshy, kitchen. Judge: `anthropic/claude-sonnet-5`. Frames per camera: 3. Single-frame calls per model: 36.

Scores: count_exact = % of frames where the people count fell inside the human range; count_near = within one of it; hazard_recall = % of human-flagged issues the model mentioned; area_ok = judge says the station was identified; judge = 0-5 accuracy vs the human description; halluc = invented details per answer (judge-counted); issues/frame = how many issues the model raised (over-flagging shows here). multicam = one call with every camera of a site. temporal = three frames of one camera over time (kitchen site).

| model | fail | count exact | count near | hazard recall | area ok | judge | halluc | issues/frame | multicam judge | service ok | busiest ok | temporal judge | temporal counts | p50 s | max s | cost $ |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free | 4/43 | 97% | 100% | 64% | 97% | 3.39 | 1.12 | 0.42 | 3.5 | 2/2 | 2/2 | 2.75 | 12/12 | 2.0 | 59.1 | 0 |
| qwen/qwen3.8-27b:free | 41/43 | 0% | 0% | None% | 100% | 3 | 1 | 0 | None | 0/2 | 0/2 | 3 | 3/3 | 22.8 | 34.0 | 0 |
| google/gemma-4-31b-it:free | 43/43 | None% | None% | None% | None% | None | None | None | None | 0/2 | 0/2 | None | - | None | None | 0 |
| z-ai/glm-5.3-flash | 5/43 | 94% | 97% | 69% | 100% | 3.26 | 2.42 | 1.77 | 3 | 1/2 | 2/2 | 2.8 | 15/15 | 12.5 | 55.2 | 0.0326 |
| google/gemini-3.1-flash-lite | 0/43 | 92% | 97% | 62% | 100% | 3.58 | 1.14 | 0.47 | 2.5 | 2/2 | 2/2 | 2.4 | 15/15 | 1.8 | 5.1 | 0.0246 |
| qwen/qwen3.6-flash | 33/43 | 100% | 100% | 86% | 100% | 3.44 | 2 | 1.33 | None | 0/2 | 0/2 | 2 | 3/3 | 29.5 | 40.2 | 0.0691 |
| google/gemini-3.8-flash | 4/43 | 81% | 97% | 67% | 100% | 3.16 | 2.09 | 1.25 | 3 | 2/2 | 2/2 | 2.6 | 15/15 | 6.3 | 16.0 | 0.1565 |
| anthropic/claude-haiku-4.5 | 0/43 | 92% | 94% | 50% | 89% | 3.17 | 1.78 | 1.14 | 3 | 1/2 | 2/2 | 2.4 | 15/15 | 3.3 | 9.0 | 0.1045 |
| openai/gpt-5.4-mini | 0/43 | 72% | 89% | 69% | 97% | 3.22 | 1.78 | 1.47 | 3 | 1/2 | 2/2 | 3 | 15/15 | 2.3 | 5.3 | 0.0932 |
| anthropic/claude-sonnet-5 | 0/43 | 81% | 92% | 75% | 97% | 3.14 | 2.75 | 2.53 | 3 | 1/2 | 2/2 | 3 | 15/15 | 4.7 | 10.5 | 0.3266 |
| google/gemini-3.1-pro-preview | 18/43 | 87% | 100% | 88% | 91% | 3.13 | 2.09 | 1.3 | None | 0/2 | 0/2 | 2.5 | 6/6 | 11.3 | 17.0 | 0.7295 |

Judge cost: $0.9444. Total model cost: $1.5366.

## Judge accuracy per camera (mean 0-5)

| model | crownshy-dining | crownshy-grill | crownshy-line | crownshy-plancha | crownshy-plating | crownshy-prep | crownshy-salamander | kitchen-overview | kitchen-prep | kitchen-sink | kitchen-stove | kitchen-wide |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free | 3.67 | 3.33 | 4 | 2.67 | 3.5 | 3 | 3.33 | 3 | 3 | 4 | 3.33 | 4 |
| qwen/qwen3.8-27b:free | 3 | - | - | - | - | - | - | - | - | - | - | - |
| google/gemma-4-31b-it:free | - | - | - | - | - | - | - | - | - | - | - | - |
| z-ai/glm-5.3-flash | 3.67 | 3.33 | 3 | 3.33 | 3.67 | 3 | 4 | 2.5 | 3 | 2.67 | 3.33 | 3.33 |
| google/gemini-3.1-flash-lite | 3.67 | 4.33 | 3.33 | 3.67 | 4 | 4 | 4 | 3.33 | 2.67 | 2.67 | 4.33 | 3 |
| qwen/qwen3.6-flash | - | 5 | - | - | - | 4 | - | 3 | 2.33 | - | 3.5 | - |
| google/gemini-3.8-flash | 3.5 | 4.33 | 3 | 3.67 | 4 | 3.33 | 2.5 | 3 | 2.33 | 2 | 3.67 | 2.67 |
| anthropic/claude-haiku-4.5 | 3.67 | 3.67 | 3 | 2.67 | 3.67 | 3 | 3.67 | 3 | 3 | 2 | 3.67 | 3 |
| openai/gpt-5.4-mini | 3.33 | 4.33 | 3 | 2.33 | 3 | 3.67 | 2.33 | 3.33 | 3.33 | 2.67 | 4 | 3.33 |
| anthropic/claude-sonnet-5 | 3.33 | 3.67 | 3 | 3.33 | 3 | 3.67 | 2.67 | 3 | 2.67 | 3 | 3.33 | 3 |
| google/gemini-3.1-pro-preview | 4 | 4.33 | - | 3 | 3.5 | 4 | 3 | 2.33 | 4 | 1.67 | 3.5 | 2.67 |

## Multi-camera summaries (verbatim)

**nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free** on crownshy (judge 4, busiest: 'open kitchen pass', service: True, people: 15): The restaurant is in full evening service with active cooking and plating operations. The open kitchen allows diners to observe food preparation while staff work efficiently across multiple stations. Multiple dishes are being prepared simultaneously, indicating a high volume of service.

**nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free** on kitchen (judge 3, busiest: 'kitchen-prep', service: False, people: 1): The kitchen is occupied by a single person preparing food. No customers are visible, indicating service is not currently in progress. The environment appears organized with ingredients and tools ready for use.

**qwen/qwen3.8-27b:free** on crownshy (judge None, busiest: None, service: None, people: None): HTTP 429: {"error":{"message":"Provider returned error","code":429,"metadata":{"raw":"qwen/qwen3.8-27b:free is temporarily rate-limited upstream. Please retry shortly, or

**qwen/qwen3.8-27b:free** on kitchen (judge None, busiest: None, service: None, people: None): HTTP 429: {"error":{"message":"Provider returned error","code":429,"metadata":{"raw":"qwen/qwen3.8-27b:free is temporarily rate-limited upstream. Please retry shortly, or

**google/gemma-4-31b-it:free** on crownshy (judge None, busiest: None, service: None, people: None): HTTP 429: {"error":{"message":"Provider returned error","code":429,"metadata":{"raw":"google/gemma-4-31b-it:free is temporarily rate-limited upstream. Please retry shortl

**google/gemma-4-31b-it:free** on kitchen (judge None, busiest: None, service: None, people: None): HTTP 429: {"error":{"message":"Rate limit exceeded: free-models-per-min. ","code":429,"metadata":{"headers":{"X-RateLimit-Limit":"20","X-RateLimit-Remaining":"0","X-RateL

**z-ai/glm-5.3-flash** on crownshy (judge 4, busiest: 'Hot line/plating pass', service: True, people: 20): Evening service is in full swing: the dining room is largely seated and the open kitchen is staffed across every station. The grill, plancha, salamander and plating pass are all simultaneously active, with several finished dishes being plated at once. Prep is running in parallel with three cooks working vegetables for later service.

**z-ai/glm-5.3-flash** on kitchen (judge 2, busiest: 'main prep counter (grocery unpacking and ingredient staging beside the induction hob)', service: True, people: 1): A single cook is the only person present and is in the early stage of meal preparation, unpacking groceries and staging ingredients at the main counter. Vegetables, a knife and utensils are laid out by the prep sink, while an empty pot already sits on the induction hob awaiting use. The kitchen is otherwise tidy and calm, with no customers or other staff visible in any camera.

**google/gemini-3.1-flash-lite** on crownshy (judge 3, busiest: 'plating', service: True, people: 16): The restaurant is in the middle of a busy evening service with all stations fully operational. The kitchen team is coordinating between prep, cooking, and final plating to serve guests seated at the counter. Workflow appears efficient and synchronized across all areas.

**google/gemini-3.1-flash-lite** on kitchen (judge 2, busiest: 'prep area', service: False, people: 1): A single individual is currently working in the kitchen, primarily focused on reading a document at the prep station. The kitchen is in a state of preparation with ingredients laid out and a pot on the stove, but no active cooking or service is occurring. The environment appears orderly and quiet.

**qwen/qwen3.6-flash** on crownshy (judge None, busiest: None, service: None, people: None): The user wants a JSON status report based on the provided camera feeds.

**1. Analyze each camera feed:**
*   **crownshy-line:** Shows the main kitchen line. Several staff members are visible. One person in a grey apron is walking/standing near the pass. Others are working at stations further back. 

**qwen/qwen3.6-flash** on kitchen (judge None, busiest: None, service: None, people: None): The user wants a JSON report based on the provided camera feeds.

**1. Analyze the input:**
- **Cameras:** kitchen-overview, kitchen-wide, kitchen-stove, kitchen-prep, kitchen-sink.
- **Context:** Small institutional kitchen, single cook preparing a meal, faces blurred.
- **Goal:** Combine into one 

**google/gemini-3.8-flash** on crownshy (judge 3, busiest: 'plating pass', service: True, people: 20): Evening dinner service is fully underway with multiple tables seated in the dining room. The kitchen line, grill, and plancha are actively cooking proteins while finished dishes are being assembled at the pass. Prep staff in the back station continue processing vegetables to support service.

**google/gemini-3.8-flash** on kitchen (judge 3, busiest: 'main prep counter', service: False, people: 1): A single cook is preparing to begin a meal in an institutional kitchen environment. Raw vegetables, pasta, and cooking supplies are laid out across the prep surfaces and sink area. No active cooking has started yet while the cook consults paper instructions.

**anthropic/claude-haiku-4.5** on crownshy (judge 3, busiest: 'crownshy-line (main kitchen pass)', service: True, people: 18): The restaurant is in full evening service with approximately 18 distinct staff and customers across all areas. The open kitchen is operating at high capacity with active cooking, plating, and service occurring simultaneously across all stations. Dining room is occupied with customers being actively served while kitchen maintains coordinated workflow.

**anthropic/claude-haiku-4.5** on kitchen (judge 3, busiest: 'prep station', service: True, people: 1): One cook is actively preparing a meal in this institutional kitchen. Fresh vegetables are laid out on prep surfaces and a pot is heating on the stove, indicating active cooking in progress. The kitchen appears organized with ingredients and tools positioned for efficient meal preparation.

**openai/gpt-5.4-mini** on crownshy (judge 4, busiest: 'plating station', service: True, people: 19): The restaurant is in active evening service with both kitchen and dining room occupied. The open kitchen is busy across prep, grill, plancha, salamander, and plating stations. Several dishes are being finished at the pass while guests are seated nearby.

**openai/gpt-5.4-mini** on kitchen (judge 2, busiest: 'prep counter by the sink', service: True, people: 1): A single cook is working in the kitchen and appears to be preparing food. The prep surfaces are occupied with vegetables, utensils, and containers, while the stove is not actively in use. The kitchen is active and in the middle of meal preparation.

**anthropic/claude-sonnet-5** on crownshy (judge 4, busiest: 'crownshy-line (pass/expo area with dense crowd of cooks)', service: True, people: 20): The restaurant is in full evening service with the dining room seated and multiple kitchen stations actively cooking and plating dishes. The line and pass area appear busiest, with several cooks converging near the expo station while prep, grill, plancha, and salamander stations each handle individual tasks. Overall the kitchen and dining room show a coordinated, high-tempo fine-dining service flow.

**anthropic/claude-sonnet-5** on kitchen (judge 2, busiest: 'kitchen-stove/prep counter area', service: True, people: 1): A single cook is actively preparing a meal in the small kitchen, working between the stove, prep counter, and sink areas. A pot of water is heating on the induction stove while raw vegetables and pasta are staged for cooking. The kitchen is orderly with no other staff or customers present, indicating an early prep stage rather than active dining service.

**google/gemini-3.1-pro-preview** on crownshy (judge None, busiest: None, service: None, people: None): ```json
{
  "distinct_people": 28,
  "busiest": "main kitchen line",
  "service_in_progress": true,
  "per_camera": {
    "crownshy-line": "A wide view of the busy kitchen line

**google/gemini-3.1-pro-preview** on kitchen (judge None, busiest: None, service: None, people: None): {
  "distinct_people": 1,
  "busiest": "Stove and prep counter",
  "service_in_progress": false,
  "per_camera": {
    "kitchen-overview": "A person stands at a counter reading a document

## Hallucinations the judge flagged (sample)

- **nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free** (46): crownshy-line: exact count of 8 people; crownshy-line: open flame visible under pan on stove; crownshy-prep: Right person handling green vegetables (human says peeling carrots); crownshy-line: chef holding a cloth; crownshy-line: bare hand visible near a pan on the stove; crownshy-dining: diners described as seated at tables, not counter seating
- **qwen/qwen3.8-27b:free** (2): crownshy-dining: Claims 13 people, far exceeding the described 8-9; kitchen-stove: Describes a pot of water on cooktop, but pan stays empty
- **z-ai/glm-5.3-flash** (87): crownshy-line: Exact people count of 8 not confirmed by human; crownshy-line: Open gas burner flame unattended claim; crownshy-line: Beverage cups stored on counter; crownshy-line: Loose knives near counter edge; crownshy-dining: specific count of 9 people; crownshy-dining: server in a white shirt standing by
- **google/gemini-3.1-flash-lite** (57): crownshy-line: Counted 8 people, more than human's 4-5; crownshy-line: Claims a knife was left out; crownshy-line: exact count of 8 people not confirmed; crownshy-line: 10 people (likely overcounted staff); crownshy-line: knife left out (unverified detail); crownshy-dining: claims 11 people, more than the ~8 described
- **qwen/qwen3.6-flash** (20): crownshy-prep: No hair restraint - not mentioned or contradicted by human description; kitchen-overview: zucchini, eggplant, and yellow pepper (human saw cucumber and orange); kitchen-overview: several bottles near person; kitchen-overview: loose electrical cords hanging near power strip; kitchen-stove: Box described as blue, human said white; kitchen-stove: Trash bin positioned near walkway
- **google/gemini-3.8-flash** (84): crownshy-line: counted 9 people, exceeds human estimate of 4-5; crownshy-line: cook walking down aisle carrying a towel; crownshy-line: Specific count of 9 people; crownshy-line: Knife left resting improperly in a sink container; crownshy-dining: exact count of 10 people; crownshy-dining: front-of-house staff standing by to run orders
- **anthropic/claude-haiku-4.5** (78): crownshy-line: 12 people (human implies ~5-8); crownshy-line: knife visible on counter; crownshy-line: Exact count of 12 people not confirmed; crownshy-line: Knife visible on counter as safety hazard; crownshy-line: Count of 8 people (human indicates about 4-5 staff); crownshy-line: Knife visible on the left counter (not mentioned)
- **openai/gpt-5.4-mini** (75): crownshy-line: 10 people is likely too many (human implies ~5-6); crownshy-line: open flame under front-right pan not mentioned; crownshy-line: walkway blocked by staff member and equipment; crownshy-line: exact count of 8 people; crownshy-line: open flame visible under a pan; crownshy-dining: chef stands with his back to the camera
- **anthropic/claude-sonnet-5** (116): crownshy-line: Exact count of 9 people; crownshy-line: Grill present on the line; crownshy-line: Disposable cups and containers on counter; crownshy-line: 11 people (human suggests ~4-6 total); crownshy-line: staff gathered near back possibly during briefing; crownshy-line: knife/utensil resting on prep counter
- **google/gemini-3.1-pro-preview** (53): crownshy-dining: exact count of 10 people not confirmed by human description; crownshy-prep: green stalks; crownshy-prep: unattended knife framed as an issue; crownshy-grill: Chef using tongs (human describes pan work, not tongs); crownshy-grill: Chef wearing watch and bracelets on wrists; crownshy-plancha: 3 people total, two other kitchen staff at prep and plating stations

## Failures

- nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free single crownshy-prep#2: 'api: {"message": "Upstream error from Nvidia: ResourceExhausted: Worker local total request limit reached (16/16)", "code": 502, "metadata": {"error_type": "provider_unavailable"}}'
- nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free single crownshy-plating#1: 'api: {"message": "Upstream error from Nvidia: ResourceExhausted: Worker local total request limit reached (16/16)", "code": 502, "metadata": {"error_type": "provider_unavailable"}}'
- nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free single kitchen-sink#1: 'api: {"message": "Upstream error from Nvidia: ResourceExhausted: Worker local total request limit reached (16/16)", "code": 502, "metadata": {"error_type": "provider_unavailable"}}'
- nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free temporal kitchen-wide#: 'HTTP 429: {"error":{"message":"Rate limit exceeded: free-models-per-min. ","code":429,"metadata":{"headers":{"X-RateLimit-Limit":"20","X-RateLimit-Remaining":"0","X-RateL'
- qwen/qwen3.8-27b:free single crownshy-line#1: 'HTTP 429: {"error":{"message":"Provider returned error","code":429,"metadata":{"raw":"qwen/qwen3.8-27b:free is temporarily rate-limited upstream. Please retry shortly, or'
- qwen/qwen3.8-27b:free single crownshy-line#2: 'HTTP 429: {"error":{"message":"Provider returned error","code":429,"metadata":{"raw":"qwen/qwen3.8-27b:free is temporarily rate-limited upstream. Please retry shortly, or'
- qwen/qwen3.8-27b:free single crownshy-line#3: 'HTTP 429: {"error":{"message":"Provider returned error","code":429,"metadata":{"raw":"qwen/qwen3.8-27b:free is temporarily rate-limited upstream. Please retry shortly, or'
- qwen/qwen3.8-27b:free single crownshy-dining#1: 'HTTP 429: {"error":{"message":"Provider returned error","code":429,"metadata":{"raw":"qwen/qwen3.8-27b:free is temporarily rate-limited upstream. Please retry shortly, or'
- qwen/qwen3.8-27b:free single crownshy-dining#2: 'HTTP 429: {"error":{"message":"Rate limit exceeded: free-models-per-min. ","code":429,"metadata":{"headers":{"X-RateLimit-Limit":"20","X-RateLimit-Remaining":"0","X-RateL'
- qwen/qwen3.8-27b:free single crownshy-prep#1: 'HTTP 429: {"error":{"message":"Provider returned error","code":429,"metadata":{"raw":"qwen/qwen3.8-27b:free is temporarily rate-limited upstream. Please retry shortly, or'
- qwen/qwen3.8-27b:free single crownshy-prep#2: 'HTTP 429: {"error":{"message":"Provider returned error","code":429,"metadata":{"raw":"qwen/qwen3.8-27b:free is temporarily rate-limited upstream. Please retry shortly, or'
- qwen/qwen3.8-27b:free single crownshy-prep#3: 'HTTP 429: {"error":{"message":"Rate limit exceeded: free-models-per-min. ","code":429,"metadata":{"headers":{"X-RateLimit-Limit":"20","X-RateLimit-Remaining":"0","X-RateL'
- qwen/qwen3.8-27b:free single crownshy-grill#1: 'HTTP 429: {"error":{"message":"Provider returned error","code":429,"metadata":{"raw":"qwen/qwen3.8-27b:free is temporarily rate-limited upstream. Please retry shortly, or'
- qwen/qwen3.8-27b:free single crownshy-grill#2: 'HTTP 429: {"error":{"message":"Provider returned error","code":429,"metadata":{"raw":"qwen/qwen3.8-27b:free is temporarily rate-limited upstream. Please retry shortly, or'
- qwen/qwen3.8-27b:free single crownshy-grill#3: 'HTTP 429: {"error":{"message":"Rate limit exceeded: free-models-per-min. ","code":429,"metadata":{"headers":{"X-RateLimit-Limit":"20","X-RateLimit-Remaining":"0","X-RateL'
- qwen/qwen3.8-27b:free single crownshy-plancha#1: 'HTTP 429: {"error":{"message":"Rate limit exceeded: free-models-per-min. ","code":429,"metadata":{"headers":{"X-RateLimit-Limit":"20","X-RateLimit-Remaining":"0","X-RateL'
- qwen/qwen3.8-27b:free single crownshy-salamander#3: "unparseable answer: Let me carefully examine this frame.\n\nThe image shows a commercial kitchen station. There's a large stainless steel rang"
- qwen/qwen3.8-27b:free single crownshy-plancha#2: 'HTTP 429: {"error":{"message":"Rate limit exceeded: free-models-per-min. ","code":429,"metadata":{"headers":{"X-RateLimit-Limit":"20","X-RateLimit-Remaining":"0","X-RateL'
- qwen/qwen3.8-27b:free single crownshy-salamander#1: 'HTTP 429: {"error":{"message":"Provider returned error","code":429,"metadata":{"raw":"qwen/qwen3.8-27b:free is temporarily rate-limited upstream. Please retry shortly, or'
- qwen/qwen3.8-27b:free single crownshy-plancha#3: 'HTTP 429: {"error":{"message":"Rate limit exceeded: free-models-per-min. ","code":429,"metadata":{"headers":{"X-RateLimit-Limit":"20","X-RateLimit-Remaining":"0","X-RateL'
- qwen/qwen3.8-27b:free single crownshy-salamander#2: 'HTTP 429: {"error":{"message":"Provider returned error","code":429,"metadata":{"raw":"qwen/qwen3.8-27b:free is temporarily rate-limited upstream. Please retry shortly, or'
- qwen/qwen3.8-27b:free single crownshy-plating#2: 'HTTP 429: {"error":{"message":"Rate limit exceeded: free-models-per-min. ","code":429,"metadata":{"headers":{"X-RateLimit-Limit":"20","X-RateLimit-Remaining":"0","X-RateL'
- qwen/qwen3.8-27b:free single crownshy-plating#1: 'HTTP 429: {"error":{"message":"Provider returned error","code":429,"metadata":{"raw":"qwen/qwen3.8-27b:free is temporarily rate-limited upstream. Please retry shortly, or'
- qwen/qwen3.8-27b:free single crownshy-plating#3: 'HTTP 429: {"error":{"message":"Rate limit exceeded: free-models-per-min. ","code":429,"metadata":{"headers":{"X-RateLimit-Limit":"20","X-RateLimit-Remaining":"0","X-RateL'
- qwen/qwen3.8-27b:free single kitchen-overview#1: 'HTTP 429: {"error":{"message":"Provider returned error","code":429,"metadata":{"raw":"qwen/qwen3.8-27b:free is temporarily rate-limited upstream. Please retry shortly, or'
- qwen/qwen3.8-27b:free single kitchen-overview#2: 'HTTP 429: {"error":{"message":"Provider returned error","code":429,"metadata":{"raw":"qwen/qwen3.8-27b:free is temporarily rate-limited upstream. Please retry shortly, or'
- qwen/qwen3.8-27b:free single kitchen-overview#3: 'HTTP 429: {"error":{"message":"Provider returned error","code":429,"metadata":{"raw":"qwen/qwen3.8-27b:free is temporarily rate-limited upstream. Please retry shortly, or'
- qwen/qwen3.8-27b:free multicam crownshy#: 'HTTP 429: {"error":{"message":"Provider returned error","code":429,"metadata":{"raw":"qwen/qwen3.8-27b:free is temporarily rate-limited upstream. Please retry shortly, or'
- qwen/qwen3.8-27b:free single kitchen-wide#1: "unparseable answer: Let me carefully examine this frame.\n\nI see a small kitchen. There's one person visible—a woman with a ponytail, wearing"
- qwen/qwen3.8-27b:free single kitchen-wide#2: 'HTTP 429: {"error":{"message":"Provider returned error","code":429,"metadata":{"raw":"qwen/qwen3.8-27b:free is temporarily rate-limited upstream. Please retry shortly, or'
- qwen/qwen3.8-27b:free single kitchen-wide#3: 'HTTP 429: {"error":{"message":"Rate limit exceeded: free-models-per-min. ","code":429,"metadata":{"headers":{"X-RateLimit-Limit":"20","X-RateLimit-Remaining":"0","X-RateL'
- qwen/qwen3.8-27b:free single kitchen-stove#1: 'HTTP 429: {"error":{"message":"Provider returned error","code":429,"metadata":{"raw":"qwen/qwen3.8-27b:free is temporarily rate-limited upstream. Please retry shortly, or'
- qwen/qwen3.8-27b:free single kitchen-stove#2: 'HTTP 429: {"error":{"message":"Rate limit exceeded: free-models-per-min. ","code":429,"metadata":{"headers":{"X-RateLimit-Limit":"20","X-RateLimit-Remaining":"0","X-RateL'
- qwen/qwen3.8-27b:free single kitchen-stove#3: 'HTTP 429: {"error":{"message":"Rate limit exceeded: free-models-per-min. ","code":429,"metadata":{"headers":{"X-RateLimit-Limit":"20","X-RateLimit-Remaining":"0","X-RateL'
- qwen/qwen3.8-27b:free single kitchen-prep#1: 'HTTP 429: {"error":{"message":"Rate limit exceeded: free-models-per-min. ","code":429,"metadata":{"headers":{"X-RateLimit-Limit":"20","X-RateLimit-Remaining":"0","X-RateL'
- qwen/qwen3.8-27b:free single kitchen-prep#2: 'HTTP 429: {"error":{"message":"Rate limit exceeded: free-models-per-min. ","code":429,"metadata":{"headers":{"X-RateLimit-Limit":"20","X-RateLimit-Remaining":"0","X-RateL'
- qwen/qwen3.8-27b:free single kitchen-prep#3: 'HTTP 429: {"error":{"message":"Rate limit exceeded: free-models-per-min. ","code":429,"metadata":{"headers":{"X-RateLimit-Limit":"20","X-RateLimit-Remaining":"0","X-RateL'
- qwen/qwen3.8-27b:free single kitchen-sink#2: 'HTTP 429: {"error":{"message":"Rate limit exceeded: free-models-per-min. ","code":429,"metadata":{"headers":{"X-RateLimit-Limit":"20","X-RateLimit-Remaining":"0","X-RateL'
- qwen/qwen3.8-27b:free single kitchen-sink#1: 'unparseable answer: Let me carefully analyze this frame.\n\nI see one person in the center, wearing a gray t-shirt, with a blurred face, and w'
- qwen/qwen3.8-27b:free multicam kitchen#: 'HTTP 429: {"error":{"message":"Provider returned error","code":429,"metadata":{"raw":"qwen/qwen3.8-27b:free is temporarily rate-limited upstream. Please retry shortly, or'
- qwen/qwen3.8-27b:free temporal kitchen-wide#: 'HTTP 429: {"error":{"message":"Provider returned error","code":429,"metadata":{"raw":"qwen/qwen3.8-27b:free is temporarily rate-limited upstream. Please retry shortly, or'
- qwen/qwen3.8-27b:free temporal kitchen-overview#: 'HTTP 429: {"error":{"message":"Provider returned error","code":429,"metadata":{"raw":"qwen/qwen3.8-27b:free is temporarily rate-limited upstream. Please retry shortly, or'
- qwen/qwen3.8-27b:free single kitchen-sink#3: 'unparseable answer: The user wants a JSON report based on the provided image. I need to act as a vision analyst for a restaurant operations '
- qwen/qwen3.8-27b:free temporal kitchen-prep#: 'HTTP 429: {"error":{"message":"Provider returned error","code":429,"metadata":{"raw":"qwen/qwen3.8-27b:free is temporarily rate-limited upstream. Please retry shortly, or'
- qwen/qwen3.8-27b:free temporal kitchen-sink#: 'HTTP 429: {"error":{"message":"Rate limit exceeded: free-models-per-min. ","code":429,"metadata":{"headers":{"X-RateLimit-Limit":"20","X-RateLimit-Remaining":"0","X-RateL'
- google/gemma-4-31b-it:free single crownshy-line#1: 'HTTP 429: {"error":{"message":"Provider returned error","code":429,"metadata":{"raw":"google/gemma-4-31b-it:free is temporarily rate-limited upstream. Please retry shortl'
- google/gemma-4-31b-it:free single crownshy-line#2: 'HTTP 429: {"error":{"message":"Rate limit exceeded: free-models-per-min. ","code":429,"metadata":{"headers":{"X-RateLimit-Limit":"20","X-RateLimit-Remaining":"0","X-RateL'
- google/gemma-4-31b-it:free single crownshy-line#3: 'HTTP 429: {"error":{"message":"Provider returned error","code":429,"metadata":{"raw":"google/gemma-4-31b-it:free is temporarily rate-limited upstream. Please retry shortl'
- google/gemma-4-31b-it:free single crownshy-dining#1: 'HTTP 429: {"error":{"message":"Rate limit exceeded: free-models-per-min. ","code":429,"metadata":{"headers":{"X-RateLimit-Limit":"20","X-RateLimit-Remaining":"0","X-RateL'
- google/gemma-4-31b-it:free single crownshy-dining#3: 'HTTP 429: {"error":{"message":"Rate limit exceeded: free-models-per-min. ","code":429,"metadata":{"headers":{"X-RateLimit-Limit":"20","X-RateLimit-Remaining":"0","X-RateL'
- google/gemma-4-31b-it:free single crownshy-dining#2: 'HTTP 429: {"error":{"message":"Rate limit exceeded: free-models-per-min. ","code":429,"metadata":{"headers":{"X-RateLimit-Limit":"20","X-RateLimit-Remaining":"0","X-RateL'
- google/gemma-4-31b-it:free single crownshy-prep#1: 'HTTP 429: {"error":{"message":"Rate limit exceeded: free-models-per-min. ","code":429,"metadata":{"headers":{"X-RateLimit-Limit":"20","X-RateLimit-Remaining":"0","X-RateL'
- google/gemma-4-31b-it:free single crownshy-prep#2: 'HTTP 429: {"error":{"message":"Rate limit exceeded: free-models-per-min. ","code":429,"metadata":{"headers":{"X-RateLimit-Limit":"20","X-RateLimit-Remaining":"0","X-RateL'
- google/gemma-4-31b-it:free single crownshy-prep#3: 'HTTP 429: {"error":{"message":"Provider returned error","code":429,"metadata":{"raw":"google/gemma-4-31b-it:free is temporarily rate-limited upstream. Please retry shortl'
- google/gemma-4-31b-it:free single crownshy-grill#1: 'HTTP 429: {"error":{"message":"Rate limit exceeded: free-models-per-min. ","code":429,"metadata":{"headers":{"X-RateLimit-Limit":"20","X-RateLimit-Remaining":"0","X-RateL'
- google/gemma-4-31b-it:free single crownshy-grill#2: 'HTTP 429: {"error":{"message":"Rate limit exceeded: free-models-per-min. ","code":429,"metadata":{"headers":{"X-RateLimit-Limit":"20","X-RateLimit-Remaining":"0","X-RateL'
- google/gemma-4-31b-it:free single crownshy-grill#3: 'HTTP 429: {"error":{"message":"Rate limit exceeded: free-models-per-min. ","code":429,"metadata":{"headers":{"X-RateLimit-Limit":"20","X-RateLimit-Remaining":"0","X-RateL'
- google/gemma-4-31b-it:free single crownshy-plancha#1: 'HTTP 429: {"error":{"message":"Provider returned error","code":429,"metadata":{"raw":"google/gemma-4-31b-it:free is temporarily rate-limited upstream. Please retry shortl'
- google/gemma-4-31b-it:free single crownshy-plancha#2: 'HTTP 429: {"error":{"message":"Provider returned error","code":429,"metadata":{"raw":"google/gemma-4-31b-it:free is temporarily rate-limited upstream. Please retry shortl'
- google/gemma-4-31b-it:free single crownshy-plancha#3: 'HTTP 429: {"error":{"message":"Provider returned error","code":429,"metadata":{"raw":"google/gemma-4-31b-it:free is temporarily rate-limited upstream. Please retry shortl'
- google/gemma-4-31b-it:free single crownshy-salamander#1: 'HTTP 429: {"error":{"message":"Rate limit exceeded: free-models-per-min. ","code":429,"metadata":{"headers":{"X-RateLimit-Limit":"20","X-RateLimit-Remaining":"0","X-RateL'
- google/gemma-4-31b-it:free single crownshy-salamander#2: 'HTTP 429: {"error":{"message":"Provider returned error","code":429,"metadata":{"raw":"google/gemma-4-31b-it:free is temporarily rate-limited upstream. Please retry shortl'
- google/gemma-4-31b-it:free single crownshy-salamander#3: 'HTTP 429: {"error":{"message":"Rate limit exceeded: free-models-per-min. ","code":429,"metadata":{"headers":{"X-RateLimit-Limit":"20","X-RateLimit-Remaining":"0","X-RateL'
- google/gemma-4-31b-it:free single crownshy-plating#1: 'HTTP 429: {"error":{"message":"Rate limit exceeded: free-models-per-min. ","code":429,"metadata":{"headers":{"X-RateLimit-Limit":"20","X-RateLimit-Remaining":"0","X-RateL'
- google/gemma-4-31b-it:free single crownshy-plating#2: 'HTTP 429: {"error":{"message":"Rate limit exceeded: free-models-per-min. ","code":429,"metadata":{"headers":{"X-RateLimit-Limit":"20","X-RateLimit-Remaining":"0","X-RateL'
- google/gemma-4-31b-it:free single crownshy-plating#3: 'HTTP 429: {"error":{"message":"Rate limit exceeded: free-models-per-min. ","code":429,"metadata":{"headers":{"X-RateLimit-Limit":"20","X-RateLimit-Remaining":"0","X-RateL'
- google/gemma-4-31b-it:free single kitchen-overview#1: 'HTTP 429: {"error":{"message":"Rate limit exceeded: free-models-per-min. ","code":429,"metadata":{"headers":{"X-RateLimit-Limit":"20","X-RateLimit-Remaining":"0","X-RateL'
- google/gemma-4-31b-it:free multicam crownshy#: 'HTTP 429: {"error":{"message":"Provider returned error","code":429,"metadata":{"raw":"google/gemma-4-31b-it:free is temporarily rate-limited upstream. Please retry shortl'
- google/gemma-4-31b-it:free single kitchen-overview#2: 'HTTP 429: {"error":{"message":"Provider returned error","code":429,"metadata":{"raw":"google/gemma-4-31b-it:free is temporarily rate-limited upstream. Please retry shortl'
- google/gemma-4-31b-it:free single kitchen-overview#3: 'HTTP 429: {"error":{"message":"Rate limit exceeded: free-models-per-min. ","code":429,"metadata":{"headers":{"X-RateLimit-Limit":"20","X-RateLimit-Remaining":"0","X-RateL'
- google/gemma-4-31b-it:free single kitchen-wide#1: 'HTTP 429: {"error":{"message":"Provider returned error","code":429,"metadata":{"raw":"google/gemma-4-31b-it:free is temporarily rate-limited upstream. Please retry shortl'
- google/gemma-4-31b-it:free single kitchen-wide#2: 'HTTP 429: {"error":{"message":"Provider returned error","code":429,"metadata":{"raw":"google/gemma-4-31b-it:free is temporarily rate-limited upstream. Please retry shortl'
- google/gemma-4-31b-it:free single kitchen-wide#3: 'HTTP 429: {"error":{"message":"Rate limit exceeded: free-models-per-min. ","code":429,"metadata":{"headers":{"X-RateLimit-Limit":"20","X-RateLimit-Remaining":"0","X-RateL'
- google/gemma-4-31b-it:free single kitchen-stove#1: 'HTTP 429: {"error":{"message":"Provider returned error","code":429,"metadata":{"raw":"google/gemma-4-31b-it:free is temporarily rate-limited upstream. Please retry shortl'
- google/gemma-4-31b-it:free single kitchen-stove#2: 'HTTP 429: {"error":{"message":"Rate limit exceeded: free-models-per-min. ","code":429,"metadata":{"headers":{"X-RateLimit-Limit":"20","X-RateLimit-Remaining":"0","X-RateL'
- google/gemma-4-31b-it:free single kitchen-stove#3: 'HTTP 429: {"error":{"message":"Provider returned error","code":429,"metadata":{"raw":"google/gemma-4-31b-it:free is temporarily rate-limited upstream. Please retry shortl'
- google/gemma-4-31b-it:free single kitchen-prep#1: 'HTTP 429: {"error":{"message":"Rate limit exceeded: free-models-per-min. ","code":429,"metadata":{"headers":{"X-RateLimit-Limit":"20","X-RateLimit-Remaining":"0","X-RateL'
- google/gemma-4-31b-it:free single kitchen-prep#2: 'HTTP 429: {"error":{"message":"Rate limit exceeded: free-models-per-min. ","code":429,"metadata":{"headers":{"X-RateLimit-Limit":"20","X-RateLimit-Remaining":"0","X-RateL'
- google/gemma-4-31b-it:free single kitchen-prep#3: 'HTTP 429: {"error":{"message":"Provider returned error","code":429,"metadata":{"raw":"google/gemma-4-31b-it:free is temporarily rate-limited upstream. Please retry shortl'
- google/gemma-4-31b-it:free single kitchen-sink#1: 'HTTP 429: {"error":{"message":"Rate limit exceeded: free-models-per-min. ","code":429,"metadata":{"headers":{"X-RateLimit-Limit":"20","X-RateLimit-Remaining":"0","X-RateL'
- google/gemma-4-31b-it:free single kitchen-sink#2: 'HTTP 429: {"error":{"message":"Rate limit exceeded: free-models-per-min. ","code":429,"metadata":{"headers":{"X-RateLimit-Limit":"20","X-RateLimit-Remaining":"0","X-RateL'
- google/gemma-4-31b-it:free single kitchen-sink#3: 'HTTP 429: {"error":{"message":"Rate limit exceeded: free-models-per-min. ","code":429,"metadata":{"headers":{"X-RateLimit-Limit":"20","X-RateLimit-Remaining":"0","X-RateL'
- google/gemma-4-31b-it:free multicam kitchen#: 'HTTP 429: {"error":{"message":"Rate limit exceeded: free-models-per-min. ","code":429,"metadata":{"headers":{"X-RateLimit-Limit":"20","X-RateLimit-Remaining":"0","X-RateL'
- google/gemma-4-31b-it:free temporal kitchen-overview#: 'HTTP 429: {"error":{"message":"Provider returned error","code":429,"metadata":{"raw":"google/gemma-4-31b-it:free is temporarily rate-limited upstream. Please retry shortl'
- google/gemma-4-31b-it:free temporal kitchen-wide#: 'HTTP 429: {"error":{"message":"Provider returned error","code":429,"metadata":{"raw":"google/gemma-4-31b-it:free is temporarily rate-limited upstream. Please retry shortl'
- z-ai/glm-5.3-flash single crownshy-line#1: 'unparseable answer: Let me analyze this frame carefully.\n\nThe image shows a professional kitchen during evening service. Let me count people'
- google/gemma-4-31b-it:free temporal kitchen-stove#: 'HTTP 429: {"error":{"message":"Provider returned error","code":429,"metadata":{"raw":"google/gemma-4-31b-it:free is temporarily rate-limited upstream. Please retry shortl'
- google/gemma-4-31b-it:free temporal kitchen-prep#: 'HTTP 429: {"error":{"message":"Provider returned error","code":429,"metadata":{"raw":"google/gemma-4-31b-it:free is temporarily rate-limited upstream. Please retry shortl'
- z-ai/glm-5.3-flash single crownshy-line#2: 'unparseable answer: {"people": 10, "area": "Main hot line / cook\'s line of the open kitchen, showing the grill and range section with the pa'
- google/gemma-4-31b-it:free temporal kitchen-sink#: 'HTTP 429: {"error":{"message":"Provider returned error","code":429,"metadata":{"raw":"google/gemma-4-31b-it:free is temporarily rate-limited upstream. Please retry shortl'
- z-ai/glm-5.3-flash single crownshy-salamander#1: "unparseable answer: Let me analyze this frame carefully.\n\nThe image shows a kitchen scene, viewed from above/behind a person's head (a chef "
- z-ai/glm-5.3-flash single kitchen-overview#2: "unparseable answer: Let me analyze this frame carefully.\n\nThe image shows a small kitchen area. There's one person visible - a woman with lo"
- z-ai/glm-5.3-flash single kitchen-prep#3: "unparseable answer: Let me analyze this frame carefully.\n\nThe image shows a kitchen scene, viewed from above a sink area. There's a stainles"
- qwen/qwen3.6-flash single crownshy-line#1: 'unparseable answer: The user wants a JSON report based on the provided image of a restaurant kitchen.\n\n**1. Analyze the image:**\n*   **Peopl'
- qwen/qwen3.6-flash single crownshy-dining#1: 'unparseable answer: The user wants a JSON report based on the provided image of a restaurant kitchen.\n\n**1. Analyze the image:**\n*   **Peopl'
- qwen/qwen3.6-flash single crownshy-dining#2: 'unparseable answer: The user wants a JSON report based on the provided image of a restaurant kitchen.\n\n**1. Analyze the image:**\n*   **Peopl'
- qwen/qwen3.6-flash single crownshy-line#2: 'unparseable answer: The user wants a JSON report based on the provided image of a restaurant kitchen.\n\n**1. Analyze the image:**\n*   **Scene'
- qwen/qwen3.6-flash single crownshy-dining#3: 'unparseable answer: The user wants a JSON report based on the provided image of a restaurant kitchen.\n\n**1. Analyze the image:**\n*   **Peopl'
- qwen/qwen3.6-flash single crownshy-prep#2: 'unparseable answer: The user wants a JSON report based on the provided image of a restaurant kitchen.\n\n**1. Analyze the Image:**\n*   **Peopl'
- qwen/qwen3.6-flash single crownshy-line#3: 'unparseable answer: The user wants a JSON report based on the provided image of a restaurant kitchen.\n\n**1. Analyze the image:**\n*   **Area:'
- qwen/qwen3.6-flash single crownshy-prep#3: 'unparseable answer: The user wants a JSON report on the provided image.\n1.  **Identify People:** I see three people clearly visible in the f'
- qwen/qwen3.6-flash single crownshy-plancha#1: 'unparseable answer: The user wants a JSON report based on the provided image of a restaurant kitchen.\n\n**1. Analyze the Image:**\n*   **Area:'
- qwen/qwen3.6-flash single crownshy-grill#1: 'unparseable answer: The user wants a JSON report based on the provided image of a restaurant kitchen.\n\n**1. Analyze the Image:**\n*   **Area:'
- qwen/qwen3.6-flash single crownshy-salamander#2: 'unparseable answer: The user wants a JSON report based on the provided image of a restaurant kitchen.\n\n**1. Analyze the Image:**\n*   **Peopl'
- qwen/qwen3.6-flash single crownshy-plancha#2: 'unparseable answer: The user wants a JSON report based on the provided image of a restaurant kitchen.\n\n**1. Analyze the image:**\n*   **Peopl'
- qwen/qwen3.6-flash single crownshy-plancha#3: 'unparseable answer: The user wants a JSON report based on the provided image of a restaurant kitchen.\n\n**1. Analyze the Image:**\n*   **Scene'
- qwen/qwen3.6-flash single crownshy-salamander#1: 'unparseable answer: The user wants a JSON report based on the provided image of a restaurant kitchen.\n\n**1. Analyze the image:**\n*   **Peopl'
- qwen/qwen3.6-flash single crownshy-plating#1: 'unparseable answer: The user wants a JSON report based on the provided image of a restaurant kitchen.\n\n**1. Analyze the Image:**\n*   **Persp'
- qwen/qwen3.6-flash single crownshy-salamander#3: 'unparseable answer: The user wants a JSON report based on the provided image of a restaurant kitchen.\n\n**1. Analyze the Image:**\n*   **Area:'
- qwen/qwen3.6-flash single crownshy-plating#2: 'unparseable answer: The user wants a JSON report based on the provided image of a restaurant kitchen.\n\n**1. Analyze the image:**\n*   **Persp'
- qwen/qwen3.6-flash single crownshy-plating#3: 'unparseable answer: The user wants a JSON report based on the provided image of a restaurant kitchen.\n\n**1. Analyze the image:**\n*   **Persp'
- qwen/qwen3.6-flash single kitchen-overview#3: 'unparseable answer: The user wants a JSON report based on the provided image.\n\n**1. Analyze the image:**\n*   **Scene:** It looks like a smal'
- qwen/qwen3.6-flash single kitchen-overview#2: 'unparseable answer: The user wants a JSON report based on the provided image of a kitchen.\n\n**1. Analyze the image:**\n*   **People:** I see '
- qwen/qwen3.6-flash single kitchen-wide#1: 'unparseable answer: The user wants a JSON report based on the provided image of a kitchen.\n\n1.  **Analyze the image:**\n    *   **People:** I'
- qwen/qwen3.6-flash single kitchen-wide#2: 'unparseable answer: ```json\n{\n  "people": 1,\n  "area": "Kitchen prep station",\n  "'
- qwen/qwen3.6-flash multicam crownshy#: 'unparseable answer: The user wants a JSON status report based on the provided camera feeds.\n\n**1. Analyze each camera feed:**\n*   **crownshy'
- qwen/qwen3.6-flash single kitchen-wide#3: 'unparseable answer: The user wants a JSON report based on the provided image of a kitchen.\n\n**1. Analyze the image:**\n*   **People:** I see '
- qwen/qwen3.6-flash single kitchen-stove#2: 'unparseable answer: ```json\n{\n  "people": 1,\n  "area": "Kitchen prep and cooking station",\n  "activity": "A worker stands at the far end of '
- qwen/qwen3.6-flash single kitchen-sink#2: 'unparseable answer: The user wants a JSON report based on the provided image of a kitchen.\n\n**1. Analyze the image:**\n*   **People:** I see '
- qwen/qwen3.6-flash single kitchen-sink#1: 'unparseable answer: The user wants a JSON report based on the provided image of a kitchen.\n\n1.  **Analyze the image:**\n    *   **People:** I'
- qwen/qwen3.6-flash single kitchen-sink#3: 'unparseable answer: The user wants a JSON report based on the provided image of a kitchen.\n\n1.  **Analyze the image:**\n    *   **People:** I'
- qwen/qwen3.6-flash temporal kitchen-overview#: 'unparseable answer: The user wants a description of changes across three frames from a fixed camera in a kitchen.\nI need to identify:\n1.  **'
- google/gemini-3.8-flash single crownshy-line#2: 'unparseable answer: {"people": 16, "area": "hot line and main kitchen cooking suite", "activity": "A line cook stands in the central aisle b'
- qwen/qwen3.6-flash temporal kitchen-stove#: 'unparseable answer: The user wants a description of changes across three frames from a restaurant kitchen camera.\nI need to identify:\n1.  **'
- qwen/qwen3.6-flash multicam kitchen#: 'unparseable answer: The user wants a JSON report based on the provided camera feeds.\n\n**1. Analyze the input:**\n- **Cameras:** kitchen-overv'
- qwen/qwen3.6-flash temporal kitchen-prep#: 'unparseable answer: The user wants a description of changes across three frames from a fixed camera in a kitchen.\nI need to output a JSON ob'
- qwen/qwen3.6-flash temporal kitchen-sink#: 'unparseable answer: The user wants a description of changes across three frames from a restaurant kitchen.\nI need to identify:\n1.  **People:'
- google/gemini-3.8-flash single crownshy-dining#2: 'unparseable answer: ```json\n{"people": 11,\n "area": "open kitchen pass and chef\'s counter dining area",\n "activity": "Kitchen staff are acti'
- google/gemini-3.8-flash single crownshy-salamander#3: 'unparseable answer: {"people": 3, "area": "hot line / grill and salamander station", "activity": "A cook stands by the salamander holding a '
- google/gemini-3.8-flash single crownshy-plating#1: 'unparseable answer: {"people": 4, "area": "kitchen pass and plating station", "activity": "Kitchen staff are garnishing and inspecting multi'
- google/gemini-3.1-pro-preview single crownshy-line#2: 'unparseable answer: {\n"people": 14,\n"area": "main kitchen line and prep station",\n"activity": "A chef in the midground is wiping his hands w'
- google/gemini-3.1-pro-preview single crownshy-line#3: 'unparseable answer: {\n"people": 12,\n"area": "hot line and prep kitchen",\n"activity": "Chefs are actively working at their stations on the ho'
- google/gemini-3.1-pro-preview single crownshy-line#1: 'unparseable answer: {\n"people": 11,\n"area": "hot line and prep kitchen",\n"activity": "A chef is walking down the center aisle holding a towe'
- google/gemini-3.1-pro-preview single crownshy-dining#3: 'unparseable answer: {\n"people": 13,\n"area": "Open kitchen and adjacent dining area",\n"activity": "Kitchen staff are actively preparing food '
- google/gemini-3.1-pro-preview single crownshy-dining#2: 'unparseable answer: {\n"people": 13,\n"area": "Open kitchen and chef\'s counter",\n"activity": "Kitchen staff are preparing food and conversing '
- google/gemini-3.1-pro-preview single crownshy-prep#2: 'unparseable answer: {\n  "people": 3,\n  "area": "Kitchen prep station",\n  "activity": "Three kitchen staff members are standing at a stainles'
- google/gemini-3.1-pro-preview single crownshy-prep#3: 'unparseable answer: {\n  "people": 3,\n  "area": "Prep station",\n  "activity": "Three kitchen staff members are actively preparing vegetables '
- google/gemini-3.1-pro-preview single crownshy-plancha#1: 'unparseable answer: {\n"people": 3,\n"area": "Plancha and grill station",\n"activity": "Octopus, diced meat, and cubed ingredients are cooking '
- google/gemini-3.1-pro-preview single crownshy-salamander#1: 'unparseable answer: {\n  "people": 3,\n  "area": "Hot line and plating station in front of the broilers",\n  "activity": "A chef stands at the '
- google/gemini-3.1-pro-preview single crownshy-salamander#2: 'unparseable answer: {\n"people": 3,\n"area": "Hot line and broiler station",\n"activity": "A cook in the center is standing at the stainless st'
- google/gemini-3.1-pro-preview single crownshy-plating#1: 'unparseable answer: {\n  "people": 5,\n  "area": "kitchen pass and plating station",\n  "activity": "Chefs are actively plating and finishing m'
- google/gemini-3.1-pro-preview multicam crownshy#: 'unparseable answer: ```json\n{\n  "distinct_people": 28,\n  "busiest": "main kitchen line",\n  "service_in_progress": true,\n  "per_camera": {\n  '
- google/gemini-3.1-pro-preview single kitchen-stove#2: 'unparseable answer: {\n  "people": 1,\n  "area": "Kitchen prep and cooking area",\n  "activity": "A partially visible person is standing at the'
- google/gemini-3.1-pro-preview single kitchen-prep#3: 'unparseable answer: {\n "people": 1,\n "area": "prep and sink station",\n "activity": "A person is standing at a counter using a knife to chop '
- google/gemini-3.1-pro-preview temporal kitchen-stove#: 'unparseable answer: {\n  "what_changed": "A person started at the right counter opening a small box, briefly moved towards the top edge of th'
- google/gemini-3.1-pro-preview multicam kitchen#: 'unparseable answer: {\n  "distinct_people": 1,\n  "busiest": "Stove and prep counter",\n  "service_in_progress": false,\n  "per_camera": {\n    "'
- google/gemini-3.1-pro-preview temporal kitchen-prep#: 'unparseable answer: {\n  "what_changed": "A person stood in the background near the cabinets in the first two frames before moving to the cou'
- google/gemini-3.1-pro-preview temporal kitchen-sink#: 'unparseable answer: {\n  "what_changed": "In the first frame, a person stands at the foreground counter opening a blue box, but by the second'