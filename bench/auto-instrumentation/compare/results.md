# Offline SDK comparison

**LAPTOP QUICK SMOKE — NOT A BENCHMARK. No 10x performance claim is supported.**

Measured calls/tool/scenario/repetition: 3; warmups: 1; repetitions: 6. Providers and collectors are loopback mocks. Adapters and tools run sequentially.

All six tool-order permutations are run per cycle in reverse-paired blocks. Each tool occupies each position twice and pairwise precedence is 3:3. Differences pair calls by index within the same repetition, then pool equal-N samples. Performance ratios are withheld unless the complete counterbalanced cycle and parity pass. Startup/RSS show medians across repetition processes.

| Runtime/scenario | Spans T/B | Semantic % T/B | Parity T/B | Added p50 us T/B | Added p99 us T/B | BT/T absolute p50 |
|---|---:|---:|---|---:|---:|---:|
| python/openai_chat | 24/24 | 85.71/100.00 | True/True | 837.42/195.87 | 1099.69/21059.39 | 0.68 |
| python/openai_stream | 24/24 | 75.00/87.50 | True/True | 1109.42/-1848.92 | 6630.84/5251.41 | 0.96 |
| python/anthropic_stream | 48/24 | 87.50/100.00 | True/True | -1225.02/-386.35 | 25132.01/8033.67 | 1.01 |
| python/gemini | 24/24 | 85.71/100.00 | True/True | 872.88/148.21 | 1906.21/272.44 | 0.47 |
| python/embeddings | 24/24 | 85.71/100.00 | True/True | 669.25/24.02 | 891.78/283.04 | 0.47 |
| python/tool_calls | 24/24 | 87.50/100.00 | True/True | 1299.85/178.88 | 1641.17/461.01 | 0.42 |
| python/transcription | 24/24 | 87.50/100.00 | True/True | 801.79/66.02 | 1143.13/431.97 | 0.56 |
| python/tts | 24/24 | 75.00/87.50 | True/True | 724.02/100.37 | 983.09/378.65 | 0.48 |
| python/langchain | 24/48 | 85.71/100.00 | True/True | 994.40/259.35 | 1281.84/589.14 | 0.57 |
| python/raw_http | 24/24 | 100.00/100.00 | True/True | 1043.48/57.75 | 1229.61/217.10 | 0.32 |
| python/unsupported_sdk | 24/24 | 85.71/100.00 | True/True | 1041.54/103.04 | 1432.51/362.38 | 0.47 |
| python/openai_error | 24/24 | 80.00/80.00 | True/True | 742.60/22.50 | 10231.81/20653.15 | 0.70 |
| node/openai_chat | 24/24 | 85.71/100.00 | True/True | 1588.35/222.54 | 2799.49/1570.45 | 0.68 |
| node/openai_stream | 24/24 | 75.00/87.50 | True/True | 1144.92/-191.38 | 5750.09/4364.31 | 0.98 |
| node/anthropic_stream | 24/24 | 75.00/87.50 | True/True | 1399.06/-565.35 | 4949.89/2226.62 | 0.97 |
| node/gemini | 24/24 | 85.71/85.71 | True/True | 1657.88/269.06 | 2726.80/961.72 | 0.63 |
| node/embeddings | 24/24 | 85.71/100.00 | True/True | 1756.06/175.10 | 2267.37/411.51 | 0.60 |
| node/tool_calls | 24/24 | 87.50/100.00 | True/True | 2368.56/353.71 | 3564.07/1453.12 | 0.58 |
| node/transcription | 24/24 | 87.50/100.00 | True/True | 1844.31/182.62 | 8069.39/1172.99 | 0.66 |
| node/tts | 24/24 | 75.00/75.00 | True/True | 1618.02/143.98 | 2441.93/507.01 | 0.65 |
| node/vercel | 24/48 | 85.71/85.71 | True/True | 1703.67/561.96 | 3282.20/5667.30 | 0.72 |
| node/raw_http | 24/24 | 100.00/100.00 | True/True | 1718.21/263.54 | 2349.25/2240.09 | 0.65 |
| node/unsupported_sdk | 24/24 | 85.71/100.00 | True/True | 1834.15/332.33 | 3268.06/891.19 | 0.65 |
| node/openai_error | 24/24 | 80.00/100.00 | True/True | 1098.52/149.33 | 3940.98/3048.87 | 0.71 |

