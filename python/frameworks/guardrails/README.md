# Trace Guardrails validations

Send Guardrails' native OpenTelemetry traces to Future AGI with semantic mapping and content filtering, without adding a second set of execution spans.

## Install

Use Python 3.10–3.13. This example uses Guardrails AI 0.11.0.

```bash
pip install -U traceAI-guardrails 'guardrails-ai==0.11.0'
export FI_API_KEY="your-futureagi-api-key"
export FI_SECRET_KEY="your-futureagi-secret-key"
```

## Configure tracing

Configure the instrumentor once, before running Guards. The provider does not need to be global, and the adapter does not replace your application's global tracer provider.

```python
from fi_instrumentation import register, TraceConfig
from fi_instrumentation.fi_types import ProjectType
from traceai_guardrails import GuardrailsInstrumentor

trace_provider = register(
    project_type=ProjectType.OBSERVE,
    project_name="guardrails_project",
)
GuardrailsInstrumentor().instrument(
    tracer_provider=trace_provider,
    config=TraceConfig(hide_inputs=True, hide_outputs=True),
)
```

## Run a validation

This custom validator runs locally and does not require a model-provider key.

```python
from guardrails import Guard
from guardrails.validators import FailResult, PassResult, Validator, register_validator


@register_validator(name="two-words", data_type="string")
class TwoWords(Validator):
    def _validate(self, value, metadata):
        if len(value.split()) == 2:
            return PassResult()
        return FailResult(error_message="Expected exactly two words.")


guard = Guard().use(TwoWords(on_fail="exception"))
guard.configure(allow_metrics_collection=False)
try:
    result = guard.validate("Hello world")
    print(result.validation_passed)
finally:
    trace_provider.force_flush()
```

In **Observe**, select `guardrails_project` to inspect the native `guard`, `step`, `call`, and validator spans. The validator result is available alongside its name and failure policy.

## Content and compatibility

- Both hide flags apply to native spans emitted through this adapter, including streaming guard spans. Error descriptions and exception details are also hidden by default when content hiding is enabled.
- Arbitrary validator metadata, constructor parameters, invocation parameters, baggage, and mixed runner-history dumps are not exported. Step spans retain structure rather than serializing application history. Model and token attributes are mapped when Guardrails supplies them; they are not inferred.
- Configure other SDK instrumentors separately. This adapter does not filter unrelated application spans or Guardrails Hub telemetry; `allow_metrics_collection=False` disables Hub collection for the Guard above.
- Guardrails' `settings.disable_tracing` remains authoritative. When it is `True`, native traces are suppressed.
- Existing `GuardrailsInstrumentor().instrument(...)` and `.uninstrument()` calls remain supported. SDKs without native guard/runner telemetry, including Guardrails 0.5.0, retain the legacy adapter. Upgrade Guardrails to use the native route.
- Uninstrumentation restores the telemetry bindings for subsequent calls. Finish in-flight validations and streams before changing tracing configuration.
