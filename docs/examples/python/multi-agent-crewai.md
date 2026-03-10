# Multi-Agent CrewAI Example

Build a traced multi-agent workflow with CrewAI.

## Prerequisites

```bash
pip install fi-instrumentation traceai-crewai traceai-openai
pip install crewai crewai-tools
```

## Full Example

```python
import os
from fi_instrumentation import register, using_attributes
from fi_instrumentation.fi_types import ProjectType
from traceai_crewai import CrewAIInstrumentor
from traceai_openai import OpenAIInstrumentor

from crewai import Agent, Task, Crew, Process

# 1. Set environment variables
os.environ["FI_API_KEY"] = "your-api-key"
os.environ["FI_SECRET_KEY"] = "your-secret-key"
os.environ["OPENAI_API_KEY"] = "your-openai-key"

# 2. Register and instrument
trace_provider = register(
    project_type=ProjectType.OBSERVE,
    project_name="crewai_agents"
)

CrewAIInstrumentor().instrument(tracer_provider=trace_provider)
OpenAIInstrumentor().instrument(tracer_provider=trace_provider)

# 3. Define agents
researcher = Agent(
    role="Research Analyst",
    goal="Research and analyze topics thoroughly",
    backstory="You are an expert researcher with deep analytical skills.",
    verbose=True,
    allow_delegation=False
)

writer = Agent(
    role="Content Writer",
    goal="Write clear, engaging content based on research",
    backstory="You are a skilled writer who creates compelling content.",
    verbose=True,
    allow_delegation=False
)

editor = Agent(
    role="Editor",
    goal="Review and improve content for clarity and accuracy",
    backstory="You are a meticulous editor with an eye for detail.",
    verbose=True,
    allow_delegation=False
)

# 4. Define tasks
research_task = Task(
    description="Research the topic: {topic}. Provide key facts and insights.",
    expected_output="A comprehensive research summary with key points.",
    agent=researcher
)

writing_task = Task(
    description="Write a blog post based on the research about: {topic}",
    expected_output="A well-structured blog post of 300-500 words.",
    agent=writer,
    context=[research_task]  # Depends on research
)

editing_task = Task(
    description="Edit and improve the blog post for clarity and engagement.",
    expected_output="A polished, publication-ready blog post.",
    agent=editor,
    context=[writing_task]  # Depends on writing
)

# 5. Create crew
crew = Crew(
    agents=[researcher, writer, editor],
    tasks=[research_task, writing_task, editing_task],
    process=Process.sequential,
    verbose=True
)

# 6. Run function
def create_content(topic: str, session_id: str = None) -> str:
    """Run the content creation crew."""
    with using_attributes(
        session_id=session_id,
        metadata={"workflow": "content_creation", "topic": topic}
    ):
        result = crew.kickoff(inputs={"topic": topic})
        return result

# 7. Execute
if __name__ == "__main__":
    result = create_content(
        topic="The future of AI in healthcare",
        session_id="crew-session-001"
    )
    print("Final Output:")
    print(result)
```

## Trace Structure

CrewAI creates a hierarchical trace:

```
crew.kickoff (AGENT)
├── research_task (CHAIN)
│   └── researcher.execute (AGENT)
│       └── llm.chat (LLM)
├── writing_task (CHAIN)
│   └── writer.execute (AGENT)
│       └── llm.chat (LLM)
└── editing_task (CHAIN)
    └── editor.execute (AGENT)
        └── llm.chat (LLM)
```

## Captured Attributes

### Crew Span
| Attribute | Value |
|-----------|-------|
| `fi.span_kind` | `AGENT` |
| `crew.key` | Crew identifier |
| `crew.id` | Unique crew run ID |

### Task Span
| Attribute | Value |
|-----------|-------|
| `fi.span_kind` | `CHAIN` |
| `task.description` | Task description |
| `task.expected_output` | Expected output |

### Agent Span
| Attribute | Value |
|-----------|-------|
| `fi.span_kind` | `AGENT` |
| `agent.role` | Agent role |
| `agent.goal` | Agent goal |

## With Tools

