from fi_instrumentation import register
from fi_instrumentation.fi_types import ProjectType
import os
from guardrails import Guard, OnFailAction
from traceai_guardrails import GuardrailsInstrumentor
from dotenv import load_dotenv

load_dotenv()

# --- Setup Instrumentation ---
# This setup registers the project with the FI observability platform
# and instruments the Guardrails library to send OTel spans.
trace_provider = register(
    project_type=ProjectType.OBSERVE,
    project_name="GUARDRAILSPAN2",
    session_name="GUARDRAILSPAN2",
)

GuardrailsInstrumentor().instrument(tracer_provider=trace_provider)

#from guardrails import Guard, OnFailAction
from guardrails.hub import CompetitorCheck, ToxicLanguage

guard = Guard().use_many(
    CompetitorCheck(["Apple", "Microsoft", "Google"], on_fail=OnFailAction.EXCEPTION),
    ToxicLanguage(threshold=0.5, validation_method="sentence", on_fail=OnFailAction.EXCEPTION)
)

guard.validate(
    """An apple a day keeps a doctor away.
    This is good advice for keeping your health."""
)  # Both the guardrails pass

try:
    guard.validate(
        """Shut the hell up! Apple just released a new iPhone."""
    )  # Both the guardrails fail
except Exception as e:
    print(e)