## Streaming timing validation

Exported timing names OR numerical coincidence establish no semantic credit. Reviewed implementation trigger contracts and source hashes are recorded per language/run: body/first-SDK-item timers remain TTFC even if buffering coincides with text. Only a source-verified text-aware trigger may earn TTFT, and its span-relative seconds must also align to the independently observed first text within 3 ms (+1 ms for Node rounding), with first item/text separated by >=10 ms. A 30 ms mock pause exposes role/header events. Unknown source/units and ambiguous/out-of-window text timings abstain. The inspected Python Braintrust Anthropic trigger is text-aware and hash-pinned; traceAI body timers and Braintrust OpenAI/Node generic first-item timers are not. This validates this fixture, not all production streams.

| Runtime/scenario | Calls T/B (warmups included) | First chunk/item timing T/B | Semantic TTFT T/B | Unresolved T/B |
|---|---:|---:|---:|---:|
| python/openai_stream | 24/24 | 24/24 | 0/0 | 0/0 |
| python/anthropic_stream | 24/24 | 24/0 | 0/24 | 0/0 |
| node/openai_stream | 24/24 | 24/24 | 0/0 | 0/0 |
| node/anthropic_stream | 24/24 | 24/24 | 0/0 | 0/0 |

## Setup, startup, memory and privacy

T/B = traceAI / Braintrust. Costs include setup/shutdown policy below. Credential/media = exported payload canaries, not authorization headers.

| Runtime/scenario | App LOC T/B | Commands T/B | Startup ms T/B | Peak MiB T/B | Credentials T/B | Raw media T/B |
|---|---:|---:|---:|---:|---|---|
| python/openai_chat | 0/4 | 2/2 | 268.32/1278.87 | 69.65/180.21 | False/False | False/False |
| python/openai_stream | 0/4 | 2/2 | 275.93/1201.96 | 69.61/180.43 | False/False | False/False |
| python/anthropic_stream | 0/4 | 2/2 | 480.24/1189.87 | 93.09/182.51 | False/False | False/False |
| python/gemini | 0/4 | 2/2 | 250.09/1163.80 | 74.49/183.28 | False/False | False/False |
| python/embeddings | 0/4 | 2/2 | 263.94/1159.27 | 68.05/179.76 | False/False | False/False |
| python/tool_calls | 0/4 | 2/2 | 263.50/1154.05 | 69.55/180.21 | False/False | False/False |
| python/transcription | 0/4 | 2/2 | 266.50/1235.81 | 68.86/179.96 | False/False | False/True |
| python/tts | 0/4 | 2/2 | 281.16/1227.77 | 68.77/180.05 | False/False | False/True |
| python/langchain | 0/4 | 2/2 | 478.18/1251.07 | 94.92/190.86 | False/False | False/False |
| python/raw_http | 0/5 | 2/2 | 157.31/1269.30 | 52.96/181.52 | False/False | False/False |
| python/unsupported_sdk | 0/5 | 2/2 | 257.93/1488.59 | 70.73/194.76 | False/False | False/False |
| python/openai_error | 0/4 | 2/2 | 613.28/3179.32 | 69.35/179.75 | False/False | False/False |
| node/openai_chat | 0/3 | 2/2 | 260.47/341.55 | 126.66/140.90 | False/False | False/False |
| node/openai_stream | 0/3 | 2/2 | 182.53/159.72 | 133.03/143.32 | False/False | False/False |
| node/anthropic_stream | 0/3 | 2/2 | 193.16/156.01 | 134.12/143.94 | False/False | False/False |
| node/gemini | 0/3 | 2/2 | 128.32/379.56 | 123.67/230.16 | False/False | False/False |
| node/embeddings | 0/3 | 2/2 | 181.48/161.11 | 126.77/140.73 | False/False | False/False |
| node/tool_calls | 0/3 | 2/2 | 187.44/165.70 | 131.73/140.66 | False/False | False/False |
| node/transcription | 0/3 | 2/2 | 184.31/163.07 | 128.14/141.45 | False/False | False/False |
| node/tts | 0/3 | 2/2 | 178.16/161.47 | 126.01/141.34 | False/False | False/False |
| node/vercel | 0/3 | 2/2 | 149.29/247.21 | 134.13/226.77 | False/False | False/False |
| node/raw_http | 0/4 | 2/2 | 103.20/109.75 | 112.48/129.38 | False/False | False/False |
| node/unsupported_sdk | 0/4 | 2/2 | 128.84/118.16 | 118.47/132.34 | False/False | False/False |
| node/openai_error | 0/3 | 2/2 | 242.62/176.42 | 130.02/141.09 | False/False | False/False |

