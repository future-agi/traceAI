"""Human-in-the-Loop (HITL) Example with TraceAI LangGraph Instrumentation.

This example demonstrates TraceAI's comprehensive interrupt/resume tracing,
which is a key competitive advantage over Langfuse (which has this broken).

Key features demonstrated:
1. Intentional interrupts are marked as OK status (not ERROR)
2. Resume spans link back to interrupt spans for unified tracing
3. Human decisions are captured with approver info and feedback
4. Full state preservation across interrupt/resume cycles

Usage (with local test backend):
    # Terminal 1: Start test infrastructure (from core-backend)
    cd /path/to/core-backend
    bin/sdk-test setup
    bin/sdk-test backend

    # Terminal 2: Run this example (from langchain directory)
    export $(cat /path/to/core-backend/.env.test.local | xargs)
    export FI_API_KEY=test_api_key_12345
    export FI_SECRET_KEY=test_secret_key_67890
    export FI_BASE_URL=http://localhost:8001
    python examples/langgraph_human_in_the_loop.py

Usage (with production):
    export OPENAI_API_KEY=your-key
    export FI_API_KEY=your-futureagi-key
    export FI_SECRET_KEY=your-futureagi-secret
    python examples/langgraph_human_in_the_loop.py
"""

import os
from typing import Annotated, TypedDict, Literal
from datetime import datetime

# OpenTelemetry setup
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

# LangGraph imports
from langgraph.graph import StateGraph, END
from langgraph.checkpoint.memory import MemorySaver

# TraceAI imports
from traceai_langchain import LangGraphInstrumentor


# State definition
class ApprovalState(TypedDict):
    """State for an approval workflow."""
    request_id: str
    request_type: str
    amount: float
    requester: str
    status: str
    approval_notes: str
    approved_by: str
    messages: Annotated[list, lambda x, y: x + y]  # Reducer for messages


def setup_tracing():
    """Set up OpenTelemetry tracing with FutureAGI."""
    # Create tracer provider
    provider = TracerProvider()

    # Determine endpoint based on environment
    # For local testing: FI_BASE_URL=http://localhost:8001
    # For production: uses default FutureAGI endpoint
    base_url = os.environ.get("FI_BASE_URL", "https://trace.futureagi.com")
    endpoint = f"{base_url}/v1/traces"

    # Configure exporter
    exporter = OTLPSpanExporter(
        endpoint=endpoint,
        headers={
            "Authorization": f"Bearer {os.environ.get('FI_API_KEY', '')}",
            "x-secret-key": os.environ.get("FI_SECRET_KEY", ""),
        }
    )

    # Add processor
    provider.add_span_processor(BatchSpanProcessor(exporter))

    # Set as global provider
    trace.set_tracer_provider(provider)

    print(f"[TRACE] Exporting to: {endpoint}")
    return provider


def analyze_request(state: ApprovalState) -> ApprovalState:
    """Analyze the approval request and add risk assessment."""
    messages = [f"[{datetime.now().isoformat()}] Analyzing request {state['request_id']}..."]

    # Simple risk assessment based on amount
    if state["amount"] > 10000:
        risk_level = "HIGH"
        messages.append(f"High value request: ${state['amount']:,.2f} - requires senior approval")
    elif state["amount"] > 1000:
        risk_level = "MEDIUM"
        messages.append(f"Medium value request: ${state['amount']:,.2f} - standard approval process")
    else:
        risk_level = "LOW"
        messages.append(f"Low value request: ${state['amount']:,.2f} - may qualify for auto-approval")

    return {
        "messages": messages,
        "status": f"analyzed_{risk_level.lower()}",
    }


def human_review(state: ApprovalState) -> ApprovalState:
    """Human review checkpoint - this is where we interrupt for HITL.

    In a real application, this would use langgraph's interrupt() function.
    For demo purposes, we simulate the interrupt/resume pattern.
    """
    messages = [f"[{datetime.now().isoformat()}] Awaiting human review for request {state['request_id']}"]

    # In production, you would call:
    # from langgraph.types import interrupt
    # result = interrupt({"request": state, "prompt": "Please approve or reject"})

    # For this example, we simulate approval
    return {
        "messages": messages,
        "status": "pending_approval",
    }


