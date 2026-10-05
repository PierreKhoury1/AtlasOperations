# Multi-camera restaurant eval: vision models on real feeds

Run: smoke. Sites: crownshy, kitchen. Judge: `anthropic/claude-sonnet-5`. Frames per camera: 1. Single-frame calls per model: 12.

Scores: count_exact = % of frames where the people count fell inside the human range; count_near = within one of it; hazard_recall = % of human-flagged issues the model mentioned; area_ok = judge says the station was identified; judge = 0-5 accuracy vs the human description; halluc = invented details per answer (judge-counted); issues/frame = how many issues the model raised (over-flagging shows here). multicam = one call with every camera of a site. temporal = three frames of one camera over time (kitchen site).

| model | fail | count exact | count near | hazard recall | area ok | judge | halluc | issues/frame | multicam judge | service ok | busiest ok | temporal judge | temporal counts | p50 s | max s | cost $ |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| google/gemini-3.1-flash-lite | 0/14 | 100% | 100% | 60% | 92% | 2.83 | 1.5 | 0.42 | 3 | 2/2 | 1/2 | None | - | 2.8 | 4.9 | 0.0093 |
| nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free | 0/14 | 92% | 100% | 60% | 83% | 2.64 | 1.82 | 0.58 | 4 | 2/2 | 1/2 | None | - | 2.3 | 10.0 | 0 |

Judge cost: $0.1160. Total model cost: $0.0093.

## Judge accuracy per camera (mean 0-5)

| model | crownshy-dining | crownshy-grill | crownshy-line | crownshy-plancha | crownshy-plating | crownshy-prep | crownshy-salamander | kitchen-overview | kitchen-prep | kitchen-sink | kitchen-stove | kitchen-wide |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| google/gemini-3.1-flash-lite | 4 | 4 | 2 | 3 | 3 | 3 | 2 | 2 | 3 | 2 | 4 | 2 |
| nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free | 3 | 3 | None | 3 | 3 | 3 | 2 | 3 | 2 | 1 | 3 | 3 |

## Multi-camera summaries (verbatim)

**google/gemini-3.1-flash-lite** on crownshy (judge 3, busiest: 'plating', service: True, people: 16): The restaurant is in the middle of a busy evening service with all kitchen stations fully operational. Staff are actively prepping, cooking, and plating dishes to meet customer demand. The kitchen environment appears organized and focused on maintaining high-volume output.

**google/gemini-3.1-flash-lite** on kitchen (judge None, busiest: 'kitchen-stove', service: False, people: 1): A single cook is currently engaged in food preparation within a small institutional kitchen. The individual is focused on tasks at the stove and counter area. No other staff or customers are present in the facility.

**nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free** on crownshy (judge 4, busiest: 'plating station', service: True, people: 15): The restaurant is in full operation with staff actively cooking, prepping, and plating dishes during evening service. The open kitchen allows diners to observe food preparation while being served at tables. Multiple stations are simultaneously active, indicating a high-volume, coordinated service.

**nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free** on kitchen (judge None, busiest: 'kitchen-prep', service: False, people: 1): The kitchen is currently being used for food preparation by a single individual. No customers are visible, indicating service is not currently in progress. The environment appears to be a functional, small-scale kitchen setup.

## Hallucinations the judge flagged (sample)

- **google/gemini-3.1-flash-lite** (18): crownshy-prep: celery being prepared; crownshy-dining: Specific headcount of 9 people not confirmed by human description; crownshy-line: 8 people (human indicates about 4-5); crownshy-line: one staff member walking through the center of the kitchen; crownshy-line: knife left out; crownshy-salamander: A chef is wiping down the counter with a cloth (activity not observed)
- **nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free** (20): crownshy-prep: person on the right handles green vegetables; crownshy-dining: Specific count of 8 people (not verifiable from human description); crownshy-grill: flare-up on grill; crownshy-grill: raw meat uncovered as a safety issue; crownshy-salamander: 3 people present; crownshy-salamander: another staff member partially visible on the right side

## Failures