## Per-metric ratios (Braintrust / traceAI)

Cost ratios >1 favor traceAI; coverage/span ratios are descriptive (more spans can mean duplicates). LOC has a zero denominator and is n/a, not infinity. Parity/privacy are categorical, not meaningful multiplicative ratios. Failed parity suppresses performance/coverage ratios.

| Runtime/scenario | Semantic | Spans | App LOC | Commands | p50 call | p99 call | Startup | RSS | Positive added p50 / p99 |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| python/openai_chat | 1.17 | 1.00 | n/a | 1.00 | 0.68 | 12.27 | 4.77 | 2.59 | 0.23 / 19.15 |
| python/openai_stream | 1.17 | 1.00 | n/a | 1.00 | 0.96 | 0.99 | 4.36 | 2.59 | n/a / 0.79 |
| python/anthropic_stream | 1.14 | 0.50 | n/a | 1.00 | 1.01 | 0.75 | 2.48 | 1.96 | n/a / 0.32 |
| python/gemini | 1.17 | 1.00 | n/a | 1.00 | 0.47 | 0.36 | 4.65 | 2.46 | 0.17 / 0.14 |
| python/embeddings | 1.17 | 1.00 | n/a | 1.00 | 0.47 | 0.62 | 4.39 | 2.64 | 0.04 / 0.32 |
| python/tool_calls | 1.14 | 1.00 | n/a | 1.00 | 0.42 | 0.55 | 4.38 | 2.59 | 0.14 / 0.28 |
| python/transcription | 1.14 | 1.00 | n/a | 1.00 | 0.56 | 0.63 | 4.64 | 2.61 | 0.08 / 0.38 |
| python/tts | 1.17 | 1.00 | n/a | 1.00 | 0.48 | 0.67 | 4.37 | 2.62 | 0.14 / 0.39 |
| python/langchain | 1.17 | 2.00 | n/a | 1.00 | 0.57 | 0.72 | 2.62 | 2.01 | 0.26 / 0.46 |
| python/raw_http | 1.00 | 1.00 | n/a | 1.00 | 0.32 | 0.39 | 8.07 | 3.43 | 0.06 / 0.18 |
| python/unsupported_sdk | 1.17 | 1.00 | n/a | 1.00 | 0.47 | 0.50 | 5.77 | 2.75 | 0.10 / 0.25 |
| python/openai_error | 1.00 | 1.00 | n/a | 1.00 | 0.70 | 2.63 | 5.18 | 2.59 | 0.03 / 2.02 |
| node/openai_chat | 1.17 | 1.00 | n/a | 1.00 | 0.68 | 0.69 | 1.31 | 1.11 | 0.14 / 0.56 |
| node/openai_stream | 1.17 | 1.00 | n/a | 1.00 | 0.98 | 0.97 | 0.88 | 1.08 | n/a / 0.76 |
| node/anthropic_stream | 1.17 | 1.00 | n/a | 1.00 | 0.97 | 0.95 | 0.81 | 1.07 | n/a / 0.45 |
| node/gemini | 1.00 | 1.00 | n/a | 1.00 | 0.63 | 0.67 | 2.96 | 1.86 | 0.16 / 0.35 |
| node/embeddings | 1.17 | 1.00 | n/a | 1.00 | 0.60 | 0.63 | 0.89 | 1.11 | 0.10 / 0.18 |
| node/tool_calls | 1.14 | 1.00 | n/a | 1.00 | 0.58 | 0.56 | 0.88 | 1.07 | 0.15 / 0.41 |
| node/transcription | 1.14 | 1.00 | n/a | 1.00 | 0.66 | 0.31 | 0.88 | 1.10 | 0.10 / 0.15 |
| node/tts | 1.00 | 1.00 | n/a | 1.00 | 0.65 | 0.58 | 0.91 | 1.12 | 0.09 / 0.21 |
| node/vercel | 1.00 | 2.00 | n/a | 1.00 | 0.72 | 1.66 | 1.66 | 1.69 | 0.33 / 1.73 |
| node/raw_http | 1.00 | 1.00 | n/a | 1.00 | 0.65 | 0.98 | 1.06 | 1.15 | 0.15 / 0.95 |
| node/unsupported_sdk | 1.17 | 1.00 | n/a | 1.00 | 0.65 | 0.61 | 0.92 | 1.12 | 0.18 / 0.27 |
| node/openai_error | 1.25 | 1.00 | n/a | 1.00 | 0.71 | 0.86 | 0.73 | 1.09 | 0.14 / 0.77 |

