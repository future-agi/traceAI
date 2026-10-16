# End-to-end run (ElevenLabs stood in)

`run_e2e.sh` starts the real `traceai-elevenlabs-agents` process, configured only with the
environment variables from the package README, and drives it with `standin.py`, a local
**stand-in for ElevenLabs**: deliveries built from the docs-shaped fixtures in
`tests/fixtures/`, signed exactly as documented with a throwaway secret, and a
`GET /v1/convai/conversations/{id}?format=opentelemetry` endpoint. No ElevenLabs account
is used and nothing here is a live ElevenLabs session.

```bash
pip install -e ..            # or: pip install traceAI-elevenlabs-agents
pip install elevenlabs       # optional: cross-checks signatures and the GET body with the real SDK
export FI_API_KEY=... FI_SECRET_KEY=... FI_BASE_URL=http://localhost:14338   # or your Future AGI
./run_e2e.sh my-elevenlabs-project ./out
```

It runs twice: content capture off into `<project>`, then on into `<project>-optin`.
Steps: one conversation with a tool call and two LLM turns, the same delivery again
(must be `duplicate`), an abnormal-disconnect conversation (root ERROR, failing tool),
wrong / stale / missing / mutated signatures (401), the three other webhook types
(200 ignored), a payload without `resourceSpans` (400), a backfill of the delivered
conversation (`duplicate`) and of a GET-only conversation (`forwarded`), and a second
forwarder whose collector is down (502). The report (`out/e2e-report.json`) lists every
status, the conversation ids and the unique marker written into every content field,
so you can search your project for it and confirm it is absent with content capture off.
