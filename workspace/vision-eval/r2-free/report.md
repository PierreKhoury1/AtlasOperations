# Multi-camera restaurant eval: vision models on real feeds

Run: r2-free. Sites: crownshy, kitchen. Judge: `anthropic/claude-sonnet-5`. Frames per camera: 3. Single-frame calls per model: 36.

Scores: count_exact = % of frames where the people count fell inside the human range; count_near = within one of it; hazard_recall = % of human-flagged issues the model mentioned; area_ok = judge says the station was identified; judge = 0-5 accuracy vs the human description; halluc = invented details per answer (judge-counted); issues/frame = how many issues the model raised (over-flagging shows here). multicam = one call with every camera of a site. temporal = three frames of one camera over time (kitchen site).

| model | fail | count exact | count near | hazard recall | area ok | judge | halluc | issues/frame | multicam judge | service ok | busiest ok | temporal judge | temporal counts | p50 s | max s | cost $ |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free | 0/43 | 97% | 100% | 56% | 97% | 3.39 | 1.17 | 0.42 | 3.5 | 2/2 | 2/2 | 2.8 | 15/15 | 2.2 | 59.1 | 0 |
| qwen/qwen3.8-27b:free | 28/43 | 64% | 86% | 100% | 93% | 3.43 | 2 | 1.07 | None | 0/2 | 0/2 | 3 | 3/3 | 18.1 | 42.4 | 0 |
| google/gemma-4-31b-it:free | 36/43 | 100% | 100% | 50% | 100% | 3.25 | 1.75 | 1.75 | None | 0/2 | 0/2 | 3 | 9/9 | 14.5 | 20.1 | 0 |

Judge cost: $0.1705. Total model cost: $0.0000.

## Judge accuracy per camera (mean 0-5)

| model | crownshy-dining | crownshy-grill | crownshy-line | crownshy-plancha | crownshy-plating | crownshy-prep | crownshy-salamander | kitchen-overview | kitchen-prep | kitchen-sink | kitchen-stove | kitchen-wide |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free | 3.67 | 3.33 | 4 | 2.67 | 3.67 | 3.33 | 3.33 | 3 | 3 | 3.33 | 3.33 | 4 |
| qwen/qwen3.8-27b:free | 3.5 | 4 | 3 | 4 | 3 | - | 3 | - | 4 | 3.5 | 4 | 2 |
| google/gemma-4-31b-it:free | - | - | - | - | - | - | - | 4 | 3 | - | - | 3 |

## Multi-camera summaries (verbatim)

**nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free** on crownshy (judge 4, busiest: 'open kitchen pass', service: True, people: 15): The restaurant is in full evening service with active cooking and plating operations. The open kitchen allows diners to observe food preparation while staff work efficiently across multiple stations. Multiple dishes are being prepared simultaneously, indicating a high volume of service.

**nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free** on kitchen (judge 3, busiest: 'kitchen-prep', service: False, people: 1): The kitchen is occupied by a single person preparing food. No customers are visible, indicating service is not currently in progress. The environment appears organized with ingredients and tools ready for use.