```python
from crewai_tools import SerperDevTool, WebsiteSearchTool

# Add tools to agents
search_tool = SerperDevTool()
web_tool = WebsiteSearchTool()

researcher_with_tools = Agent(
    role="Research Analyst",
    goal="Research topics using web search",
    backstory="Expert researcher with access to web search.",
    tools=[search_tool, web_tool],
    verbose=True
)

research_task_with_tools = Task(
    description="Search the web and research: {topic}",
    expected_output="Research findings with sources.",
    agent=researcher_with_tools
)
```

Tool usage is traced with:
- `fi.span_kind` = `TOOL`
- Tool name, input, output

## Hierarchical Crew

```python
# Manager agent for delegation
manager = Agent(
    role="Project Manager",
    goal="Coordinate the team to produce quality content",
    backstory="Experienced PM who delegates effectively.",
    allow_delegation=True
)

hierarchical_crew = Crew(
    agents=[manager, researcher, writer, editor],
    tasks=[research_task, writing_task, editing_task],
    process=Process.hierarchical,
    manager_agent=manager,
    verbose=True
)
```

## Async Execution

```python
import asyncio

async def create_content_async(topic: str) -> str:
    """Run crew asynchronously."""
    with using_attributes(metadata={"async": True}):
        result = await crew.kickoff_async(inputs={"topic": topic})
        return result

# Run
result = asyncio.run(create_content_async("AI in education"))
```

## With Experiments

Evaluate agent performance:

```python
from fi_instrumentation.fi_types import (
    EvalTag, EvalTagType, EvalSpanKind, EvalName, ModelChoices
)

eval_tags = [
    # Evaluate task completion
    EvalTag(
        type=EvalTagType.OBSERVATION_SPAN,
        value=EvalSpanKind.AGENT,
        eval_name=EvalName.TASK_COMPLETION,
        custom_eval_name="agent_task_completion",
        mapping={
            "task": "raw.input",
            "output": "raw.output"
        },
        model=ModelChoices.TURING_SMALL
    ),
    # Check content quality
    EvalTag(
        type=EvalTagType.OBSERVATION_SPAN,
        value=EvalSpanKind.LLM,
        eval_name=EvalName.COMPLETENESS,
        custom_eval_name="output_completeness",
        mapping={"output": "raw.output"},
        model=ModelChoices.TURING_SMALL
    )
]

trace_provider = register(
    project_type=ProjectType.EXPERIMENT,
    project_name="crewai_experiment",
    project_version_name="v1.0",
    eval_tags=eval_tags
)
```

## Custom Agent Callbacks

```python
from crewai import Agent

def on_task_complete(task_output):
    """Callback when task completes."""
    print(f"Task completed: {task_output[:100]}...")

researcher = Agent(
    role="Researcher",
    goal="Research thoroughly",
    backstory="Expert researcher.",
    callbacks=[on_task_complete]  # Custom callbacks
)
```

## Error Handling

```python
def safe_create_content(topic: str) -> str:
    """Safely run the crew with error handling."""
    try:
        with using_attributes(metadata={"topic": topic}):
            return crew.kickoff(inputs={"topic": topic})
    except Exception as e:
        # Error captured in trace
        print(f"Crew error: {e}")
        return f"Failed to create content: {e}"
```

## Multiple Crews

```python
# Research crew
research_crew = Crew(
    agents=[researcher],
    tasks=[research_task],
    process=Process.sequential
)

# Writing crew
writing_crew = Crew(
    agents=[writer, editor],
    tasks=[writing_task, editing_task],
    process=Process.sequential
)

def pipeline(topic: str) -> str:
    """Run crews in sequence."""
    with using_attributes(session_id="pipeline-001"):
        # Research phase
        research_result = research_crew.kickoff(inputs={"topic": topic})

        # Writing phase (uses research)
        final_result = writing_crew.kickoff(inputs={
            "topic": topic,
            "research": research_result
        })

        return final_result
```

## Related

- [Basic OpenAI](basic-openai.md)
- [LangChain RAG](langchain-rag.md)
- [Evaluation Tags](../../configuration/eval-tags.md)