## Where traceAI is NOT ahead

- No 10x claim is established: every timing below is a shared-laptop, tiny-N smoke observation.
- Native provider attribution is a stricter rubric than protocol identity: wire exports labelled openai_compatible/gemini_compatible do not get OpenAI/Google identity credit for SDK scenarios. The raw gateway only requires openai_compatible.
- Both adapters require provisioning and a launch. Zero application LOC is not zero deployment effort; Node uses a staged register bundle because the workspace root export does not auto-start.
- python/openai_chat: Braintrust semantic score is equal or higher (100.00% vs 85.71%).
- python/openai_chat: Braintrust observed latency_p50 cost is no higher (BT/T=0.68); tiny-N noise, not a speed conclusion.
- python/openai_stream: Braintrust semantic score is equal or higher (87.50% vs 75.00%).
- python/openai_stream: Braintrust observed latency_p50 cost is no higher (BT/T=0.96); tiny-N noise, not a speed conclusion.
- python/openai_stream: Braintrust observed latency_p99 cost is no higher (BT/T=0.99); tiny-N noise, not a speed conclusion.
- python/anthropic_stream: Braintrust semantic score is equal or higher (100.00% vs 87.50%).
- python/anthropic_stream: Braintrust observed latency_p99 cost is no higher (BT/T=0.75); tiny-N noise, not a speed conclusion.
- python/gemini: Braintrust semantic score is equal or higher (100.00% vs 85.71%).
- python/gemini: Braintrust observed latency_p50 cost is no higher (BT/T=0.47); tiny-N noise, not a speed conclusion.
- python/gemini: Braintrust observed latency_p99 cost is no higher (BT/T=0.36); tiny-N noise, not a speed conclusion.
- python/embeddings: Braintrust semantic score is equal or higher (100.00% vs 85.71%).
- python/embeddings: Braintrust observed latency_p50 cost is no higher (BT/T=0.47); tiny-N noise, not a speed conclusion.
- python/embeddings: Braintrust observed latency_p99 cost is no higher (BT/T=0.62); tiny-N noise, not a speed conclusion.
- python/tool_calls: Braintrust semantic score is equal or higher (100.00% vs 87.50%).
- python/tool_calls: Braintrust observed latency_p50 cost is no higher (BT/T=0.42); tiny-N noise, not a speed conclusion.
- python/tool_calls: Braintrust observed latency_p99 cost is no higher (BT/T=0.55); tiny-N noise, not a speed conclusion.
- python/transcription: Braintrust semantic score is equal or higher (100.00% vs 87.50%).
- python/transcription: Braintrust observed latency_p50 cost is no higher (BT/T=0.56); tiny-N noise, not a speed conclusion.
- python/transcription: Braintrust observed latency_p99 cost is no higher (BT/T=0.63); tiny-N noise, not a speed conclusion.
- python/tts: Braintrust semantic score is equal or higher (87.50% vs 75.00%).
- python/tts: Braintrust observed latency_p50 cost is no higher (BT/T=0.48); tiny-N noise, not a speed conclusion.
- python/tts: Braintrust observed latency_p99 cost is no higher (BT/T=0.67); tiny-N noise, not a speed conclusion.
- python/langchain: Braintrust semantic score is equal or higher (100.00% vs 85.71%).
- python/langchain: Braintrust observed latency_p50 cost is no higher (BT/T=0.57); tiny-N noise, not a speed conclusion.
- python/langchain: Braintrust observed latency_p99 cost is no higher (BT/T=0.72); tiny-N noise, not a speed conclusion.
- python/raw_http: Braintrust semantic score is equal or higher (100.00% vs 100.00%).
- python/raw_http: Braintrust observed latency_p50 cost is no higher (BT/T=0.32); tiny-N noise, not a speed conclusion.
- python/raw_http: Braintrust observed latency_p99 cost is no higher (BT/T=0.39); tiny-N noise, not a speed conclusion.
- python/unsupported_sdk: Braintrust semantic score is equal or higher (100.00% vs 85.71%).
- python/unsupported_sdk: Braintrust observed latency_p50 cost is no higher (BT/T=0.47); tiny-N noise, not a speed conclusion.
- python/unsupported_sdk: Braintrust observed latency_p99 cost is no higher (BT/T=0.50); tiny-N noise, not a speed conclusion.
- python/openai_error: Braintrust semantic score is equal or higher (80.00% vs 80.00%).
- python/openai_error: Braintrust observed latency_p50 cost is no higher (BT/T=0.70); tiny-N noise, not a speed conclusion.
- node/openai_chat: Braintrust semantic score is equal or higher (100.00% vs 85.71%).
- node/openai_chat: Braintrust observed latency_p50 cost is no higher (BT/T=0.68); tiny-N noise, not a speed conclusion.
- node/openai_chat: Braintrust observed latency_p99 cost is no higher (BT/T=0.69); tiny-N noise, not a speed conclusion.
- node/openai_stream: Braintrust semantic score is equal or higher (87.50% vs 75.00%).
- node/openai_stream: Braintrust observed latency_p50 cost is no higher (BT/T=0.98); tiny-N noise, not a speed conclusion.
- node/openai_stream: Braintrust observed latency_p99 cost is no higher (BT/T=0.97); tiny-N noise, not a speed conclusion.
- node/openai_stream: Braintrust observed startup cost is no higher (BT/T=0.88); tiny-N noise, not a speed conclusion.
- node/anthropic_stream: Braintrust semantic score is equal or higher (87.50% vs 75.00%).
- node/anthropic_stream: Braintrust observed latency_p50 cost is no higher (BT/T=0.97); tiny-N noise, not a speed conclusion.
- node/anthropic_stream: Braintrust observed latency_p99 cost is no higher (BT/T=0.95); tiny-N noise, not a speed conclusion.
- node/anthropic_stream: Braintrust observed startup cost is no higher (BT/T=0.81); tiny-N noise, not a speed conclusion.
- node/gemini: Braintrust semantic score is equal or higher (85.71% vs 85.71%).
- node/gemini: Braintrust observed latency_p50 cost is no higher (BT/T=0.63); tiny-N noise, not a speed conclusion.
- node/gemini: Braintrust observed latency_p99 cost is no higher (BT/T=0.67); tiny-N noise, not a speed conclusion.
- node/embeddings: Braintrust semantic score is equal or higher (100.00% vs 85.71%).
- node/embeddings: Braintrust observed latency_p50 cost is no higher (BT/T=0.60); tiny-N noise, not a speed conclusion.
- node/embeddings: Braintrust observed latency_p99 cost is no higher (BT/T=0.63); tiny-N noise, not a speed conclusion.
- node/embeddings: Braintrust observed startup cost is no higher (BT/T=0.89); tiny-N noise, not a speed conclusion.
- node/tool_calls: Braintrust semantic score is equal or higher (100.00% vs 87.50%).
- node/tool_calls: Braintrust observed latency_p50 cost is no higher (BT/T=0.58); tiny-N noise, not a speed conclusion.
- node/tool_calls: Braintrust observed latency_p99 cost is no higher (BT/T=0.56); tiny-N noise, not a speed conclusion.
- node/tool_calls: Braintrust observed startup cost is no higher (BT/T=0.88); tiny-N noise, not a speed conclusion.
- node/transcription: Braintrust semantic score is equal or higher (100.00% vs 87.50%).
- node/transcription: Braintrust observed latency_p50 cost is no higher (BT/T=0.66); tiny-N noise, not a speed conclusion.
- node/transcription: Braintrust observed latency_p99 cost is no higher (BT/T=0.31); tiny-N noise, not a speed conclusion.
- node/transcription: Braintrust observed startup cost is no higher (BT/T=0.88); tiny-N noise, not a speed conclusion.
- node/tts: Braintrust semantic score is equal or higher (75.00% vs 75.00%).
- node/tts: Braintrust observed latency_p50 cost is no higher (BT/T=0.65); tiny-N noise, not a speed conclusion.
- node/tts: Braintrust observed latency_p99 cost is no higher (BT/T=0.58); tiny-N noise, not a speed conclusion.
- node/tts: Braintrust observed startup cost is no higher (BT/T=0.91); tiny-N noise, not a speed conclusion.
- node/vercel: Braintrust semantic score is equal or higher (85.71% vs 85.71%).
- node/vercel: Braintrust observed latency_p50 cost is no higher (BT/T=0.72); tiny-N noise, not a speed conclusion.
- node/raw_http: Braintrust semantic score is equal or higher (100.00% vs 100.00%).
- node/raw_http: Braintrust observed latency_p50 cost is no higher (BT/T=0.65); tiny-N noise, not a speed conclusion.
- node/raw_http: Braintrust observed latency_p99 cost is no higher (BT/T=0.98); tiny-N noise, not a speed conclusion.
- node/unsupported_sdk: Braintrust semantic score is equal or higher (100.00% vs 85.71%).
- node/unsupported_sdk: Braintrust observed latency_p50 cost is no higher (BT/T=0.65); tiny-N noise, not a speed conclusion.
- node/unsupported_sdk: Braintrust observed latency_p99 cost is no higher (BT/T=0.61); tiny-N noise, not a speed conclusion.
- node/unsupported_sdk: Braintrust observed startup cost is no higher (BT/T=0.92); tiny-N noise, not a speed conclusion.
- node/openai_error: Braintrust semantic score is equal or higher (100.00% vs 80.00%).
- node/openai_error: Braintrust observed latency_p50 cost is no higher (BT/T=0.71); tiny-N noise, not a speed conclusion.
- node/openai_error: Braintrust observed latency_p99 cost is no higher (BT/T=0.86); tiny-N noise, not a speed conclusion.
- node/openai_error: Braintrust observed startup cost is no higher (BT/T=0.73); tiny-N noise, not a speed conclusion.

