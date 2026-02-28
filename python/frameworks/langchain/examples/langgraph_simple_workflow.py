"""
LangGraph Simple Workflow Example with Tracing

Demonstrates LangGraph instrumentation with a simple state machine workflow.
This example shows:
- Graph topology capture
- Node execution tracing
- State transition tracking
- Conditional edge decisions

No external API keys required - uses simple Python functions.
"""

from typing import TypedDict, Literal
from fi_instrumentation import register
from fi_instrumentation.fi_types import ProjectType
from langgraph.graph import StateGraph, END
from traceai_langchain import LangChainInstrumentor, LangGraphInstrumentor


# Define the state schema
class WorkflowState(TypedDict):
    """State that flows through the workflow."""
    input: str
    step: int
    results: list
    status: str


# Configure trace provider
trace_provider = register(
    project_type=ProjectType.OBSERVE,
    project_name="langgraph_simple_example",
    session_name="workflow-demo"
)

# Initialize instrumentors - both LangChain and LangGraph
LangChainInstrumentor().instrument(tracer_provider=trace_provider)
LangGraphInstrumentor().instrument(tracer_provider=trace_provider)


# Define node functions
def process_input(state: WorkflowState) -> dict:
    """First node: Process the input."""
    print(f"Processing input: {state['input']}")
    return {
        "step": 1,
        "results": [f"Processed: {state['input']}"],
        "status": "processing"
    }


def validate_data(state: WorkflowState) -> dict:
    """Second node: Validate the processed data."""
    print(f"Validating data at step {state['step']}")
    is_valid = len(state['input']) > 3
    return {
        "step": 2,
        "results": state["results"] + [f"Validated: {is_valid}"],
        "status": "valid" if is_valid else "invalid"
    }


def transform_data(state: WorkflowState) -> dict:
    """Third node: Transform valid data."""
    print(f"Transforming data at step {state['step']}")
    transformed = state['input'].upper()
    return {
        "step": 3,
        "results": state["results"] + [f"Transformed: {transformed}"],
        "status": "transformed"
    }


def handle_error(state: WorkflowState) -> dict:
    """Error handling node for invalid data."""
    print(f"Handling error for invalid data")
    return {
        "step": 3,
        "results": state["results"] + ["Error: Input too short"],
        "status": "error"
    }


def finalize(state: WorkflowState) -> dict:
    """Final node: Finalize the workflow."""
    print(f"Finalizing workflow with status: {state['status']}")
    return {
        "step": 4,
        "results": state["results"] + ["Workflow complete"],
        "status": "completed"
    }


# Define conditional edge function
def route_after_validation(state: WorkflowState) -> Literal["transform", "error"]:
    """Decide whether to transform or handle error based on validation."""
    if state["status"] == "valid":
        return "transform"
    return "error"


def build_workflow() -> StateGraph:
    """Build the workflow graph."""
    # Create the graph
    workflow = StateGraph(WorkflowState)

    # Add nodes
    workflow.add_node("process", process_input)
    workflow.add_node("validate", validate_data)
    workflow.add_node("transform", transform_data)
    workflow.add_node("error", handle_error)
    workflow.add_node("finalize", finalize)

    # Add edges
    workflow.add_edge("process", "validate")
    workflow.add_conditional_edges(
        "validate",
        route_after_validation,
        {
            "transform": "transform",
            "error": "error"
        }
    )
    workflow.add_edge("transform", "finalize")
    workflow.add_edge("error", "finalize")
    workflow.add_edge("finalize", END)

    # Set entry point
    workflow.set_entry_point("process")

    return workflow


def main():
    """Run the workflow examples."""
    print("=" * 60)
    print("LangGraph Simple Workflow Example with Tracing")
    print("=" * 60)

    # Build and compile the graph
    workflow = build_workflow()
    app = workflow.compile()

    # Example 1: Valid input (will be transformed)
    print("\n--- Example 1: Valid Input ---")
    result = app.invoke({
        "input": "Hello World",
        "step": 0,
        "results": [],
        "status": "pending"
    })
    print(f"Final state: {result}")

    # Example 2: Invalid input (will trigger error handling)
    print("\n--- Example 2: Invalid Input ---")
    result = app.invoke({
        "input": "Hi",  # Too short - will fail validation
        "step": 0,
        "results": [],
        "status": "pending"
    })
    print(f"Final state: {result}")

    # Example 3: Stream mode
    print("\n--- Example 3: Stream Mode ---")
    for chunk in app.stream({
        "input": "Streaming Test",
        "step": 0,
        "results": [],
        "status": "pending"
    }):
        print(f"Stream chunk: {chunk}")

    print("\n" + "=" * 60)
    print("Workflow examples completed!")
    print("Check your trace dashboard for detailed execution traces.")
    print("=" * 60)


if __name__ == "__main__":
    main()