def process_approval(state: ApprovalState) -> ApprovalState:
    """Process the approval decision."""
    messages = [f"[{datetime.now().isoformat()}] Processing approval decision..."]

    if state["status"] == "approved":
        messages.append(f"Request {state['request_id']} APPROVED by {state.get('approved_by', 'system')}")
        if state.get("approval_notes"):
            messages.append(f"Notes: {state['approval_notes']}")
    else:
        messages.append(f"Request {state['request_id']} REJECTED")
        if state.get("approval_notes"):
            messages.append(f"Rejection reason: {state['approval_notes']}")

    return {
        "messages": messages,
        "status": f"completed_{state['status']}",
    }


def should_require_approval(state: ApprovalState) -> Literal["human_review", "auto_approve"]:
    """Determine if human approval is required."""
    # Auto-approve low value requests
    if state["amount"] <= 100:
        return "auto_approve"
    return "human_review"


def auto_approve(state: ApprovalState) -> ApprovalState:
    """Auto-approve low-value requests."""
    return {
        "messages": [f"[{datetime.now().isoformat()}] Auto-approved low-value request"],
        "status": "approved",
        "approved_by": "AUTO_APPROVAL_SYSTEM",
    }


def build_approval_workflow():
    """Build the approval workflow graph."""
    # Create graph
    workflow = StateGraph(ApprovalState)

    # Add nodes
    workflow.add_node("analyze", analyze_request)
    workflow.add_node("human_review", human_review)
    workflow.add_node("auto_approve", auto_approve)
    workflow.add_node("process", process_approval)

    # Set entry point
    workflow.set_entry_point("analyze")

    # Add edges
    workflow.add_conditional_edges(
        "analyze",
        should_require_approval,
        {
            "human_review": "human_review",
            "auto_approve": "auto_approve",
        }
    )
    workflow.add_edge("human_review", "process")
    workflow.add_edge("auto_approve", "process")
    workflow.add_edge("process", END)

    return workflow


