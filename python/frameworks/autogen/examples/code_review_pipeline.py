"""AutoGen v0.4 code review pipeline example.

This example demonstrates:
- Automated code review workflow with multiple specialized agents
- Static analysis tool integration
- Tracing the complete review pipeline
"""

import asyncio
import os
from typing import Annotated
import json
import re

from fi_instrumentation import register
from fi_instrumentation.fi_types import ProjectType
from traceai_autogen import instrument_autogen

# Setup tracing
trace_provider = register(
    project_type=ProjectType.OBSERVE,
    project_name="autogen-code-review",
)
instrument_autogen(tracer_provider=trace_provider)

# Import AutoGen v0.4
from autogen_agentchat.agents import AssistantAgent
from autogen_agentchat.teams import RoundRobinGroupChat
from autogen_agentchat.conditions import MaxMessageTermination, TextMentionTermination
from autogen_ext.models.openai import OpenAIChatCompletionClient


def analyze_code_style(
    code: Annotated[str, "Python code to analyze"],
) -> str:
    """Analyze code for style issues (simulated linter)."""
    issues = []

    lines = code.split("\n")
    for i, line in enumerate(lines, 1):
        # Check line length
        if len(line) > 79:
            issues.append(f"Line {i}: Line too long ({len(line)} > 79 characters)")

        # Check for trailing whitespace
        if line.endswith(" ") or line.endswith("\t"):
            issues.append(f"Line {i}: Trailing whitespace")

        # Check for missing spaces around operators
        if re.search(r"[a-zA-Z0-9][=+\-*/][a-zA-Z0-9]", line):
            issues.append(
                f"Line {i}: Missing spaces around operator"
            )

    return json.dumps(
        {
            "total_issues": len(issues),
            "issues": issues[:10],  # Limit output
            "style_score": max(0, 100 - len(issues) * 5),
        },
        indent=2,
    )


def check_security(
    code: Annotated[str, "Python code to check for security issues"],
) -> str:
    """Check code for common security vulnerabilities."""
    vulnerabilities = []

    # Check for eval usage
    if "eval(" in code:
        vulnerabilities.append(
            {
                "severity": "HIGH",
                "issue": "Use of eval() - potential code injection",
                "recommendation": "Use ast.literal_eval() for safe evaluation",
            }
        )

    # Check for exec usage
    if "exec(" in code:
        vulnerabilities.append(
            {
                "severity": "HIGH",
                "issue": "Use of exec() - potential code injection",
                "recommendation": "Avoid exec() or sanitize input carefully",
            }
        )

    # Check for SQL injection patterns
    if re.search(r'f".*SELECT.*{', code) or re.search(r"f'.*SELECT.*{", code):
        vulnerabilities.append(
            {
                "severity": "CRITICAL",
                "issue": "Potential SQL injection via f-string",
                "recommendation": "Use parameterized queries",
            }
        )

    # Check for hardcoded secrets
    if re.search(r'(password|secret|api_key)\s*=\s*["\']', code, re.IGNORECASE):
        vulnerabilities.append(
            {
                "severity": "MEDIUM",
                "issue": "Potential hardcoded secret",
                "recommendation": "Use environment variables or secret management",
            }
        )

    return json.dumps(
        {
            "total_vulnerabilities": len(vulnerabilities),
            "vulnerabilities": vulnerabilities,
            "security_score": 100 if not vulnerabilities else max(0, 100 - len(vulnerabilities) * 25),
        },
        indent=2,
    )


def calculate_complexity(
    code: Annotated[str, "Python code to analyze complexity"],
) -> str:
    """Calculate code complexity metrics (simplified)."""
    lines = code.split("\n")
    non_empty_lines = [l for l in lines if l.strip() and not l.strip().startswith("#")]

    # Count functions
    functions = len(re.findall(r"^\s*def\s+", code, re.MULTILINE))

    # Count classes
    classes = len(re.findall(r"^\s*class\s+", code, re.MULTILINE))

    # Count conditionals (rough cyclomatic complexity indicator)
    conditionals = (
        len(re.findall(r"\bif\b", code))
        + len(re.findall(r"\belif\b", code))
        + len(re.findall(r"\bfor\b", code))
        + len(re.findall(r"\bwhile\b", code))
        + len(re.findall(r"\btry\b", code))
    )

    return json.dumps(
        {
            "lines_of_code": len(non_empty_lines),
            "functions": functions,
            "classes": classes,
            "conditionals": conditionals,
            "estimated_complexity": "low" if conditionals < 5 else "medium" if conditionals < 10 else "high",
        },
        indent=2,
    )


async def main():
    # Create model client
    model = OpenAIChatCompletionClient(
        model="gpt-4o",
        api_key=os.environ.get("OPENAI_API_KEY"),
    )

    # Code style reviewer
    style_reviewer = AssistantAgent(
        name="style_reviewer",
        model_client=model,
        tools=[analyze_code_style],
        system_message="""You are a code style expert.
        Analyze code for:
        - PEP 8 compliance
        - Naming conventions
        - Code formatting
        - Documentation quality
        Use the analyze_code_style tool and provide actionable feedback.""",
    )

    # Security reviewer
    security_reviewer = AssistantAgent(
        name="security_reviewer",
        model_client=model,
        tools=[check_security],
        system_message="""You are a security expert.
        Analyze code for:
        - Injection vulnerabilities
        - Hardcoded secrets
        - Unsafe operations
        - Authentication/authorization issues
        Use the check_security tool and flag any concerns.""",
    )

    # Architecture reviewer
    architecture_reviewer = AssistantAgent(
        name="architecture_reviewer",
        model_client=model,
        tools=[calculate_complexity],
        system_message="""You are a software architect.
        Analyze code for:
        - Code complexity
        - Design patterns
        - Modularity and maintainability
        - Scalability concerns
        Use the calculate_complexity tool and provide improvement suggestions.
        Say APPROVED when review is complete.""",
    )

    # Create review team
    review_team = RoundRobinGroupChat(
        participants=[style_reviewer, security_reviewer, architecture_reviewer],
        termination_condition=MaxMessageTermination(max_messages=6)
        | TextMentionTermination("APPROVED"),
    )

    # Code to review
    code_to_review = '''
def process_user_data(user_id, data):
    """Process user data from input."""
    password = "admin123"  # TODO: Remove this

    query = f"SELECT * FROM users WHERE id = {user_id}"
    result = eval(data.get("expression", "1+1"))

    if result>0:
        return {"status":"success","data":result}
    else:
        return {"status": "error"}
'''

    print("=" * 70)
    print("CODE REVIEW PIPELINE")
    print("=" * 70)
    print("\nCode to review:")
    print("-" * 40)
    print(code_to_review)
    print("-" * 40)

    result = await review_team.run(
        task=f"Please review the following Python code:\n\n```python\n{code_to_review}\n```"
    )

    print("\n--- Review Complete ---")
    print(f"Total review comments: {len(result.messages)}")

    # Summary by reviewer
    for msg in result.messages:
        source = getattr(msg, "source", "unknown")
        content = getattr(msg, "content", "")
        if source != "user" and content:
            print(f"\n[{source}]:")
            print(content[:500] + "..." if len(content) > 500 else content)


if __name__ == "__main__":
    asyncio.run(main())