**qwen/qwen3.8-27b:free** on crownshy (judge None, busiest: None, service: None, people: None): HTTP 429: {"error":{"message":"Provider returned error","code":429,"metadata":{"raw":"qwen/qwen3.8-27b:free is temporarily rate-limited upstream. Please retry shortly, or

**qwen/qwen3.8-27b:free** on kitchen (judge None, busiest: None, service: None, people: None): HTTP 429: {"error":{"message":"Provider returned error","code":429,"metadata":{"raw":"qwen/qwen3.8-27b:free is temporarily rate-limited upstream. Please retry shortly, or

**google/gemma-4-31b-it:free** on crownshy (judge None, busiest: None, service: None, people: None): HTTP 429: {"error":{"message":"Provider returned error","code":429,"metadata":{"raw":"google/gemma-4-31b-it:free is temporarily rate-limited upstream. Please retry shortl

**google/gemma-4-31b-it:free** on kitchen (judge None, busiest: None, service: None, people: None): HTTP 429: {"error":{"message":"Provider returned error","code":429,"metadata":{"raw":"google/gemma-4-31b-it:free is temporarily rate-limited upstream. Please retry shortl

## Hallucinations the judge flagged (sample)

- **nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free** (52): crownshy-line: exact count of 8 people; crownshy-line: open flame visible under pan on stove; crownshy-prep: Right person handling green vegetables (human says peeling carrots); crownshy-line: chef holding a cloth; crownshy-line: bare hand visible near a pan on the stove; crownshy-dining: diners described as seated at tables, not counter seating
- **qwen/qwen3.8-27b:free** (29): crownshy-dining: Claims 13 people, far exceeding the described 8-9; kitchen-stove: Describes a pot of water on cooktop, but pan stays empty; crownshy-line: 13 people (human describes only a few staff); crownshy-line: Knife left out on pass/clipboard; crownshy-line: 9 people count unverified/likely too specific; crownshy-line: Knife left out on pass counter
- **google/gemma-4-31b-it:free** (11): kitchen-overview: open-toed shoes; kitchen-overview: lack of hair restraint for long hair; kitchen-wide: knife left out on counter; kitchen-wide: person at left counter (position differs from near counter below camera); kitchen-prep: chopping activity (human says knife moved but arm holding pan, not chopping); kitchen-prep: eggplant and zucchini

## Failures

- qwen/qwen3.8-27b:free single crownshy-line#1: 'HTTP 429: {"error":{"message":"Provider returned error","code":429,"metadata":{"raw":"qwen/qwen3.8-27b:free is temporarily rate-limited upstream. Please retry shortly, or'
- qwen/qwen3.8-27b:free single crownshy-dining#2: 'HTTP 429: {"error":{"message":"Provider returned error","code":429,"metadata":{"raw":"qwen/qwen3.8-27b:free is temporarily rate-limited upstream. Please retry shortly, or'
- qwen/qwen3.8-27b:free single crownshy-prep#1: 'HTTP 429: {"error":{"message":"Provider returned error","code":429,"metadata":{"raw":"qwen/qwen3.8-27b:free is temporarily rate-limited upstream. Please retry shortly, or'
- qwen/qwen3.8-27b:free single crownshy-prep#2: 'HTTP 429: {"error":{"message":"Provider returned error","code":429,"metadata":{"raw":"qwen/qwen3.8-27b:free is temporarily rate-limited upstream. Please retry shortly, or'
- qwen/qwen3.8-27b:free single crownshy-prep#3: 'HTTP 429: {"error":{"message":"Provider returned error","code":429,"metadata":{"raw":"qwen/qwen3.8-27b:free is temporarily rate-limited upstream. Please retry shortly, or'
- qwen/qwen3.8-27b:free single crownshy-grill#2: 'HTTP 429: {"error":{"message":"Provider returned error","code":429,"metadata":{"raw":"qwen/qwen3.8-27b:free is temporarily rate-limited upstream. Please retry shortly, or'
- qwen/qwen3.8-27b:free single crownshy-plancha#1: 'HTTP 429: {"error":{"message":"Provider returned error","code":429,"metadata":{"raw":"qwen/qwen3.8-27b:free is temporarily rate-limited upstream. Please retry shortly, or'
- qwen/qwen3.8-27b:free single crownshy-plancha#2: 'HTTP 429: {"error":{"message":"Provider returned error","code":429,"metadata":{"raw":"qwen/qwen3.8-27b:free is temporarily rate-limited upstream. Please retry shortly, or'
- qwen/qwen3.8-27b:free single crownshy-salamander#1: 'HTTP 429: {"error":{"message":"Provider returned error","code":429,"metadata":{"raw":"qwen/qwen3.8-27b:free is temporarily rate-limited upstream. Please retry shortly, or'
- qwen/qwen3.8-27b:free single crownshy-salamander#2: 'HTTP 429: {"error":{"message":"Provider returned error","code":429,"metadata":{"raw":"qwen/qwen3.8-27b:free is temporarily rate-limited upstream. Please retry shortly, or'
- qwen/qwen3.8-27b:free single crownshy-plating#1: 'HTTP 429: {"error":{"message":"Provider returned error","code":429,"metadata":{"raw":"qwen/qwen3.8-27b:free is temporarily rate-limited upstream. Please retry shortly, or'
- qwen/qwen3.8-27b:free single crownshy-plating#2: 'HTTP 429: {"error":{"message":"Provider returned error","code":429,"metadata":{"raw":"qwen/qwen3.8-27b:free is temporarily rate-limited upstream. Please retry shortly, or'
- qwen/qwen3.8-27b:free multicam crownshy#: 'HTTP 429: {"error":{"message":"Provider returned error","code":429,"metadata":{"raw":"qwen/qwen3.8-27b:free is temporarily rate-limited upstream. Please retry shortly, or'
- qwen/qwen3.8-27b:free single kitchen-overview#1: 'HTTP 429: {"error":{"message":"Provider returned error","code":429,"metadata":{"raw":"qwen/qwen3.8-27b:free is temporarily rate-limited upstream. Please retry shortly, or'
- qwen/qwen3.8-27b:free single kitchen-overview#2: 'HTTP 429: {"error":{"message":"Provider returned error","code":429,"metadata":{"raw":"qwen/qwen3.8-27b:free is temporarily rate-limited upstream. Please retry shortly, or'
- qwen/qwen3.8-27b:free single kitchen-overview#3: 'HTTP 429: {"error":{"message":"Provider returned error","code":429,"metadata":{"raw":"qwen/qwen3.8-27b:free is temporarily rate-limited upstream. Please retry shortly, or'
- qwen/qwen3.8-27b:free single kitchen-wide#1: 'HTTP 429: {"error":{"message":"Provider returned error","code":429,"metadata":{"raw":"qwen/qwen3.8-27b:free is temporarily rate-limited upstream. Please retry shortly, or'
- qwen/qwen3.8-27b:free single kitchen-wide#3: 'HTTP 429: {"error":{"message":"Provider returned error","code":429,"metadata":{"raw":"qwen/qwen3.8-27b:free is temporarily rate-limited upstream. Please retry shortly, or'
- qwen/qwen3.8-27b:free single kitchen-stove#1: 'HTTP 429: {"error":{"message":"Provider returned error","code":429,"metadata":{"raw":"qwen/qwen3.8-27b:free is temporarily rate-limited upstream. Please retry shortly, or'
- qwen/qwen3.8-27b:free single kitchen-stove#2: 'HTTP 429: {"error":{"message":"Provider returned error","code":429,"metadata":{"raw":"qwen/qwen3.8-27b:free is temporarily rate-limited upstream. Please retry shortly, or'
- qwen/qwen3.8-27b:free single kitchen-prep#1: 'HTTP 429: {"error":{"message":"Provider returned error","code":429,"metadata":{"raw":"qwen/qwen3.8-27b:free is temporarily rate-limited upstream. Please retry shortly, or'
- qwen/qwen3.8-27b:free single kitchen-prep#3: 'HTTP 429: {"error":{"message":"Provider returned error","code":429,"metadata":{"raw":"qwen/qwen3.8-27b:free is temporarily rate-limited upstream. Please retry shortly, or'
- qwen/qwen3.8-27b:free single kitchen-sink#3: 'HTTP 429: {"error":{"message":"Provider returned error","code":429,"metadata":{"raw":"qwen/qwen3.8-27b:free is temporarily rate-limited upstream. Please retry shortly, or'
- qwen/qwen3.8-27b:free multicam kitchen#: 'HTTP 429: {"error":{"message":"Provider returned error","code":429,"metadata":{"raw":"qwen/qwen3.8-27b:free is temporarily rate-limited upstream. Please retry shortly, or'
- qwen/qwen3.8-27b:free temporal kitchen-overview#: 'HTTP 429: {"error":{"message":"Provider returned error","code":429,"metadata":{"raw":"qwen/qwen3.8-27b:free is temporarily rate-limited upstream. Please retry shortly, or'
- qwen/qwen3.8-27b:free temporal kitchen-wide#: 'HTTP 429: {"error":{"message":"Provider returned error","code":429,"metadata":{"raw":"qwen/qwen3.8-27b:free is temporarily rate-limited upstream. Please retry shortly, or'
- qwen/qwen3.8-27b:free temporal kitchen-prep#: 'HTTP 429: {"error":{"message":"Provider returned error","code":429,"metadata":{"raw":"qwen/qwen3.8-27b:free is temporarily rate-limited upstream. Please retry shortly, or'
- qwen/qwen3.8-27b:free temporal kitchen-sink#: 'HTTP 429: {"error":{"message":"Provider returned error","code":429,"metadata":{"raw":"qwen/qwen3.8-27b:free is temporarily rate-limited upstream. Please retry shortly, or'
- google/gemma-4-31b-it:free single crownshy-line#1: 'HTTP 429: {"error":{"message":"Provider returned error","code":429,"metadata":{"raw":"google/gemma-4-31b-it:free is temporarily rate-limited upstream. Please retry shortl'
- google/gemma-4-31b-it:free single crownshy-line#2: 'HTTP 429: {"error":{"message":"Provider returned error","code":429,"metadata":{"raw":"google/gemma-4-31b-it:free is temporarily rate-limited upstream. Please retry shortl'
- google/gemma-4-31b-it:free single crownshy-line#3: 'HTTP 429: {"error":{"message":"Provider returned error","code":429,"metadata":{"raw":"google/gemma-4-31b-it:free is temporarily rate-limited upstream. Please retry shortl'
- google/gemma-4-31b-it:free single crownshy-dining#1: 'HTTP 429: {"error":{"message":"Provider returned error","code":429,"metadata":{"raw":"google/gemma-4-31b-it:free is temporarily rate-limited upstream. Please retry shortl'
- google/gemma-4-31b-it:free single crownshy-dining#2: 'HTTP 429: {"error":{"message":"Provider returned error","code":429,"metadata":{"raw":"google/gemma-4-31b-it:free is temporarily rate-limited upstream. Please retry shortl'
- google/gemma-4-31b-it:free single crownshy-dining#3: 'HTTP 429: {"error":{"message":"Provider returned error","code":429,"metadata":{"raw":"google/gemma-4-31b-it:free is temporarily rate-limited upstream. Please retry shortl'
- google/gemma-4-31b-it:free single crownshy-prep#1: 'HTTP 429: {"error":{"message":"Provider returned error","code":429,"metadata":{"raw":"google/gemma-4-31b-it:free is temporarily rate-limited upstream. Please retry shortl'
- google/gemma-4-31b-it:free single crownshy-prep#2: 'HTTP 429: {"error":{"message":"Provider returned error","code":429,"metadata":{"raw":"google/gemma-4-31b-it:free is temporarily rate-limited upstream. Please retry shortl'
- google/gemma-4-31b-it:free single crownshy-prep#3: 'HTTP 429: {"error":{"message":"Provider returned error","code":429,"metadata":{"raw":"google/gemma-4-31b-it:free is temporarily rate-limited upstream. Please retry shortl'
- google/gemma-4-31b-it:free single crownshy-grill#1: 'HTTP 429: {"error":{"message":"Provider returned error","code":429,"metadata":{"raw":"google/gemma-4-31b-it:free is temporarily rate-limited upstream. Please retry shortl'
- google/gemma-4-31b-it:free single crownshy-grill#2: 'HTTP 429: {"error":{"message":"Provider returned error","code":429,"metadata":{"raw":"google/gemma-4-31b-it:free is temporarily rate-limited upstream. Please retry shortl'
- google/gemma-4-31b-it:free single crownshy-grill#3: 'HTTP 429: {"error":{"message":"Provider returned error","code":429,"metadata":{"raw":"google/gemma-4-31b-it:free is temporarily rate-limited upstream. Please retry shortl'
- google/gemma-4-31b-it:free single crownshy-plancha#1: 'HTTP 429: {"error":{"message":"Provider returned error","code":429,"metadata":{"raw":"google/gemma-4-31b-it:free is temporarily rate-limited upstream. Please retry shortl'
- google/gemma-4-31b-it:free single crownshy-plancha#2: 'HTTP 429: {"error":{"message":"Provider returned error","code":429,"metadata":{"raw":"google/gemma-4-31b-it:free is temporarily rate-limited upstream. Please retry shortl'
- google/gemma-4-31b-it:free single crownshy-plancha#3: 'HTTP 429: {"error":{"message":"Provider returned error","code":429,"metadata":{"raw":"google/gemma-4-31b-it:free is temporarily rate-limited upstream. Please retry shortl'
- google/gemma-4-31b-it:free single crownshy-salamander#1: 'HTTP 429: {"error":{"message":"Provider returned error","code":429,"metadata":{"raw":"google/gemma-4-31b-it:free is temporarily rate-limited upstream. Please retry shortl'
- google/gemma-4-31b-it:free single crownshy-salamander#2: 'HTTP 429: {"error":{"message":"Provider returned error","code":429,"metadata":{"raw":"google/gemma-4-31b-it:free is temporarily rate-limited upstream. Please retry shortl'
- google/gemma-4-31b-it:free single crownshy-salamander#3: 'HTTP 429: {"error":{"message":"Provider returned error","code":429,"metadata":{"raw":"google/gemma-4-31b-it:free is temporarily rate-limited upstream. Please retry shortl'
- google/gemma-4-31b-it:free single crownshy-plating#1: 'HTTP 429: {"error":{"message":"Provider returned error","code":429,"metadata":{"raw":"google/gemma-4-31b-it:free is temporarily rate-limited upstream. Please retry shortl'
- google/gemma-4-31b-it:free single crownshy-plating#2: 'HTTP 429: {"error":{"message":"Provider returned error","code":429,"metadata":{"raw":"google/gemma-4-31b-it:free is temporarily rate-limited upstream. Please retry shortl'
- google/gemma-4-31b-it:free single crownshy-plating#3: 'HTTP 429: {"error":{"message":"Provider returned error","code":429,"metadata":{"raw":"google/gemma-4-31b-it:free is temporarily rate-limited upstream. Please retry shortl'
- google/gemma-4-31b-it:free multicam crownshy#: 'HTTP 429: {"error":{"message":"Provider returned error","code":429,"metadata":{"raw":"google/gemma-4-31b-it:free is temporarily rate-limited upstream. Please retry shortl'
- google/gemma-4-31b-it:free single kitchen-overview#1: 'HTTP 429: {"error":{"message":"Provider returned error","code":429,"metadata":{"raw":"google/gemma-4-31b-it:free is temporarily rate-limited upstream. Please retry shortl'
- google/gemma-4-31b-it:free single kitchen-overview#3: 'HTTP 429: {"error":{"message":"Provider returned error","code":429,"metadata":{"raw":"google/gemma-4-31b-it:free is temporarily rate-limited upstream. Please retry shortl'
- google/gemma-4-31b-it:free single kitchen-wide#1: 'HTTP 429: {"error":{"message":"Provider returned error","code":429,"metadata":{"raw":"google/gemma-4-31b-it:free is temporarily rate-limited upstream. Please retry shortl'
- google/gemma-4-31b-it:free single kitchen-wide#3: 'HTTP 429: {"error":{"message":"Provider returned error","code":429,"metadata":{"raw":"google/gemma-4-31b-it:free is temporarily rate-limited upstream. Please retry shortl'
- google/gemma-4-31b-it:free single kitchen-stove#1: 'HTTP 429: {"error":{"message":"Provider returned error","code":429,"metadata":{"raw":"google/gemma-4-31b-it:free is temporarily rate-limited upstream. Please retry shortl'
- google/gemma-4-31b-it:free single kitchen-stove#2: 'HTTP 429: {"error":{"message":"Provider returned error","code":429,"metadata":{"raw":"google/gemma-4-31b-it:free is temporarily rate-limited upstream. Please retry shortl'
- google/gemma-4-31b-it:free single kitchen-stove#3: 'HTTP 429: {"error":{"message":"Provider returned error","code":429,"metadata":{"raw":"google/gemma-4-31b-it:free is temporarily rate-limited upstream. Please retry shortl'
- google/gemma-4-31b-it:free single kitchen-prep#1: 'HTTP 429: {"error":{"message":"Provider returned error","code":429,"metadata":{"raw":"google/gemma-4-31b-it:free is temporarily rate-limited upstream. Please retry shortl'
- google/gemma-4-31b-it:free single kitchen-sink#1: 'HTTP 429: {"error":{"message":"Provider returned error","code":429,"metadata":{"raw":"google/gemma-4-31b-it:free is temporarily rate-limited upstream. Please retry shortl'
- google/gemma-4-31b-it:free single kitchen-sink#2: 'HTTP 429: {"error":{"message":"Provider returned error","code":429,"metadata":{"raw":"google/gemma-4-31b-it:free is temporarily rate-limited upstream. Please retry shortl'
- google/gemma-4-31b-it:free single kitchen-sink#3: 'HTTP 429: {"error":{"message":"Provider returned error","code":429,"metadata":{"raw":"google/gemma-4-31b-it:free is temporarily rate-limited upstream. Please retry shortl'
- google/gemma-4-31b-it:free multicam kitchen#: 'HTTP 429: {"error":{"message":"Provider returned error","code":429,"metadata":{"raw":"google/gemma-4-31b-it:free is temporarily rate-limited upstream. Please retry shortl'
- google/gemma-4-31b-it:free temporal kitchen-stove#: 'HTTP 429: {"error":{"message":"Provider returned error","code":429,"metadata":{"raw":"google/gemma-4-31b-it:free is temporarily rate-limited upstream. Please retry shortl'
- google/gemma-4-31b-it:free temporal kitchen-prep#: 'HTTP 429: {"error":{"message":"Provider returned error","code":429,"metadata":{"raw":"google/gemma-4-31b-it:free is temporarily rate-limited upstream. Please retry shortl'