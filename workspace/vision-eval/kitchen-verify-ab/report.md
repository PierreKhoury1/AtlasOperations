# Multi-camera restaurant eval: vision models on real feeds

Run: kitchen-verify-ab. Sites: kitchen. Judge: `anthropic/claude-sonnet-5`. Frames per camera: 3. Single-frame calls per model: 15.

Scores: count_exact = % of frames where the people count fell inside the human range; count_near = within one of it; hazard_recall = % of human-flagged issues the model mentioned; area_ok = judge says the station was identified; judge = 0-5 accuracy vs the human description; halluc = invented details per answer (judge-counted); issues/frame = how many issues the model raised (over-flagging shows here). multicam = one call with every camera of a site. temporal = three frames of one camera over time (kitchen site).

| model | fail | count exact | count near | hazard recall | area ok | judge | halluc | issues/frame | multicam judge | service ok | busiest ok | temporal judge | temporal counts | p50 s | max s | cost $ |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| google/gemini-3.1-flash-lite | 0/21 | 100% | 100% | 100% | 93% | 3.2 | 1.53 | 0.4 | 2 | 1/1 | 1/1 | 3 | 15/15 | 2.1 | 3.2 | 0.0168 |
| google/gemini-3.1-flash-lite+verify | 0/21 | 100% | 100% | 100% | 100% | 3.6 | 0.87 | 0.4 | 3 | 1/1 | 1/1 | 2.8 | 15/15 | 3.3 | 6.0 | 0.0283 |
| nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free | 0/21 | 100% | 100% | 67% | 87% | 3.13 | 1.13 | 0.2 | 3 | 1/1 | 1/1 | 3 | 14/15 | 1.5 | 15.1 | 0 |
| nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free+verify | 0/21 | 100% | 100% | 100% | 100% | 3.33 | 0.87 | 0.27 | 4 | 1/1 | 1/1 | 3 | 15/15 | 3.9 | 12.5 | 0 |

Judge cost: $0.1474. Total model cost: $0.0451.

## Judge accuracy per camera (mean 0-5)

| model | kitchen-overview | kitchen-prep | kitchen-sink | kitchen-stove | kitchen-wide |
|---|---|---|---|---|---|
| google/gemini-3.1-flash-lite | 3 | 3.33 | 2.67 | 4 | 3 |
| google/gemini-3.1-flash-lite+verify | 3.33 | 3.33 | 3.33 | 4.33 | 3.67 |
| nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free | 3.33 | 3.33 | 2.67 | 3.33 | 3 |
| nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free+verify | 3.67 | 3.33 | 2.67 | 3.33 | 3.67 |

## Multi-camera summaries (verbatim)

**google/gemini-3.1-flash-lite** on kitchen (judge 2, busiest: 'prep station', service: False, people: 1): The kitchen is currently in a quiet preparation phase with only one individual present. The person is reviewing documentation rather than actively cooking, though ingredients are staged on the counters. No active service to customers is occurring at this time.

**nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free** on kitchen (judge 3, busiest: 'kitchen-prep', service: False, people: 1): The kitchen is occupied by a single person preparing food at a counter. The space is organized with a sink, stove, and prep areas, but no customers are present. The environment appears clean and ready for service, though currently inactive.

**google/gemini-3.1-flash-lite+verify** on kitchen (judge 3, busiest: 'prep counter', service: False, people: 1): A single staff member is actively preparing a meal in a small institutional kitchen. The workspace is organized with fresh produce and cooking equipment ready for use. The cook is currently reviewing instructions at the main counter.

**nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free+verify** on kitchen (judge 4, busiest: 'kitchen-prep', service: False, people: 1): A single person is preparing food in a small, clean kitchen with white cabinets and modern appliances. The kitchen is well-lit and organized, with various cooking ingredients and utensils visible. No customers are present, indicating the restaurant is not currently serving.

## Hallucinations the judge flagged (sample)

- **google/gemini-3.1-flash-lite** (33): kitchen-overview: reading or consulting a document; kitchen-overview: small pot on a burner; kitchen-wide: cutting a vegetable; kitchen-wide: using a knife on a cutting board; kitchen-overview: person preparing/organizing food items near a refrigerator; kitchen-overview: slicing a zucchini with a knife
- **google/gemini-3.1-flash-lite+verify** (25): kitchen-wide: knife left out on the counter; kitchen-wide: Uncovered food items on the counter; kitchen-overview: knife near a vegetable; kitchen-overview: eggplant and green vegetable on cutting board; kitchen-overview: pot on a burner; kitchen-stove: Loose plastic packaging on the counter
- **nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free** (26): kitchen-overview: refrigerator and bottles/containers on countertop; kitchen-overview: chopping vegetables on a cutting board; kitchen-overview: preparing ingredients/organizing items specifics; kitchen-wide: blue container near a pot on the stove; kitchen-wide: pot on a portable burner; kitchen-wide: preparing food
- **nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free+verify** (21): kitchen-wide: yellow vegetable detail; kitchen-wide: preparing food or organizing ingredients activity not confirmed; kitchen-wide: blue container color; kitchen-stove: opening a box and handling its contents (unconfirmed detail); kitchen-overview: chopping vegetables on a cutting board; kitchen-stove: pot of water on burner (human said pan is empty)

## Failures
