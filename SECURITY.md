# Security Policy

## Reporting a vulnerability

The Future AGI team takes security seriously. If you discover a vulnerability in `traceAI`, please report it privately — **do not open a public GitHub issue.**

**Email:** **security@futureagi.com**

Include as much of the following as you can:

- Type of issue (e.g. credential leak in trace spans, data exfiltration via instrumentor, OTLP injection)
- Affected version(s) and the commit or release tag
- Reproduction steps
- Proof-of-concept or exploit code, if possible
- Impact — how an attacker might exploit it

## Response timeline

- **Acknowledgement:** within 24 hours (Mon–Fri, Pacific & IST)
- **Initial assessment:** within 3 business days
- **Fix target:** depends on severity
- **Public disclosure:** coordinated with the reporter, typically 7–90 days after a patch is available

## Scope

**In scope:**

- The `fi-instrumentation-otel` PyPI / NuGet packages
- The `@traceai/*` npm packages
- The `traceai-java-*` Maven packages
- This repository's source (`future-agi/traceAI`)

**Out of scope:**

- Third-party LLM providers or frameworks being instrumented (report upstream)
- OpenTelemetry SDK itself (report to opentelemetry.io)
- Data users send through instrumented applications (user-controlled input)

For vulnerabilities that affect the broader Future AGI platform, see the [main repo's SECURITY.md](https://github.com/future-agi/future-agi/blob/main/SECURITY.md).
