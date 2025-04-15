from crewai import LLM, Agent, Crew, Process, Task
from crewai_tools import SerperDevTool
from fi_instrumentation import register
from fi_instrumentation.fi_types import (
    EvalName,
    EvalSpanKind,
    EvalTag,
    EvalTagType,
    ProjectType,
)
from traceai_crewai import CrewAIInstrumentor

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

# Initialize the Crew AI instrumentor
CrewAIInstrumentor().instrument(tracer_provider=trace_provider)


def story_example():
    # Configure GPT-4 LLM
    llm = LLM(
        model="gpt-4",
        temperature=0.8,
        max_tokens=150,
        top_p=0.9,
        frequency_penalty=0.1,
        presence_penalty=0.1,
        stop=["END"],
        seed=42,
    )

    writer = Agent(
        role="Writer",
        goal="Write creative stories",
        backstory="You are a creative writer with a passion for storytelling",
        allow_delegation=False,
        llm=llm,
    )

    writing_task = Task(
        description="Write a short story about a magical forest",
        agent=writer,
        expected_output="A short story about a magical forest",
    )

    crew = Crew(agents=[writer], tasks=[writing_task])

    # Execute the crew
    result = crew.kickoff()


def tool_use_example():
    llm = LLM(
        model="claude-3-5-sonnet-20240620",
        temperature=0.7,
        max_tokens=1000,
        top_p=0.9,
        frequency_penalty=0.1,
        presence_penalty=0.1,
        seed=42,
    )

    # Create multiple specialized agents
    researcher = Agent(
        role="Research Analyst",
        goal="Gather and analyze information about given topics",
        backstory="You are an experienced researcher with expertise in data analysis and fact-finding",
        allow_delegation=True,
        llm=llm,
    )

    writer = Agent(
        role="Creative Writer",
        goal="Transform research and ideas into engaging content",
        backstory="You are a skilled storyteller with experience in various writing styles",
        # allow_delegation=True,
        llm=llm,
    )

    editor = Agent(
        role="Content Editor",
        goal="Review and improve written content",
        backstory="You are a detail-oriented editor with a keen eye for quality and consistency",
        allow_delegation=True,
        llm=llm,
    )

    # Create a series of interconnected tasks
    research_task = Task(
        description="""
        Research the following topics and provide key findings:
        1. The history of magical forests in folklore
        2. Common mythical creatures associated with forests
        3. Cultural significance of enchanted woods
        Compile your findings in a structured format.
        """,
        agent=researcher,
        expected_output="Detailed research notes on magical forests and related mythology",
    )

    writing_task = Task(
        description="""
        Using the research provided, write a 500-word story that includes:
        - A mysterious forest setting
        - At least two mythical creatures
        - A compelling plot with a clear beginning, middle, and end
        Consider the cultural elements from the research.
        """,
        agent=writer,
        context=[research_task],
        expected_output="An engaging story incorporating research elements",
    )

    editing_task = Task(
        description="""
        Review the story and improve it by:
        1. Checking for consistency in plot and character development
        2. Enhancing descriptive language
        3. Ensuring proper pacing
        4. Correcting any grammatical or stylistic issues
        Provide specific feedback and improvements.
        """,
        agent=editor,
        context=[writing_task],
        expected_output="Edited version of the story with improvements",
    )

    # Create crew with sequential process
    crew = Crew(
        agents=[researcher, writer, editor],
        tasks=[research_task, writing_task, editing_task],
        process=Process.sequential,  # Tasks will be executed in order
        # verbose=True,  # Enable detailed logging of agent interactions
    )

    # Execute the crew
    result = crew.kickoff()


def tool_use_example_2():

    tool = SerperDevTool(
        country="fr",
        locale="fr",
        location="Paris, Paris, Ile-de-France, France",
        n_results=2,
    )

    # Create an agent that will use the tool
    agent = Agent(
        role="Researcher",
        goal="Search for information about the Olympics",
        backstory="I am a researcher focused on Olympic Games information",
        tools=[tool],
    )

    # Create a task for the agent
    task = Task(
        description="Search for information about 'Jeux Olympiques'",
        agent=agent,
        expected_output="Information about the Olympic Games in French",
    )

    # Execute the task
    result = agent.execute_task(task)


if __name__ == "__main__":
    story_example()
    tool_use_example()
    tool_use_example_2()
