# Camera agents vs the truth: WILDTRACK

4 camera agents (C1, C2, C3, C6) each wrote a journal note at 6 shared instants, 4 s apart (the busiest stretch). Agent: `google/gemini-3.1-flash-lite` with close-up and verify pass, exactly as a live camera. Judge: `anthropic/claude-sonnet-5`, shown the camera's own frame plus the other 3 cameras at the same instant.

| camera | notes | claims | true in own frame | invented / wrong | unclear | wrong per note | other cameras agree | count error (labels) | detector count error | removed by verify pass |
|---|---|---|---|---|---|---|---|---|---|---|
| C1 | 6 | 53 | 81% | 8% | 11% | 0.67 | 96% of 26 | no total given | 12.5 | 15 |
| C2 | 6 | 56 | 84% | 2% | 14% | 0.17 | 100% of 23 | no total given | 15.0 | 12 |
| C3 | 6 | 43 | 79% | 5% | 16% | 0.33 | 100% of 22 | 3.5 (+3.5, 2 of 6 notes give a total) | 9.2 | 12 |
| C6 | 6 | 45 | 80% | 4% | 16% | 0.33 | 100% of 29 | 28.0 (+28.0, 1 of 6 notes give a total) | 19.5 | 13 |
| **all** | 24 | 197 | 81% | 5% | 14% | 0.38 | 99% of 100 | 11.7 (+11.7, 3 of 24 notes give a total) | 14.0 | 52 |

Count error compares the number of people the note states with the human labels for that camera. The labels only cover people standing on the labelled square, so people on the stairs or far away that the note rightly counts make it look high: read the bias with that in mind.

## What the agents got wrong

- **C1 @ 00000875**: "Other man wears an olive green jacket" (person): jacket appears dark, not olive
- **C1 @ 00000915**: "Man in grey jacket with bright yellow collar moving same direction behind her" (person): grey/yellow jacket is actually neon yellow sleeves, no yellow collar
- **C1 @ 00000995**: "No vehicles present" (scene): red truck visible in background
- **C1 @ 00001035**: "One man in grey hoodie carries a bag" (person): grey hoodie man holds backpack item, not a bag
- **C2 @ 00000995**: "Left person in dark jacket and blue jeans" (person): left person wears hoodie with jacket on arm, not jacket+jeans
- **C3 @ 00000915**: "Man in dark jacket with grey hood walking toward camera in foreground" (person): foreground men not wearing grey hood
- **C3 @ 00000995**: "Man in dark hooded jacket and dark trousers walking left, hands in pockets" (person): central man wears light coat, no hood, hands not in pockets
- **C6 @ 00000875**: "Group of five individuals standing in center of plaza" (group): no such group in center visible
- **C6 @ 00000915**: "Group of five in center of plaza remains in place" (group): No stationary group of five visible in plaza center

## Where another camera disagreed

- **C1 @ 00001075**: "No vehicles present": red vehicle visible in background of C1

Cost: agent $0.046 (estimate), judge $0.328. _Generated 2026-10-01 16:33. Data: WILDTRACK (Chavdarova et al., CVPR 2018), non-commercial research use. The judge is a model too: a second opinion, not ground truth._