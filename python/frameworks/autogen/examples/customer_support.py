"""AutoGen v0.4 customer support team example.

This example demonstrates:
- Building a multi-agent customer support system
- Specialized agents for different support tiers
- Tracing support ticket resolution workflow
"""

import asyncio
import os
from typing import Annotated
import json

from fi_instrumentation import register
from fi_instrumentation.fi_types import ProjectType
from traceai_autogen import instrument_autogen

# Setup tracing
trace_provider = register(
    project_type=ProjectType.OBSERVE,
    project_name="autogen-customer-support",
)
instrument_autogen(tracer_provider=trace_provider)

# Import AutoGen v0.4
from autogen_agentchat.agents import AssistantAgent
from autogen_agentchat.teams import SelectorGroupChat
from autogen_agentchat.conditions import MaxMessageTermination, TextMentionTermination
from autogen_ext.models.openai import OpenAIChatCompletionClient


# Simulated customer database
CUSTOMERS = {
    "C001": {
        "name": "John Doe",
        "plan": "Premium",
        "since": "2022-03-15",
        "tickets_resolved": 12,
    },
    "C002": {
        "name": "Jane Smith",
        "plan": "Basic",
        "since": "2023-08-01",
        "tickets_resolved": 3,
    },
}

# Knowledge base
KB_ARTICLES = {
    "password_reset": "To reset your password: 1) Go to Settings, 2) Click 'Security', 3) Select 'Reset Password', 4) Check your email for the reset link.",
    "billing": "Billing issues can be resolved by: 1) Checking your payment method in Settings > Billing, 2) Updating card information, 3) Contacting support for refunds.",
    "technical": "For technical issues: 1) Clear browser cache, 2) Try incognito mode, 3) Check system status at status.example.com, 4) Contact tech support if issue persists.",
}


def lookup_customer(
    customer_id: Annotated[str, "Customer ID to look up"],
) -> str:
    """Look up customer information."""
    if customer_id in CUSTOMERS:
        return json.dumps(CUSTOMERS[customer_id], indent=2)
    return json.dumps({"error": f"Customer {customer_id} not found"})


def search_knowledge_base(
    topic: Annotated[str, "Topic to search for in knowledge base"],
) -> str:
    """Search the support knowledge base."""
    topic_lower = topic.lower()
    for key, article in KB_ARTICLES.items():
        if key in topic_lower or topic_lower in key:
            return json.dumps({"topic": key, "article": article})

    return json.dumps(
        {
            "message": "No exact match found",
            "available_topics": list(KB_ARTICLES.keys()),
        }
    )


def create_ticket(
    customer_id: Annotated[str, "Customer ID"],
    issue: Annotated[str, "Issue description"],
    priority: Annotated[str, "Priority level: low, medium, high"] = "medium",
) -> str:
    """Create a support ticket."""
    ticket_id = f"TKT-{hash(issue) % 10000:04d}"
    return json.dumps(
        {
            "ticket_id": ticket_id,
            "customer_id": customer_id,
            "issue": issue,
            "priority": priority,
            "status": "created",
        }
    )


def escalate_ticket(
    ticket_id: Annotated[str, "Ticket ID to escalate"],
    reason: Annotated[str, "Reason for escalation"],
) -> str:
    """Escalate a ticket to higher tier support."""
    return json.dumps(
        {
            "ticket_id": ticket_id,
            "escalation_reason": reason,
            "status": "escalated",
            "assigned_to": "Tier 2 Support",
        }
    )


async def main():
    # Create model client
    model = OpenAIChatCompletionClient(
        model="gpt-4o",
        api_key=os.environ.get("OPENAI_API_KEY"),
    )

    # Tier 1: First-line support
    tier1_support = AssistantAgent(
        name="tier1_support",
        description="First-line support agent for common issues and basic troubleshooting.",
        model_client=model,
        tools=[lookup_customer, search_knowledge_base, create_ticket],
        system_message="""You are a Tier 1 customer support agent.

        Your responsibilities:
        1. Greet customers warmly
        2. Look up customer information
        3. Search knowledge base for solutions
        4. Handle common issues (password reset, billing questions)
        5. Create tickets for tracking
        6. Escalate complex issues to Tier 2

        Be empathetic and professional. If you can't resolve the issue,
        clearly explain that you'll escalate to specialized support.""",
    )

    # Tier 2: Technical support
    tier2_support = AssistantAgent(
        name="tier2_support",
        description="Technical support specialist for complex technical issues.",
        model_client=model,
        tools=[lookup_customer, escalate_ticket],
        system_message="""You are a Tier 2 technical support specialist.

        Your responsibilities:
        1. Handle complex technical issues
        2. Perform advanced troubleshooting
        3. Investigate system-level problems
        4. Provide detailed technical solutions
        5. Escalate to engineering if needed

        When you've resolved an issue, say RESOLVED.""",
    )

    # Customer success manager
    success_manager = AssistantAgent(
        name="success_manager",
        description="Customer success manager for premium customers and retention.",
        model_client=model,
        tools=[lookup_customer],
        system_message="""You are a Customer Success Manager.

        Your responsibilities:
        1. Handle premium customer concerns
        2. Ensure customer satisfaction
        3. Offer retention benefits when appropriate
        4. Follow up on resolved issues

        Focus on building long-term relationships.
        When the customer is satisfied, say RESOLVED.""",
    )

    # Create support team
    support_team = SelectorGroupChat(
        participants=[tier1_support, tier2_support, success_manager],
        model_client=model,
        termination_condition=MaxMessageTermination(max_messages=10)
        | TextMentionTermination("RESOLVED"),
    )

    # Simulate customer support scenarios
    scenarios = [
        {
            "customer_id": "C001",
            "issue": "I'm a premium customer (C001) and I've been having trouble logging in for the past 2 days. I've already tried resetting my password but it's not working.",
        },
        {
            "customer_id": "C002",
            "issue": "Hi, I'm customer C002. I was charged twice this month and I'd like a refund.",
        },
    ]

    for scenario in scenarios:
        print(f"\n{'=' * 70}")
        print(f"Support Ticket: {scenario['issue'][:60]}...")
        print("=" * 70)

        result = await support_team.run(task=scenario["issue"])

        print(f"\n--- Resolution Summary ---")
        print(f"Total messages: {len(result.messages)}")
        print(f"Stop reason: {result.stop_reason}")

        # Analyze agent participation
        agents_involved = set()
        for msg in result.messages:
            source = getattr(msg, "source", None)
            if source:
                agents_involved.add(source)

        print(f"Agents involved: {', '.join(agents_involved)}")


if __name__ == "__main__":
    asyncio.run(main())
