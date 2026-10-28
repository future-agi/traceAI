"""Trace TypeSafe System One decision calls in Future AGI.

Needs FI_API_KEY, FI_SECRET_KEY and TYPESAFE_API_KEY. TYPESAFE_BASE_URL is
optional (the SDK default is https://api.typesafe.ai).
"""

from fi_instrumentation import register
from fi_instrumentation.fi_types import ProjectType
from traceai_typesafe import TypeSafeInstrumentor
from typesafe_sdk import Choice, Noul, Score, TypeSafeClient

trace_provider = register(project_type=ProjectType.OBSERVE, project_name="typesafe-example")
TypeSafeInstrumentor().instrument(tracer_provider=trace_provider)

with TypeSafeClient() as client:
    result = client.system_one(
        state="I was charged twice for my order and nobody is answering. Please help.",
        questions={
            "billing": Noul(instructions="Is this message about billing?"),
            "tone": Choice(
                instructions="What is the tone of this message?",
                criteria={"calm": None, "angry": None, "excited": None},
            ),
            "urgency": Score(
                instructions="How urgent is this message?",
                criteria=["Can wait", "Needs attention this week", "Needs attention today"],
            ),
        },
    )
    print(result.model, result.nouls["billing"].noul, result.choices["tone"].choice, result.scores["urgency"].score)

trace_provider.force_flush()
