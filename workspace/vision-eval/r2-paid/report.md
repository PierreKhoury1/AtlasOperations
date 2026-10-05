# Multi-camera restaurant eval: vision models on real feeds

Run: r2-paid. Sites: crownshy, kitchen. Judge: `anthropic/claude-sonnet-5`. Frames per camera: 3. Single-frame calls per model: 36.

Scores: count_exact = % of frames where the people count fell inside the human range; count_near = within one of it; hazard_recall = % of human-flagged issues the model mentioned; area_ok = judge says the station was identified; judge = 0-5 accuracy vs the human description; halluc = invented details per answer (judge-counted); issues/frame = how many issues the model raised (over-flagging shows here). multicam = one call with every camera of a site. temporal = three frames of one camera over time (kitchen site).

| model | fail | count exact | count near | hazard recall | area ok | judge | halluc | issues/frame | multicam judge | service ok | busiest ok | temporal judge | temporal counts | p50 s | max s | cost $ |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| z-ai/glm-5.3-flash | 0/43 | 92% | 94% | 75% | 100% | 3.28 | 2.47 | 1.83 | 3 | 1/2 | 2/2 | 2.8 | 15/15 | 11.6 | 70.3 | 0.032 |
| google/gemini-3.1-flash-lite | 0/43 | 92% | 97% | 62% | 100% | 3.58 | 1.14 | 0.47 | 2.5 | 2/2 | 2/2 | 2.4 | 15/15 | 1.8 | 5.1 | 0.0246 |
| qwen/qwen3.6-flash | 6/43 | 97% | 100% | 81% | 97% | 3.23 | 2.17 | 1.4 | 3 | 1/2 | 2/2 | 3 | 15/15 | 40.8 | 95.9 | 0.1108 |
| google/gemini-3.8-flash | 0/43 | 81% | 94% | 69% | 100% | 3.19 | 2 | 1.19 | 3 | 2/2 | 2/2 | 2.6 | 15/15 | 5.9 | 16.0 | 0.1396 |
| anthropic/claude-haiku-4.5 | 0/43 | 92% | 94% | 50% | 89% | 3.17 | 1.78 | 1.14 | 3 | 1/2 | 2/2 | 2.4 | 15/15 | 3.3 | 9.0 | 0.1045 |
| openai/gpt-5.4-mini | 0/43 | 72% | 89% | 69% | 97% | 3.22 | 1.78 | 1.47 | 3 | 1/2 | 2/2 | 3 | 15/15 | 2.3 | 5.3 | 0.0932 |
| anthropic/claude-sonnet-5 | 0/43 | 81% | 92% | 75% | 97% | 3.14 | 2.75 | 2.53 | 3 | 1/2 | 2/2 | 3 | 15/15 | 4.7 | 10.5 | 0.3266 |
| google/gemini-3.1-pro-preview | 1/43 | 75% | 89% | 81% | 94% | 3.17 | 2 | 1.06 | 3 | 1/2 | 0/2 | 2.8 | 15/15 | 8.8 | 12.7 | 0.4846 |

Judge cost: $0.9278. Total model cost: $1.3159.

## Judge accuracy per camera (mean 0-5)

