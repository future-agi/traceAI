# TraceAI Instrumentation for OpenAI Swarm

This package provides OpenTelemetry instrumentation for the [OpenAI Swarm](https://github.com/openai/swarm) framework.

## Installation

```bash
pip install traceai-swarm
```

## Usage

```python
from swarm import Swarm, Agent
from traceai_swarm import SwarmInstrumentor

# Instrument Swarm
SwarmInstrumentor().instrument()

client = Swarm()

def transfer_to_agent_b():
    return agent_b

agent_a = Agent(
    name="Agent A",
    instructions="You are a helpful agent.",
    functions=[transfer_to_agent_b],
)

agent_b = Agent(
    name="Agent B",
    instructions="Only speak in Haikus.",
)

response = client.run(
    agent=agent_a,
    messages=[{"role": "user", "content": "I want to talk to agent B."}],
)

print(response.messages[-1]["content"])
```
