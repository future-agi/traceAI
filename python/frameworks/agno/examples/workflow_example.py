"""Agno workflow example with TraceAI instrumentation.

This example demonstrates how to set up tracing for complex workflows
with multiple steps and state management.
"""

import os

# Setup tracing BEFORE importing Agno
from fi_instrumentation import register
from fi_instrumentation.fi_types import ProjectType
from traceai_agno import configure_agno_tracing, create_trace_context

# Initialize TraceAI
trace_provider = register(
    project_type=ProjectType.OBSERVE,
    project_name="agno-workflow-example",
)

# Configure Agno to use TraceAI
configure_agno_tracing(tracer_provider=trace_provider)

# Now import Agno modules
from agno.agent import Agent
from agno.models.openai import OpenAIChat
from agno.workflow import Workflow


def main():
    """Run a workflow example with multiple steps."""
    # Create agents for different workflow steps
    analyzer = Agent(
        name="Analyzer",
        model=OpenAIChat(id="gpt-4"),
        description="Analyzes input and extracts key information.",
        instructions=["Analyze the input carefully", "Extract key points and themes"],
    )

    processor = Agent(
        name="Processor",
        model=OpenAIChat(id="gpt-4"),
        description="Processes analyzed data and generates insights.",
        instructions=["Use the analysis to generate insights", "Identify patterns"],
    )

    summarizer = Agent(
        name="Summarizer",
        model=OpenAIChat(id="gpt-4"),
        description="Creates concise summaries of processed information.",
        instructions=["Create clear, concise summaries", "Highlight key findings"],
    )

    # Create a workflow
    class DataProcessingWorkflow(Workflow):
        """A workflow for processing and summarizing data."""

        def __init__(self):
            super().__init__(name="DataProcessingWorkflow")
            self.analyzer = analyzer
            self.processor = processor
            self.summarizer = summarizer

        def run(self, input_data: str) -> str:
            """Execute the workflow steps."""
            # Step 1: Analyze
            print("Step 1: Analyzing input...")
            analysis = self.analyzer.run(f"Analyze this data: {input_data}")

            # Step 2: Process
            print("Step 2: Processing analysis...")
            insights = self.processor.run(f"Process this analysis: {analysis.content}")

            # Step 3: Summarize
            print("Step 3: Creating summary...")
            summary = self.summarizer.run(f"Summarize these insights: {insights.content}")

            return summary.content

    # Create trace context for this workflow run
    context = create_trace_context(
        session_id="workflow-session-001",
        user_id="user-123",
        tags=["demo", "workflow"],
        metadata={"workflow_type": "data_processing"},
    )

    print("Trace Context:", context)
    print("=" * 50)

    # Run the workflow
    workflow = DataProcessingWorkflow()

    input_data = """
    Recent trends in artificial intelligence show significant growth in several areas:
    1. Large Language Models have become more capable and accessible
    2. Computer vision applications are being deployed in healthcare
    3. Autonomous systems are advancing in transportation
    4. AI ethics and safety research is gaining importance
    """

    print(f"\nInput Data:\n{input_data.strip()}")
    print("-" * 50)

    result = workflow.run(input_data)
    print(f"\nWorkflow Result:\n{result}")


if __name__ == "__main__":
    main()
