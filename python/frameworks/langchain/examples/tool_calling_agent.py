import os

import requests
from fi.evals import Evaluator, Protect
from fi_instrumentation import register
from fi_instrumentation.fi_types import (
    EvalName,
    EvalSpanKind,
    EvalTag,
    EvalTagType,
    ProjectType,
)
from langchain import agents
from langchain.chains import RetrievalQA
from langchain.memory import ConversationBufferMemory
from langchain.retrievers import ContextualCompressionRetriever
from langchain.text_splitter import RecursiveCharacterTextSplitter
from langchain.tools import tool
from langchain_cohere import CohereRerank
from langchain_community.document_loaders import PyPDFLoader
from langchain_community.embeddings import OpenAIEmbeddings
from langchain_community.vectorstores import FAISS
from langchain_core.prompts import ChatPromptTemplate
from langchain_openai import ChatOpenAI
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
    )
]


def setup_instrumentation():
    """Configure and initialize OpenTelemetry instrumentation."""
    trace_provider = register(
        project_name="GUARDRAILSPAN2",
        project_type=ProjectType.OBSERVE,
        session_name="GUARDRAILSPAN2",
        # project_version_name="V2",
        # eval_tags=eval_tags,
    )
    instrumentor = LangChainInstrumentor()
    instrumentor.instrument(tracer_provider=trace_provider)

    # Return the specific FITracer instance used by the instrumentor
    return instrumentor.fi_tracer


@tool
def get_weather_info(
    location: str,
    units: str = "celsius",
) -> dict:
    """Retrieves current weather information for a specified location.

    Args:
        location (str): City name or location (e.g., "London,UK")
        units (str): Temperature unit - "celsius" or "fahrenheit" (default: celsius)

    Returns:
        dict: Weather data from Open-Meteo API including temperature, conditions, etc.
    """
    # Clean up location string and handle common variations
    location = location.strip().replace(" ", "")
    if "," not in location:
        # If no country code provided, default to US for major cities
        location = f"{location},US"

    # First, get coordinates for the location using Open-Meteo Geocoding API
    geocoding_url = (
        f"https://geocoding-api.open-meteo.com/v1/search?name={location}&count=1"
    )
    geo_response = requests.get(geocoding_url)
    geo_response.raise_for_status()

    geo_data = geo_response.json()
    if not geo_data.get("results"):
        # Try without country code if initial search fails
        location_without_country = location.split(",")[0]
        geocoding_url = f"https://geocoding-api.open-meteo.com/v1/search?name={location_without_country}&count=1"
        geo_response = requests.get(geocoding_url)
        geo_response.raise_for_status()
        geo_data = geo_response.json()
        if not geo_data.get("results"):
            raise ValueError(f"Location '{location}' not found")

    location_data = geo_data["results"][0]
    lat, lon = location_data["latitude"], location_data["longitude"]

    # Get weather data from Open-Meteo Weather API
    temperature_unit = "fahrenheit" if units.lower() == "fahrenheit" else "celsius"
    weather_url = (
        f"https://api.open-meteo.com/v1/forecast"
        f"?latitude={lat}&longitude={lon}"
        f"&current=temperature_2m,relative_humidity_2m,weather_code,wind_speed_10m"
        f"&temperature_unit={temperature_unit}"
    )

    weather_response = requests.get(weather_url)
    weather_response.raise_for_status()

    weather_data = weather_response.json()
    current = weather_data["current"]

    # Map weather codes to descriptions
    # Reference: https://open-meteo.com/en/docs
    weather_codes = {
        0: "clear sky",
        1: "mainly clear",
        2: "partly cloudy",
        3: "overcast",
        45: "foggy",
        48: "depositing rime fog",
        51: "light drizzle",
        53: "moderate drizzle",
        55: "dense drizzle",
        61: "slight rain",
        63: "moderate rain",
        65: "heavy rain",
        71: "slight snow fall",
        73: "moderate snow fall",
        75: "heavy snow fall",
        77: "snow grains",
        80: "slight rain showers",
        81: "moderate rain showers",
        82: "violent rain showers",
        85: "slight snow showers",
        86: "heavy snow showers",
        95: "thunderstorm",
        96: "thunderstorm with slight hail",
        99: "thunderstorm with heavy hail",
    }

    return {
        "temperature": current["temperature_2m"],
        "humidity": current["relative_humidity_2m"],
        "conditions": weather_codes.get(current["weather_code"], "unknown"),
        "wind_speed": current["wind_speed_10m"],
    }


@tool
def get_stock_info(symbol: str) -> dict:
    """Retrieves current stock market information for a specified symbol using Yahoo Finance.

    Args:
        symbol (str): Stock symbol (e.g., "AAPL" for Apple Inc.)

    Returns:
        dict: Basic stock data including current price and daily change
    """
    # Using Yahoo Finance public data
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"
    }

    response = requests.get(url, headers=headers)
    response.raise_for_status()

    data = response.json()
    if (
        "chart" not in data
        or "result" not in data["chart"]
        or not data["chart"]["result"]
    ):
        raise ValueError(f"No data found for symbol '{symbol}'")

    quote = data["chart"]["result"][0]["meta"]
    return {
        "symbol": symbol.upper(),
        "price": quote.get("regularMarketPrice", 0),
        "previous_close": quote.get("previousClose", 0),
        "currency": quote.get("currency", "USD"),
        "exchange": quote.get("exchangeName", "N/A"),
    }