| model | crownshy-dining | crownshy-grill | crownshy-line | crownshy-plancha | crownshy-plating | crownshy-prep | crownshy-salamander | kitchen-overview | kitchen-prep | kitchen-sink | kitchen-stove | kitchen-wide |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| z-ai/glm-5.3-flash | 3.67 | 3.33 | 3.33 | 3.33 | 3.67 | 3 | 4 | 2.67 | 3 | 2.67 | 3.33 | 3.33 |
| google/gemini-3.1-flash-lite | 3.67 | 4.33 | 3.33 | 3.67 | 4 | 4 | 4 | 3.33 | 2.67 | 2.67 | 4.33 | 3 |
| qwen/qwen3.6-flash | 4 | 5 | 3.67 | 3.33 | 3.33 | 3.33 | - | 2.67 | 2.33 | 2 | 3.33 | 3 |
| google/gemini-3.8-flash | 3.33 | 4.33 | 3 | 3.67 | 4 | 3.33 | 3 | 3 | 2.33 | 2 | 3.67 | 2.67 |
| anthropic/claude-haiku-4.5 | 3.67 | 3.67 | 3 | 2.67 | 3.67 | 3 | 3.67 | 3 | 3 | 2 | 3.67 | 3 |
| openai/gpt-5.4-mini | 3.33 | 4.33 | 3 | 2.33 | 3 | 3.67 | 2.33 | 3.33 | 3.33 | 2.67 | 4 | 3.33 |
| anthropic/claude-sonnet-5 | 3.33 | 3.67 | 3 | 3.33 | 3 | 3.67 | 2.67 | 3 | 2.67 | 3 | 3.33 | 3 |
| google/gemini-3.1-pro-preview | 3 | 4.33 | 3.33 | 3 | 3.33 | 3.67 | 3.33 | 2.33 | 3.67 | 1.67 | 3.67 | 2.67 |

## Multi-camera summaries (verbatim)

**z-ai/glm-5.3-flash** on crownshy (judge 4, busiest: 'Hot line/plating pass', service: True, people: 20): Evening service is in full swing: the dining room is largely seated and the open kitchen is staffed across every station. The grill, plancha, salamander and plating pass are all simultaneously active, with several finished dishes being plated at once. Prep is running in parallel with three cooks working vegetables for later service.

**z-ai/glm-5.3-flash** on kitchen (judge 2, busiest: 'main prep counter (grocery unpacking and ingredient staging beside the induction hob)', service: True, people: 1): A single cook is the only person present and is in the early stage of meal preparation, unpacking groceries and staging ingredients at the main counter. Vegetables, a knife and utensils are laid out by the prep sink, while an empty pot already sits on the induction hob awaiting use. The kitchen is otherwise tidy and calm, with no customers or other staff visible in any camera.

**google/gemini-3.1-flash-lite** on crownshy (judge 3, busiest: 'plating', service: True, people: 16): The restaurant is in the middle of a busy evening service with all stations fully operational. The kitchen team is coordinating between prep, cooking, and final plating to serve guests seated at the counter. Workflow appears efficient and synchronized across all areas.

**google/gemini-3.1-flash-lite** on kitchen (judge 2, busiest: 'prep area', service: False, people: 1): A single individual is currently working in the kitchen, primarily focused on reading a document at the prep station. The kitchen is in a state of preparation with ingredients laid out and a pot on the stove, but no active cooking or service is occurring. The environment appears orderly and quiet.

**google/gemini-3.8-flash** on crownshy (judge 3, busiest: 'plating pass', service: True, people: 20): Evening dinner service is fully underway with multiple tables seated in the dining room. The kitchen line, grill, and plancha are actively cooking proteins while finished dishes are being assembled at the pass. Prep staff in the back station continue processing vegetables to support service.

**google/gemini-3.8-flash** on kitchen (judge 3, busiest: 'main prep counter', service: False, people: 1): A single cook is preparing to begin a meal in an institutional kitchen environment. Raw vegetables, pasta, and cooking supplies are laid out across the prep surfaces and sink area. No active cooking has started yet while the cook consults paper instructions.

**anthropic/claude-haiku-4.5** on crownshy (judge 3, busiest: 'crownshy-line (main kitchen pass)', service: True, people: 18): The restaurant is in full evening service with approximately 18 distinct staff and customers across all areas. The open kitchen is operating at high capacity with active cooking, plating, and service occurring simultaneously across all stations. Dining room is occupied with customers being actively served while kitchen maintains coordinated workflow.