## Protocol and limitations

- Tiny N per process, warm caches, shared laptop. Each complete cycle runs all six tool-order permutations in reverse pairs (each tool in each position twice; pairwise precedence 3:3). Equal-N paired deltas pool within-repetition baseline comparisons, never unrelated processes. Drift and nonlinear carryover remain possible; p99 interpolation is not tail confidence. No 10x claim; nonpositive added overhead ratios are null.
- Call latency includes full stream consumption/SDK processing and a fixed 30 ms mock pause before first text. App TTFC is the first SDK item; app TTFT is first nonempty semantic text. Semantic TTFT requires a reviewed text-aware source trigger AND candidate alignment to separated app events within 3 ms (+1 ms Node rounding). First-body/item triggers never earn semantic credit even if buffering numerically coincides with text. Trigger contracts/source hashes are recorded; only the inspected hash-pinned Python Braintrust Anthropic trigger is semantic for these methods. Unknown implementations, missing/ambiguous/out-of-window references abstain. This is a fixture-specific check, not a universal proof. Ratios require complete counterbalance and parity.
- Startup is spawn to ready after relevant SDK imports/client construction, before first call; medians/distributions cover one workload process per repetition, not separate zero-call cold starts. RSS is individual wait4 high-water mark, not process-tree memory.
- Semantic score is equal-weight required fields per actual call (warmups included), value-checked from actual exports. Model/provider must be explicit. No inferred provider credit. Unmapped spans are reported. Span counts alone are not quality.
- Semantic evidence is unioned within each call's epoch interval, including framework/provider children; duplicates can increase span count but not score. Node exports have millisecond clocks: matching uses +/-1 ms and a 4 ms inter-call gap outside timers. Zero-resolution completed durations are valid. Successful native spans count as non-error by native conventions; a deliberate 400 separately checks errors.
- Full SDK JSON responses/chunks are compared within each language, except LangChain's generated random message id is omitted and Vercel exposes text/finishReason/usage/toolCalls. This is not HTTP wire-byte or cross-language parity.
- Mocks use OpenAI JSON/SSE, Anthropic SSE, Gemini JSON, multipart transcription and a real 0.1-second synthetic PCM WAV. TTS duration is not invented if the export does not provide it. No real provider behavior, cancellations, async/concurrency or production performance tested.
- Privacy scans exported entity bodies/attachment uploads for dummy credential and raw-media canaries; normal collector auth headers are excluded. No exports means unobserved, never privacy success. Transcript/content defaults are not disabled.
- Loopback base URLs, cleared inherited credentials, fake HOME and closed proxies are mandatory. Python socket / Node socket-DNS-fetch guards add defense in depth, not a kernel-enforced sandbox.
- Braintrust defaults to background export; a documented flush is outside timed calls after identical idle grace. Flush LOC is counted. traceAI uses normal process shutdown. Tool-specific batching/flush policy is not normalized into a misleading first-trace speed claim.
- Python FI_AUTO_INSTRUMENT tests the isolated locally built wheel; no optional native traceAI provider packages are added. Node tests an unchanged snapshot of the relocatable dist/agent register bundle using a scratch package export, not the currently inert workspace root import.
- Setup LOC counts inserted executable lines versus identical fixtures, including initialization/flush/manual wrapper; install and launch are each one command. Zero traceAI LOC yields an undefined ratio, not infinity/10x. Environment/config/account effort is not condensed into LOC.
- Source can change under other workers. Agent artifacts, fixtures, commands, versions and hashes are recorded per invocation. Harness fingerprints include compare Python/MJS, dependency locks and reused measure.py/mock_servers.py/safety.py; append rejects differing fingerprints and checks them at each block and before final save. Installations are explicit separate host-gated steps, never automatic in the orchestrator.

## Braintrust configuration

Python uses `init_logger` + `auto_instrument()` before SDK imports (including LangChain); Node uses `initLogger` + `node --import braintrust/hook.mjs` (including Vercel). OpenAI audio/embedding/tool calls use the same native auto configuration, not a weaker obsolete chat-only wrapper.

Raw HTTP and Together have no native entry in the inspected matrices; they additionally use a documented generic `traced`/`wrapTraced` operation with model/provider metadata and automatic I/O. These are labelled manual, not native provider coverage. Usage/TTFT/audio metadata are not fabricated by the harness. Exact inserted setup/diffs, actual commands and all installed versions are in results.json.

Documentation was read via indexed public excerpts (no direct Braintrust-domain request). Read-only source matrices were inspected, never executed. Published PyPI/npm packages are the implementations run. Sources:
- https://www.braintrust.dev/docs/sdks/python/install-and-instrument.md
- https://www.braintrust.dev/docs/instrument/trace-llm-calls
- https://www.braintrust.dev/docs/sdks/typescript/versions/3.38.0
- https://github.com/braintrustdata/braintrust-sdk-python

## Failures

