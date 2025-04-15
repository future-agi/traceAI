"""
LangGraph Agent Supervisor Example

A multi-agent system that coordinates between a researcher and coder through a supervisor.
Based on https://colab.research.google.com/drive/1xDEPe2i_2rRqs7o6oNTtqA4J7Orsnvx1

Requires Tavily API Key https://github.com/tavily-ai/tavily-python
"""

import functools
import operator
from dataclasses import dataclass
from typing import Annotated, List, Sequence, TypedDict

from fi_instrumentation import register
from fi_instrumentation.fi_types import (
    EvalName,
    EvalSpanKind,
    EvalTag,
    EvalTagType,
    ProjectType,
)
from langchain.agents import AgentExecutor, create_openai_tools_agent
from langchain_community.tools.tavily_search import TavilySearchResults
from langchain_core.messages import BaseMessage, HumanMessage
from langchain_core.output_parsers.openai_functions import JsonOutputFunctionsParser
from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder
from langchain_experimental.tools import PythonREPLTool
from langchain_openai import ChatOpenAI
from langgraph.graph import END, StateGraph
from traceai_langchain import LangChainInstrumentor

# Configure trace provider with custom evaluation tags
eval_tags = [
    EvalTag(
        eval_name=EvalName.DETERMINISTIC_EVALS,
        value=EvalSpanKind.TOOL,
        type=EvalTagType.OBSERVATION_SPAN,
        config={
            "multi_choice": False,
            "choices": ["Yes", "No"],
            "rule_prompt": "Evaluate if the response is correct",
        },
        custom_eval_name="<custom_eval_name>",
    )
]

# Configure trace provider with custom evaluation tags
trace_provider = register(
    project_type=ProjectType.EXPERIMENT,
    eval_tags=eval_tags,
    project_name="FUTURE_AGI",
    project_version_name="v1",
)

# Initialize the LangChain instrumentor
LangChainInstrumentor().instrument(tracer_provider=trace_provider)

tavily_tool = TavilySearchResults(max_results=5)

# This executes code locally, which can be unsafe
python_repl_tool = PythonREPLTool()


@dataclass
class AgentConfig:
    """Configuration for individual agents"""

    name: str
    system_prompt: str
    tools: List[any]


class AgentState(TypedDict):
    """The agent state that flows through the graph"""

    messages: Annotated[Sequence[BaseMessage], operator.add]
    next: str


class AgentFactory:
    """Factory for creating and configuring agents"""

    def __init__(self, llm: ChatOpenAI):
        self.llm = llm

    def create_agent(self, config: AgentConfig) -> AgentExecutor:
        prompt = ChatPromptTemplate.from_messages(
            [
                ("system", config.system_prompt),
                MessagesPlaceholder(variable_name="messages"),
                MessagesPlaceholder(variable_name="agent_scratchpad"),
            ]
        )
        agent = create_openai_tools_agent(self.llm, config.tools, prompt)
        return AgentExecutor(agent=agent, tools=config.tools)


class SupervisorConfig:
    """Configuration for the supervisor agent"""

    def __init__(self, members: List[str]):
        self.members = members
        self.options = ["FINISH"] + members
        self.system_prompt = (
            "You are a supervisor tasked with managing a conversation between the"
            " following workers: {members}. Given the following user request,"
            " respond with the worker to act next. Each worker will perform a"
            " task and respond with their results and status. When finished,"
            " respond with FINISH."
        )
        self.function_def = {
            "name": "route",
            "description": "Select the next role.",
            "parameters": {
                "title": "routeSchema",
                "type": "object",
                "properties": {
                    "next": {
                        "title": "Next",
                        "anyOf": [{"enum": self.options}],
                    }
                },
                "required": ["next"],
            },
        }


class WorkflowBuilder:
    """Builds and configures the agent workflow"""

    def __init__(self, llm: ChatOpenAI):
        self.llm = llm
        self.agent_factory = AgentFactory(llm)

    def create_workflow(self, supervisor_config: SupervisorConfig) -> StateGraph:
        # Create supervisor chain
        supervisor_prompt = ChatPromptTemplate.from_messages(
            [
                ("system", supervisor_config.system_prompt),
                MessagesPlaceholder(variable_name="messages"),
                (
                    "system",
                    "Given the conversation above, who should act next?"
                    " Or should we FINISH? Select one of: {options}",
                ),
            ]
        ).partial(
            options=str(supervisor_config.options),
            members=", ".join(supervisor_config.members),
        )

        supervisor_chain = (
            supervisor_prompt
            | self.llm.bind_functions(
                functions=[supervisor_config.function_def], function_call="route"
            )
            | JsonOutputFunctionsParser()
        )

        # Create workflow
        workflow = StateGraph(AgentState)

        # Add nodes
        for member in supervisor_config.members:
            workflow.add_node(member, self._create_agent_node(member))

        workflow.add_node("supervisor", supervisor_chain)

        # Add edges
        for member in supervisor_config.members:
            workflow.add_edge(member, "supervisor")

        conditional_map = {k: k for k in supervisor_config.members}
        conditional_map["FINISH"] = END
        workflow.add_conditional_edges(
            "supervisor", lambda x: x["next"], conditional_map
        )

        workflow.set_entry_point("supervisor")
        return workflow.compile()

    def _create_agent_node(self, agent_type: str):
        """Create an agent node based on type"""
        if agent_type == "Researcher":
            agent = self.agent_factory.create_agent(
                AgentConfig(
                    name="Researcher",
                    system_prompt="You are a web researcher.",
                    tools=[TavilySearchResults(max_results=5)],
                )
            )
        elif agent_type == "Coder":
            agent = self.agent_factory.create_agent(
                AgentConfig(
                    name="Coder",
                    system_prompt="You may generate safe python code to analyze data and generate charts using matplotlib.",
                    tools=[PythonREPLTool()],
                )
            )
        else:
            raise ValueError(f"Unknown agent type: {agent_type}")

        return functools.partial(self._agent_node, agent=agent, name=agent_type)

    @staticmethod
    def _agent_node(state, agent, name):
        """Generic agent node function"""
        result = agent.invoke(state)
        return {"messages": [HumanMessage(content=result["output"], name=name)]}


def main():
    llm = ChatOpenAI(model="gpt-4")
    members = ["Researcher", "Coder"]

    # Build workflow
    builder = WorkflowBuilder(llm)
    supervisor_config = SupervisorConfig(members)
    graph = builder.create_workflow(supervisor_config)

    # Run example
    for s in graph.stream(
        {"messages": [HumanMessage(content="Write a brief research report on pikas.")]},
        {"recursion_limit": 100},
    ):
        if "__end__" not in s:
            print(s)


if __name__ == "__main__":
    main()