**anthropic/claude-haiku-4.5** on kitchen (judge 3, busiest: 'prep station', service: True, people: 1): One cook is actively preparing a meal in this institutional kitchen. Fresh vegetables are laid out on prep surfaces and a pot is heating on the stove, indicating active cooking in progress. The kitchen appears organized with ingredients and tools positioned for efficient meal preparation.

**openai/gpt-5.4-mini** on crownshy (judge 4, busiest: 'plating station', service: True, people: 19): The restaurant is in active evening service with both kitchen and dining room occupied. The open kitchen is busy across prep, grill, plancha, salamander, and plating stations. Several dishes are being finished at the pass while guests are seated nearby.

**openai/gpt-5.4-mini** on kitchen (judge 2, busiest: 'prep counter by the sink', service: True, people: 1): A single cook is working in the kitchen and appears to be preparing food. The prep surfaces are occupied with vegetables, utensils, and containers, while the stove is not actively in use. The kitchen is active and in the middle of meal preparation.

**anthropic/claude-sonnet-5** on crownshy (judge 4, busiest: 'crownshy-line (pass/expo area with dense crowd of cooks)', service: True, people: 20): The restaurant is in full evening service with the dining room seated and multiple kitchen stations actively cooking and plating dishes. The line and pass area appear busiest, with several cooks converging near the expo station while prep, grill, plancha, and salamander stations each handle individual tasks. Overall the kitchen and dining room show a coordinated, high-tempo fine-dining service flow.

**anthropic/claude-sonnet-5** on kitchen (judge 2, busiest: 'kitchen-stove/prep counter area', service: True, people: 1): A single cook is actively preparing a meal in the small kitchen, working between the stove, prep counter, and sink areas. A pot of water is heating on the induction stove while raw vegetables and pasta are staged for cooking. The kitchen is orderly with no other staff or customers present, indicating an early prep stage rather than active dining service.

**qwen/qwen3.6-flash** on crownshy (judge 4, busiest: 'plating', service: True, people: 15): The restaurant is in full swing during evening service with active cooking and plating stations. Staff are busy preparing ingredients, grilling proteins, and assembling final dishes. The dining area is occupied with guests being attended to by waitstaff.

**qwen/qwen3.6-flash** on kitchen (judge 2, busiest: 'prep counter and stove area', service: True, people: 1): A single cook is actively working in a small kitchen space, surrounded by raw vegetables and cooking ingredients. A pot is heating on the stove while the worker organizes items at the main preparation counter. The kitchen appears organized but busy with various supplies spread across the workspace.

