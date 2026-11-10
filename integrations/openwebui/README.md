# Open WebUI

Use the maintained [traceAI-openwebui package](../../python/frameworks/openwebui/) to trace individual model calls through an Open WebUI Pipe Function.

The standalone `futureagi_filter.py` has been retired. Its inlet/outlet hooks measured a whole chat turn, not an individual model invocation, and did not reliably run on errors or cancellation.

Disable the old Future AGI Filter in Open WebUI before installing the package's Function. Select the new **Traced model** for calls you want to trace. Existing copies stored in Open WebUI's database are not automatically removed. See the package's [migration instructions](../../python/frameworks/openwebui/#replace-the-old-filter).
