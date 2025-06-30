import math
from collections import deque
from typing import Optional

from fi_instrumentation import register
from fi_instrumentation.fi_types import ProjectType
from langchain_community.tools.tavily_search import TavilySearchResults
from langchain_community.utilities.tavily_search import TavilySearchAPIWrapper
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage
from langchain_core.output_parsers.openai_tools import (
    JsonOutputToolsParser,
    PydanticToolsParser,
)
from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder
from langchain_core.runnables import chain as as_runnable
from langchain_openai import ChatOpenAI
from langgraph.graph import END, START, StateGraph
from langgraph.prebuilt import ToolNode
from pydantic import BaseModel, Field
from traceai_langchain import LangChainInstrumentor
from typing_extensions import TypedDict

# Configure trace provider with custom evaluation tags
trace_provider = register(
    project_type=ProjectType.OBSERVE,
    project_name="LANGCHAIN_TREE_SEARCH",
    project_version_name="V1",
)

# Initialize the LangChain instrumentor
LangChainInstrumentor().instrument(tracer_provider=trace_provider)


# Define the reflection model for scoring responses
class Reflection(BaseModel):
    reflections: str = Field(
        description="The critique and reflections on the quality of the research report"
    )
    score: int = Field(
        description="Score from 0-10 on the quality of the research report",
        gte=0,
        lte=10,
    )
    found_solution: bool = Field(
        description="Whether the report fully answers the research question"
    )

    def as_message(self):
        return HumanMessage(
            content=f"Reasoning: {self.reflections}\nScore: {self.score}"
        )

    @property
    def normalized_score(self) -> float:
        return self.score / 10.0


# Define the Node class for tree search
class Node:
    def __init__(
        self,
        messages: list[BaseMessage],
        reflection: Reflection,
        parent: Optional["Node"] = None,
    ):
        self.messages = messages
        self.parent = parent
        self.children = []
        self.value = 0
        self.visits = 0
        self.reflection = reflection
        self.depth = parent.depth + 1 if parent is not None else 1
        self._is_solved = reflection.found_solution if reflection else False
        if self._is_solved:
            self._mark_tree_as_solved()
        self.backpropagate(reflection.normalized_score)

    def __repr__(self) -> str:
        return (
            f"<Node value={self.value}, visits={self.visits},"
            f" solution={self.messages} reflection={self.reflection}/>"
        )

    @property
    def is_solved(self):
        """If any solutions exist, we can end the search."""
        return self._is_solved

    @property
    def is_terminal(self):
        return not self.children

    @property
    def best_child_score(self):
        """Return the child with the highest value."""
        if not self.children:
            return None
        return max(self.children, key=lambda child: int(child.is_solved) * child.value)

    @property
    def height(self) -> int:
        """Check for how far we've rolled out the tree."""
        if self.children:
            return 1 + max([child.height for child in self.children])
        return 1

    def upper_confidence_bound(self, exploration_weight=1.0):
        """Return the UCT score. This helps balance exploration vs. exploitation of a branch."""
        if self.parent is None:
            raise ValueError("Cannot obtain UCT from root node")
        if self.visits == 0:
            return self.value
        # Encourages exploitation of high-value trajectories
        average_reward = self.value / self.visits
        # Encourages exploration of less-visited trajectories
        exploration_term = math.sqrt(math.log(self.parent.visits) / self.visits)
        return average_reward + exploration_weight * exploration_term

    def backpropagate(self, reward: float):
        """Update the score of this node and its parents."""
        node = self
        while node:
            node.visits += 1
            node.value = (node.value * (node.visits - 1) + reward) / node.visits
            node = node.parent

    def get_messages(self, include_reflections: bool = True):
        if include_reflections:
            return self.messages + [self.reflection.as_message()]
        return self.messages

    def get_trajectory(self, include_reflections: bool = True) -> list[BaseMessage]:
        """Get messages representing this search branch."""
        messages = []
        node = self
        while node:
            messages.extend(
                node.get_messages(include_reflections=include_reflections)[::-1]
            )
            node = node.parent
        # Reverse the final back-tracked trajectory to return in the correct order
        return messages[::-1]  # root solution, reflection, child 1, ...

    def _get_all_children(self):
        all_nodes = []
        nodes = deque()
        nodes.append(self)
        while nodes:
            node = nodes.popleft()
            all_nodes.extend(node.children)
            for n in node.children:
                nodes.append(n)
        return all_nodes

    def get_best_solution(self):
        """Return the best solution from within the current sub-tree."""
        all_nodes = [self] + self._get_all_children()
        best_node = max(
            all_nodes,
            # We filter out all non-terminal, non-solution trajectories
            key=lambda node: int(node.is_terminal and node.is_solved) * node.value,
        )
        return best_node

    def _mark_tree_as_solved(self):
        parent = self.parent
        while parent:
            parent._is_solved = True
            parent = parent.parent