**google/gemini-3.1-pro-preview** on kitchen (judge None, busiest: None, service: None, people: None): {
  "distinct_people": 1,
  "busiest": "prep counter",
  "service_in_progress": false,
  "per_camera": {
    "kitchen-overview": "A person stands at the prep counter reading a document, with various ingredients and a pot on the stove nearby.",
    "kitchen-wide": "Shows the person from a different a

**google/gemini-3.1-pro-preview** on crownshy (judge 3, busiest: 'dining room', service: True, people: 25): The restaurant is in the middle of a busy evening service. The dining room is full of patrons, and the open kitchen is fully staffed and active. Chefs are engaged in various tasks across different stations, from prepping ingredients to grilling, cooking on the plancha, and meticulously plating finished dishes.

## Hallucinations the judge flagged (sample)

- **z-ai/glm-5.3-flash** (101): crownshy-line: Exact people count of 8 not confirmed by human; crownshy-line: Open gas burner flame unattended claim; crownshy-line: Beverage cups stored on counter; crownshy-line: Loose knives near counter edge; crownshy-dining: specific count of 9 people; crownshy-dining: server in a white shirt standing by
- **google/gemini-3.1-flash-lite** (57): crownshy-line: Counted 8 people, more than human's 4-5; crownshy-line: Claims a knife was left out; crownshy-line: exact count of 8 people not confirmed; crownshy-line: 10 people (likely overcounted staff); crownshy-line: knife left out (unverified detail); crownshy-dining: claims 11 people, more than the ~8 described
- **qwen/qwen3.6-flash** (78): crownshy-prep: No hair restraint - not mentioned or contradicted by human description; kitchen-overview: zucchini, eggplant, and yellow pepper (human saw cucumber and orange); kitchen-overview: several bottles near person; kitchen-overview: loose electrical cords hanging near power strip; kitchen-stove: Box described as blue, human said white; kitchen-stove: Trash bin positioned near walkway
- **google/gemini-3.8-flash** (89): crownshy-line: counted 9 people, exceeds human estimate of 4-5; crownshy-line: cook walking down aisle carrying a towel; crownshy-line: Specific count of 9 people; crownshy-line: Knife left resting improperly in a sink container; crownshy-dining: exact count of 10 people; crownshy-dining: front-of-house staff standing by to run orders
- **anthropic/claude-haiku-4.5** (78): crownshy-line: 12 people (human implies ~5-8); crownshy-line: knife visible on counter; crownshy-line: Exact count of 12 people not confirmed; crownshy-line: Knife visible on counter as safety hazard; crownshy-line: Count of 8 people (human indicates about 4-5 staff); crownshy-line: Knife visible on the left counter (not mentioned)
- **openai/gpt-5.4-mini** (75): crownshy-line: 10 people is likely too many (human implies ~5-6); crownshy-line: open flame under front-right pan not mentioned; crownshy-line: walkway blocked by staff member and equipment; crownshy-line: exact count of 8 people; crownshy-line: open flame visible under a pan; crownshy-dining: chef stands with his back to the camera
- **anthropic/claude-sonnet-5** (116): crownshy-line: Exact count of 9 people; crownshy-line: Grill present on the line; crownshy-line: Disposable cups and containers on counter; crownshy-line: 11 people (human suggests ~4-6 total); crownshy-line: staff gathered near back possibly during briefing; crownshy-line: knife/utensil resting on prep counter
- **google/gemini-3.1-pro-preview** (86): crownshy-dining: exact count of 10 people not confirmed by human description; crownshy-prep: green stalks; crownshy-prep: unattended knife framed as an issue; crownshy-grill: Chef using tongs (human describes pan work, not tongs); crownshy-grill: Chef wearing watch and bracelets on wrists; crownshy-plancha: 3 people total, two other kitchen staff at prep and plating stations

## Failures

- qwen/qwen3.6-flash single crownshy-dining#3: 'unparseable answer: The user wants a JSON report based on the provided image of a restaurant kitchen.\n\n**1. Analyze the Image:**\n*   **Peopl'
- qwen/qwen3.6-flash single crownshy-dining#2: 'unparseable answer: ```json\n{\n  "people": 11,\n  "area": "Open kitchen pass and counter seating area",\n  "activity": "Kitchen staff are busy '
- qwen/qwen3.6-flash single crownshy-salamander#1: 'unparseable answer: ```json\n'
- qwen/qwen3.6-flash single crownshy-salamander#2: 'unparseable answer: The user wants a JSON report based on the provided image of a restaurant kitchen.\n\n**1. Analyze the image:**\n*   **Peopl'
- qwen/qwen3.6-flash single crownshy-salamander#3: 'unparseable answer: The user wants a JSON report based on the provided image of a restaurant kitchen.\n\n**1. Analyze the image:**\n*   **Peopl'
- qwen/qwen3.6-flash single kitchen-wide#3: 'unparseable answer: The user wants a JSON report based on the provided image of a kitchen.\n\n**1. Analyze the Image:**\n*   **People:** I see '
- google/gemini-3.1-pro-preview multicam kitchen#: 'unparseable answer: {\n  "distinct_people": 1,\n  "busiest": "prep counter",\n  "service_in_progress": false,\n  "per_camera": {\n    "kitchen-ov'