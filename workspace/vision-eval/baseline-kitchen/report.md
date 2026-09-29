# Multi-camera restaurant eval: vision models on real feeds

Run: baseline-kitchen. Sites: kitchen. Judge: `anthropic/claude-sonnet-5`. Frames per camera: 3. Single-frame calls per model: 15.

Scores: count_exact = % of frames where the people count fell inside the human range; count_near = within one of it; hazard_recall = % of human-flagged issues the model mentioned; area_ok = judge says the station was identified; judge = 0-5 accuracy vs the human description; halluc = invented details per answer (judge-counted); issues/frame = how many issues the model raised (over-flagging shows here). multicam = one call with every camera of a site. temporal = three frames of one camera over time (kitchen site).

| model | fail | count exact | count near | hazard recall | area ok | judge | halluc | issues/frame | multicam judge | service ok | busiest ok | temporal judge | temporal counts | p50 s | max s | cost $ |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| google/gemini-3.1-flash-lite | 0/21 | 100% | 100% | 100% | 93% | 3.2 | 1.53 | 0.4 | 2 | 1/1 | 1/1 | 3 | 15/15 | 2.1 | 3.2 | 0.0168 |
| nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free | 0/21 | 100% | 100% | 67% | 87% | 3.13 | 1.13 | 0.2 | 3 | 1/1 | 1/1 | 3 | 14/15 | 1.5 | 15.1 | 0 |

Judge cost: $0.0751. Total model cost: $0.0168.

## Judge accuracy per camera (mean 0-5)

| model | kitchen-overview | kitchen-prep | kitchen-sink | kitchen-stove | kitchen-wide |
|---|---|---|---|---|---|
| google/gemini-3.1-flash-lite | 3 | 3.33 | 2.67 | 4 | 3 |
| nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free | 3.33 | 3.33 | 2.67 | 3.33 | 3 |

## Multi-camera summaries (verbatim)

**google/gemini-3.1-flash-lite** on kitchen (judge 2, busiest: 'prep station', service: False, people: 1): The kitchen is currently in a quiet preparation phase with only one individual present. The person is reviewing documentation rather than actively cooking, though ingredients are staged on the counters. No active service to customers is occurring at this time.

**nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free** on kitchen (judge 3, busiest: 'kitchen-prep', service: False, people: 1): The kitchen is occupied by a single person preparing food at a counter. The space is organized with a sink, stove, and prep areas, but no customers are present. The environment appears clean and ready for service, though currently inactive.

## Hallucinations the judge flagged (sample)

- **google/gemini-3.1-flash-lite** (33): kitchen-overview: reading or consulting a document; kitchen-overview: small pot on a burner; kitchen-wide: cutting a vegetable; kitchen-wide: using a knife on a cutting board; kitchen-overview: person preparing/organizing food items near a refrigerator; kitchen-overview: slicing a zucchini with a knife
- **nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free** (26): kitchen-overview: refrigerator and bottles/containers on countertop; kitchen-overview: chopping vegetables on a cutting board; kitchen-overview: preparing ingredients/organizing items specifics; kitchen-wide: blue container near a pot on the stove; kitchen-wide: pot on a portable burner; kitchen-wide: preparing food

## Failures