def main():
    """Main function demonstrating HITL workflow with TraceAI tracing."""
    print("=" * 60)
    print("LangGraph Human-in-the-Loop Example with TraceAI")
    print("=" * 60)

    # Set up tracing
    provider = setup_tracing()

    # Instrument LangGraph
    instrumentor = LangGraphInstrumentor()
    instrumentor.instrument(tracer_provider=provider)

    print("\n[OK] TraceAI instrumentation enabled")

    # Build workflow with checkpointing
    workflow = build_approval_workflow()
    memory = MemorySaver()
    app = workflow.compile(checkpointer=memory)

    print("[OK] Approval workflow compiled with checkpointing")

    # Example 1: Low-value request (auto-approved)
    print("\n" + "-" * 40)
    print("Example 1: Low-value request (auto-approval)")
    print("-" * 40)

    config_1 = {"configurable": {"thread_id": "request-001"}}
    result_1 = app.invoke(
        {
            "request_id": "REQ-001",
            "request_type": "office_supplies",
            "amount": 50.00,
            "requester": "alice@company.com",
            "status": "new",
            "approval_notes": "",
            "approved_by": "",
            "messages": [],
        },
        config_1,
    )

    print(f"Final status: {result_1['status']}")
    print(f"Approved by: {result_1['approved_by']}")
    for msg in result_1["messages"]:
        print(f"  {msg}")

    # Example 2: High-value request (requires human approval)
    print("\n" + "-" * 40)
    print("Example 2: High-value request (human approval required)")
    print("-" * 40)

    config_2 = {"configurable": {"thread_id": "request-002"}}

    # Initial submission
    result_2 = app.invoke(
        {
            "request_id": "REQ-002",
            "request_type": "equipment",
            "amount": 15000.00,
            "requester": "bob@company.com",
            "status": "new",
            "approval_notes": "",
            "approved_by": "",
            "messages": [],
        },
        config_2,
    )

    print(f"Status after analysis: {result_2['status']}")
    for msg in result_2["messages"]:
        print(f"  {msg}")

    # Record the interrupt event using TraceAI
    # This is the key differentiator - we mark interrupts as intentional (OK status)
    interrupt_info = instrumentor.on_interrupt(
        thread_id="request-002",
        node_name="human_review",
        reason="High-value equipment purchase requires senior approval",
        state=result_2,
        is_intentional=True,  # Marked as intentional, NOT an error
    )

    if interrupt_info:
        print(f"\n[INTERRUPT] Workflow paused for human review")
        print(f"  Interrupt ID: {interrupt_info.interrupt_id}")
        print(f"  Reason: {interrupt_info.reason}")

    # Simulate human review process
    print("\n... Human reviewing request ...")
    print("... Manager checks budget and approves ...")

    # Record the human decision
    decision = instrumentor.record_human_decision(
        decision="approved",
        thread_id="request-002",
        approver_id="manager@company.com",
        feedback="Approved after budget review. Equipment is necessary for Q2 project.",
    )

    if decision:
        print(f"\n[DECISION] Human decision recorded")
        print(f"  Decision: {decision.decision}")
        print(f"  Approver: {decision.approver_id}")

    # Record resume and continue
    resume_info = instrumentor.on_resume(
        thread_id="request-002",
        resume_input={
            "status": "approved",
            "approved_by": "manager@company.com",
            "approval_notes": "Approved after budget review. Equipment is necessary for Q2 project.",
        },
    )

    if resume_info:
        print(f"\n[RESUME] Workflow resumed")
        print(f"  Resume ID: {resume_info.resume_id}")
        if resume_info.interrupt_id:
            print(f"  Linked to interrupt: {resume_info.interrupt_id}")

    # Continue execution with the approval
    result_2_continued = app.invoke(
        {
            **result_2,
            "status": "approved",
            "approved_by": "manager@company.com",
            "approval_notes": "Approved after budget review. Equipment is necessary for Q2 project.",
        },
        config_2,
    )

    print(f"\nFinal status: {result_2_continued['status']}")
    for msg in result_2_continued["messages"][-2:]:  # Last 2 messages
        print(f"  {msg}")

    # Show interrupt/resume statistics
    print("\n" + "-" * 40)
    print("Interrupt/Resume Statistics")
    print("-" * 40)

    stats = instrumentor.get_interrupt_stats()
    if "error" not in stats:
        print(f"Total interrupts: {stats.get('total_interrupts', 0)}")
        print(f"Total resumes: {stats.get('total_resumes', 0)}")
        print(f"Pending interrupts: {stats.get('pending_interrupts', 0)}")
        print(f"Human decisions: {stats.get('total_human_decisions', 0)}")

        if stats.get("decisions_by_type"):
            print("\nDecisions by type:")
            for decision_type, count in stats["decisions_by_type"].items():
                print(f"  {decision_type}: {count}")

    # Show cost statistics (if any LLM calls were tracked)
    cost_stats = instrumentor.get_cost_stats()
    if "error" not in cost_stats:
        print("\n" + "-" * 40)
        print("Cost Statistics")
        print("-" * 40)
        print(f"Total cost: ${cost_stats.get('total_cost_usd', 0):.4f}")

    print("\n" + "=" * 60)
    print("Example completed!")
    print("=" * 60)
    print("\nKey TraceAI Advantages Demonstrated:")
    print("1. Interrupts marked as intentional (OK status, not ERROR)")
    print("2. Resume spans link back to interrupt spans")
    print("3. Human decisions captured with full context")
    print("4. Unified trace view across interrupt/resume cycles")
    print("\nView traces at: https://app.futureagi.com")


if __name__ == "__main__":
    main()