# Define the state type
class TreeState(TypedDict):
    root: Node
    input: str


# Initialize tools and LLM
llm = ChatOpenAI(model="gpt-4")
search = TavilySearchAPIWrapper()
tavily_tool = TavilySearchResults(api_wrapper=search, max_results=5)
tools = [tavily_tool]
tool_node = ToolNode(tools=tools)

# Define the reflection chain
reflection_prompt = ChatPromptTemplate.from_messages(
    [
        (
            "system",
            "Evaluate the quality of this research report. Consider comprehensiveness, accuracy, and clarity.",
        ),
        ("user", "{input}"),
        MessagesPlaceholder(variable_name="candidate"),
    ]
)

reflection_llm_chain = (
    reflection_prompt
    | llm.bind_tools(tools=[Reflection], tool_choice="Reflection")
    | PydanticToolsParser(tools=[Reflection])
)


@as_runnable
def reflection_chain(inputs) -> Reflection:
    tool_choices = reflection_llm_chain.invoke(inputs)
    reflection = tool_choices[0]
    if not isinstance(inputs["candidate"][-1], AIMessage):
        reflection.found_solution = False
    return reflection


# Define the initial response generation
initial_prompt = ChatPromptTemplate.from_messages(
    [
        (
            "system",
            "You are a research assistant. Generate a comprehensive research report.",
        ),
        ("user", "{input}"),
        MessagesPlaceholder(variable_name="messages", optional=True),
    ]
)

initial_answer_chain = initial_prompt | llm.bind_tools(tools=tools)
parser = JsonOutputToolsParser(return_id=True)


def generate_initial_response(state: TreeState) -> dict:
    res = initial_answer_chain.invoke({"input": state["input"]})
    parsed = parser.invoke(res)
    tool_responses = [
        tool_node.invoke(
            {
                "messages": [
                    AIMessage(
                        content="",
                        tool_calls=[
                            {"name": r["type"], "args": r["args"], "id": r["id"]}
                        ],
                    )
                ]
            }
        )
        for r in parsed
    ]
    output_messages = [res] + [tr["messages"][0] for tr in tool_responses]
    reflection = reflection_chain.invoke(
        {"input": state["input"], "candidate": output_messages}
    )
    root = Node(output_messages, reflection=reflection)
    return {**state, "root": root}


# Define the candidate generation and expansion logic
def select(node: Node) -> Node:
    """Select a node to expand using UCT."""
    while node.children:
        # If we found a solution, return it immediately
        if node.is_solved:
            return node.best_child_score
        # Otherwise use UCT to select the most promising branch
        node = max(node.children, key=lambda n: n.upper_confidence_bound())
    return node


def expand(state: TreeState) -> TreeState:
    """Expand the tree by selecting a node and generating new candidates."""
    root = state["root"]
    node_to_expand = select(root)

    # Generate new response based on the current trajectory
    current_messages = node_to_expand.get_trajectory(include_reflections=True)
    res = initial_answer_chain.invoke(
        {"input": state["input"], "messages": current_messages}
    )

    # Parse and execute tool calls
    parsed = parser.invoke(res)
    tool_responses = [
        tool_node.invoke(
            {
                "messages": [
                    AIMessage(
                        content="",
                        tool_calls=[
                            {"name": r["type"], "args": r["args"], "id": r["id"]}
                        ],
                    )
                ]
            }
        )
        for r in parsed
    ]

    # Create new messages and get reflection
    output_messages = [res] + [tr["messages"][0] for tr in tool_responses]
    reflection = reflection_chain.invoke(
        {"input": state["input"], "candidate": output_messages}
    )

    # Create new node and add to tree
    new_node = Node(output_messages, reflection=reflection, parent=node_to_expand)
    node_to_expand.children.append(new_node)

    return state


# Create the graph
def should_loop(state: TreeState):
    root = state["root"]
    if root.is_solved:
        return END
    if root.height > 3:  # Limit tree depth
        return END
    return "expand"


builder = StateGraph(TreeState)
builder.add_node("start", generate_initial_response)
builder.add_node("expand", expand)
builder.add_edge(START, "start")
builder.add_conditional_edges("start", should_loop, ["expand", END])
builder.add_conditional_edges("expand", should_loop, ["expand", END])

graph = builder.compile()

# Example usage
if __name__ == "__main__":
    question = (
        "Write a research report on the environmental impact of electric vehicles"
    )
    result = graph.invoke({"input": question})

    # Get the best solution
    final_node = result["root"].get_best_solution()
    best_trajectory = final_node.get_trajectory(include_reflections=False)

    # Print the final research report
    print("Final Research Report:")
    print(best_trajectory[-1].content)