@tool
def get_document_qa(query: str, pdf_folder: str = "documents") -> str:
    """Answers questions based on the content of PDF documents in the specified folder.

    Args:
        query (str): The question to answer about the documents
        pdf_folder (str): Folder containing PDF documents (default: "documents")

    Returns:
        str: Answer based on the document content
    """
    if not os.path.exists(pdf_folder):
        raise ValueError(f"PDF folder '{pdf_folder}' does not exist")

    if not os.getenv("OPENAI_API_KEY"):
        raise ValueError("Please set OPENAI_API_KEY environment variable")

    if not os.getenv("COHERE_API_KEY"):
        raise ValueError("Please set COHERE_API_KEY environment variable")

    # Create RAG pipeline
    documents = []
    for filename in os.listdir(pdf_folder):
        if filename.endswith(".pdf"):
            pdf_path = os.path.join(pdf_folder, filename)
            loader = PyPDFLoader(pdf_path)
            documents.extend(loader.load())

    if not documents:
        raise ValueError(f"No PDF files found in {pdf_folder}")

    text_splitter = RecursiveCharacterTextSplitter(
        chunk_size=1000,
        chunk_overlap=200,
        length_function=len,
        separators=["\n\n", "\n", " ", ""],
    )
    texts = text_splitter.split_documents(documents)

    embeddings = OpenAIEmbeddings(
        model="text-embedding-3-small",
        api_key=os.getenv("OPENAI_API_KEY"),
    )
    vectorstore = FAISS.from_documents(texts, embeddings)

    base_retriever = vectorstore.as_retriever(search_kwargs={"k": 20})
    reranker = CohereRerank(
        cohere_api_key=os.getenv("COHERE_API_KEY"),
        model="rerank-english-v2.0",
        top_n=4,
    )

    compression_retriever = ContextualCompressionRetriever(
        base_compressor=reranker, base_retriever=base_retriever
    )

    llm = ChatOpenAI(model_name="gpt-3.5-turbo", temperature=0)
    qa_chain = RetrievalQA.from_chain_type(
        llm=llm,
        chain_type="stuff",
        retriever=compression_retriever,
    )

    result = qa_chain.invoke({"query": query})
    return result["result"]


def create_agent(tools: list) -> agents.AgentExecutor:
    """Create and configure the LangChain agent.

    Args:
        tools (list): List of tools available to the agent

    Returns:
        agents.AgentExecutor: Configured agent executor
    """
    llm = ChatOpenAI(model_name="gpt-4.1-nano", temperature=0)
    memory = ConversationBufferMemory(memory_key="chat_history", return_messages=True)

    prompt = ChatPromptTemplate.from_messages(
        [
            (
                "system",
                "You are a helpful AI assistant. Get information using tools. "
                "Before providing a final answer, run the run_guardrail_check tool on your proposed answer.",
            ),
            ("human", "{input}"),
            ("human", "Chat history: {chat_history}"),
            ("placeholder", "{agent_scratchpad}"),
        ]
    )
    agent = agents.create_tool_calling_agent(llm, tools, prompt)
    return agents.AgentExecutor(agent=agent, tools=tools, verbose=True, memory=memory)


def main():
    """Main execution function."""
    # Get the specific FITracer instance from setup
    tracer = setup_instrumentation()
    evaluator = Evaluator(
        fi_api_key=os.environ.get("FI_API_KEY"),
        fi_secret_key=os.environ.get("FI_SECRET_KEY"),
        fi_base_url=os.environ.get("FI_BASE_URL"),
    )
    protector = Protect(evaluator=evaluator)
    tools = [get_weather_info, get_stock_info, get_document_qa]
    agent_executor = create_agent(tools)

    # Example queries with conversation context
    queries = [
        "What's the current weather like in London?",
        "How does that compare to New York?",
        "What's the latest stock price for AAPL?",
        "And how about MSFT?",
    ]
    rules = [
        {"metric": "Tone", "contains": ["anger", "fear"], "type": "any"},
        {"metric": "Toxicity"},
        {
            "metric": "Prompt Injection",
        },
    ]
    results = []
    for query in queries:
        # Create a parent span for each query processing step using the correct tracer
        with tracer.start_as_current_span(
            f"Process Query: {query[:50]}..."
        ) as parent_span:  # Limit query length for span name
            # Agent execution should now inherit context from parent_span
            agent_result = agent_executor.invoke({"input": query})

            # Guardrail execution should also inherit context from parent_span
            protect_result = protector.protect(
                inputs=query, protect_rules=rules, action="block", reason=True
            )  # Protecting original query for now

            # Add attributes to the parent span
            parent_span.set_attribute(
                "agent.output", str(agent_result.get("output", ""))
            )
            parent_span.set_attribute(
                "guardrail.status", protect_result.get("status", "")
            )
            parent_span.set_attribute(
                "guardrail.failed_rule", protect_result.get("failed_rule", "")
            )

            results.append(protect_result)  # Or maybe agent_result?

    return results


if __name__ == "__main__":
    main()
