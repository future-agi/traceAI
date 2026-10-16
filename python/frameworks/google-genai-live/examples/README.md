# Example: a traced Gemini Live voice session

`voice_tool.py` opens one Live session on `gemini-3.8-live`, asks for the weather (typed, or
spoken from a 16 kHz 16-bit mono PCM file), answers the model's `get_weather` tool call, and
lets the model answer out loud. With `instrument()` the whole session is one Future AGI trace.

```bash
pip install traceAI-google-genai-live "google-genai>=2.28,<3"
export GEMINI_API_KEY=... FI_API_KEY=... FI_SECRET_KEY=...
# export FI_BASE_URL=...   # only for a self-hosted Future AGI collector
python voice_tool.py --project gemini-live-demo
python voice_tool.py --pcm question.pcm          # spoken question
python voice_tool.py --metadata-only --marker X  # no content leaves the process
python voice_tool.py --resume                    # drop the socket, resume with the handle: still one trace
python voice_tool.py --model not-a-model         # setup rejected: one ERROR root
```

A spoken question on macOS:

```bash
say -o q.aiff "What is the weather in Paris right now?"
afconvert -f WAVE -d LEI16@16000 -c 1 q.aiff q.wav
python -c "import wave; w=wave.open('q.wav'); open('question.pcm','wb').write(w.readframes(w.getnframes()))"
```

`run_e2e.sh` runs the scenarios above in a fresh virtualenv built from this checkout.
