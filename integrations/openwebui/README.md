"""Install

1. In Open WebUI, open Admin, Functions, and create a function.
2. Paste the contents of `futureagi_filter.py`.
3. Set the valves: API key, secret key, endpoint, and the Future AGI project name.

The filter exports to `{endpoint}/tracer/v1/traces` with `X-Api-Key` and `X-Secret-Key`. Content is omitted when `redact` is true. Email is never sent. `include_email_hash` sends a SHA-256 of the email and defaults to off.

Tested against Open WebUI v0.11.4. This file does not modify Open WebUI.

The native OpenTelemetry exporter in Open WebUI is a separate path. Its spans are not correlated with the spans this filter sends.
