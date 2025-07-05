from typing import Annotated

from fi_instrumentation import register
from fi_instrumentation.fi_types import (
    EvalName,
    EvalSpanKind,
    EvalTag,
    EvalTagType,
    ProjectType,
)
from langchain_community.agent_toolkits import SQLDatabaseToolkit
from langchain_community.utilities import SQLDatabase
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.runnables import RunnableLambda, RunnableWithFallbacks
from langchain_core.tools import tool
from langchain_openai import ChatOpenAI
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import AnyMessage, add_messages
from langgraph.prebuilt import ToolNode
from traceai_langchain import LangChainInstrumentor
from typing_extensions import TypedDict

# Initialize database
db = SQLDatabase.from_uri("postgresql://user:password@localhost:5432/tfc")

# Create toolkit and get basic tools
toolkit = SQLDatabaseToolkit(db=db, llm=ChatOpenAI(model="gpt-4o"))
tools = toolkit.get_tools()

list_tables_tool = next(tool for tool in tools if tool.name == "sql_db_list_tables")
get_schema_tool = next(tool for tool in tools if tool.name == "sql_db_schema")


# Define custom query tool
@tool
def db_query_tool(query: str) -> str:
    """Execute a SQL query against the database and get back the result."""
    result = db.run_no_throw(query)
    if not result:
        return "Error: Query failed. Please rewrite your query and try again."
    return result


# Define utility function for tool node fallbacks
def create_tool_node_with_fallback(tools: list) -> RunnableWithFallbacks:
    """Create a ToolNode with error handling fallback."""
    return ToolNode(tools).with_fallbacks(
        [RunnableLambda(handle_tool_error)], exception_key="error"
    )


def handle_tool_error(state):
    error = state.get("error")
    tool_calls = state["messages"][-1].tool_calls
    return {
        "messages": [
            ToolMessage(
                content=f"Error: {repr(error)}\n please fix your mistakes.",
                tool_call_id=tc["id"],
            )
            for tc in tool_calls
        ]
    }


# Define state and workflow
class State(TypedDict):
    messages: Annotated[list[AnyMessage], add_messages]


workflow = StateGraph(State)


# Define nodes
def first_tool_call(state: State):
    return {
        "messages": [
            AIMessage(
                content="Let me help you with that query. First, I'll check what tables are available.",
                tool_calls=[
                    {
                        "name": "sql_db_list_tables",
                        "args": {},
                        "id": "tool_list_tables",
                    }
                ],
            )
        ]
    }


# Add a handler for list tables response
def handle_list_tables(state: State):
    tables = next(
        msg.content
        for msg in reversed(state["messages"])
        if isinstance(msg, ToolMessage)
    )
    return {
        "messages": [
            AIMessage(
                content=f"I see the tables. Let me check their schema.",
                tool_calls=[
                    {
                        "name": "sql_db_schema",
                        "args": {},
                        "id": "tool_schema",
                    }
                ],
            )
        ]
    }


# Add a handler for schema response
def handle_schema(state: State):
    schema = next(
        msg.content
        for msg in reversed(state["messages"])
        if isinstance(msg, ToolMessage)
    )
    return {
        "messages": [
            AIMessage(
                content="I have the schema information. Now I'll create a query to answer your question.",
                tool_calls=[],  # No tool calls here as we move to query creation
            )
        ]
    }


# Define query creation prompt
query_creation_prompt = ChatPromptTemplate.from_messages(
    [
        (
            "system",
            """You are a SQL expert. Using the available tables and their schema below, create a SQL query to answer the user's question.
    Return only the SQL query without any explanation.

    Available Tables:
    {tables}

    Schema Details:
    {schema}""",
        ),
        ("user", "Question: {question}"),
    ]
)


# Add query creation function
def create_query(state: State):
    # Get the schema and tables from tool messages
    schema = None
    tables = None

    for msg in state["messages"]:
        if isinstance(msg, ToolMessage):
            if hasattr(msg, "tool_call_id"):
                if msg.tool_call_id == "tool_schema":
                    schema = msg.content
                elif msg.tool_call_id == "tool_list_tables":
                    tables = msg.content

    if schema is None or tables is None:
        raise ValueError(
            "Could not find required schema and tables information in message history"
        )

    # Get the original user question
    question = None
    for msg in state["messages"]:
        if msg.type == "human":
            question = msg.content
            break

    if question is None:
        raise ValueError("Could not find user question in message history")

    # Create the query using the LLM with the enhanced prompt
    llm = ChatOpenAI(model="gpt-4")
    query_response = llm.invoke(
        query_creation_prompt.format(tables=tables, schema=schema, question=question)
    )

    # Add some logging to help debug
    print(f"Generated SQL Query: {query_response.content}")

    return {
        "messages": [
            AIMessage(
                content=f"I'll execute this query to answer your question:\n{query_response.content}",
                tool_calls=[
                    {
                        "name": "db_query_tool",
                        "args": {"query": query_response.content},
                        "id": "tool_query",
                    }
                ],
            )
        ]
    }


# Update workflow nodes and edges
workflow.add_node("first_tool_call", first_tool_call)
workflow.add_node(
    "list_tables_tool", create_tool_node_with_fallback([list_tables_tool])
)
workflow.add_node("handle_list_tables", handle_list_tables)
workflow.add_node("get_schema_tool", create_tool_node_with_fallback([get_schema_tool]))
workflow.add_node("handle_schema", handle_schema)
workflow.add_node("create_query", create_query)
workflow.add_node("execute_query", create_tool_node_with_fallback([db_query_tool]))

# Update edges
workflow.add_edge(START, "first_tool_call")
workflow.add_edge("first_tool_call", "list_tables_tool")
workflow.add_edge("list_tables_tool", "handle_list_tables")
workflow.add_edge("handle_list_tables", "get_schema_tool")
workflow.add_edge("get_schema_tool", "handle_schema")
workflow.add_edge("handle_schema", "create_query")
workflow.add_edge("create_query", "execute_query")
workflow.add_edge("execute_query", END)

# Compile workflow
app = workflow.compile()

# Configure trace provider with custom evaluation tags
eval_tags = [
    EvalTag(
        eval_name=EvalName.DETERMINISTIC_EVALS,
        value=EvalSpanKind.TOOL,
        type=EvalTagType.OBSERVATION_SPAN,
        config={
            "multi_choice": False,
            "choices": ["Yes", "No"],
            "rule_prompt": "Evaluate if the SQL query execution was successful",
        },
    )
]


def setup_instrumentation():
    """Configure and initialize OpenTelemetry instrumentation."""
    trace_provider = register(
        project_type=ProjectType.OBSERVE,
        project_name="SQL_AGENT",
        project_version_name="V1",
        # eval_tags=eval_tags,
    )
    LangChainInstrumentor().instrument(tracer_provider=trace_provider)
    return trace_provider


# Example usage
if __name__ == "__main__":
    # Initialize instrumentation
    setup_instrumentation()

    # Example query
    result = app.invoke(
        {
            "messages": [
                ("user", "How many observations spans are there in the database?")
            ]
        }
    )
    print(result)
