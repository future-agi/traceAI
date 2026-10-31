#!/usr/bin/env python3
"""Regenerate README coverage and its manifest-derived proof (Python 3.11+).

One row is one integration family, not a package or supported language. Runtime
variants (OpenAI Realtime, Google GenAI Live, AG2 classic) share their family row.
LangGraph ships inside the LangChain package and is not counted a second time.
Shared/core SDKs and the Python meta-package are excluded from integration counts.
C# is a manual-span SDK, not a set of automatic framework instrumentors.
"""
from pathlib import Path
import json
import re
import tomllib
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / ".github/assets"
ALIASES = {
    "ag2-classic": "ag2", "google-genai-live": "google-genai",
    "openai-realtime": "openai", "llama_index": "llamaindex",
    "mistralai": "mistral", "mongodb-vector": "mongodb", "redis-vector": "redis",
    "genai": "google-genai", "langchain4j": "langchain4j",
    "fi-instrumentation-google-adk": "google-adk",
    "fi-instrumentation-livekit": "livekit", "fi-instrumentation-pipecat": "pipecat",
    "fi-instrumentation-vertexai": "vertexai",
}
NAMES = {
    "a2a": "A2A", "ag2": "AG2", "agent-framework": "Microsoft Agent Framework",
    "agno": "Agno", "anthropic": "Anthropic", "autogen": "AutoGen",
    "azure-openai": "Azure OpenAI", "azure-search": "Azure AI Search",
    "bedrock": "Amazon Bedrock", "beeai": "BeeAI", "cerebras": "Cerebras",
    "chromadb": "Chroma", "claude-agent-sdk": "Claude Agent SDK", "codex-sdk": "Codex SDK",
    "cognee": "Cognee", "cohere": "Cohere", "crewai": "CrewAI", "cursor-sdk": "Cursor SDK",
    "deepseek": "DeepSeek", "discoveryengine": "Google Discovery Engine", "dspy": "DSPy",
    "eino": "Eino", "elasticsearch": "Elasticsearch", "elevenlabs-agents": "ElevenLabs Agents",
    "exa": "Exa", "firecrawl": "Firecrawl", "fireworks": "Fireworks AI", "genkit": "Genkit",
    "google-adk": "Google ADK", "google-genai": "Google GenAI (Gemini)", "groq": "Groq",
    "guardrails": "Guardrails AI", "haystack": "Haystack", "hermes-agent": "Hermes Agent",
    "huggingface": "Hugging Face", "instructor": "Instructor", "lancedb": "LanceDB",
    "langchain": "LangChain / LangGraph", "langchain4j": "LangChain4j", "litellm": "LiteLLM",
    "livekit": "LiveKit", "llamaindex": "LlamaIndex", "mastra": "Mastra", "mcp": "MCP",
    "mcp-use": "MCP-use", "milvus": "Milvus", "minimax": "MiniMax", "mistral": "Mistral AI",
    "mongodb": "MongoDB Atlas Vector Search", "ollama": "Ollama", "openai": "OpenAI",
    "openai-agents": "OpenAI Agents", "parallel": "Parallel", "pgvector": "pgvector",
    "pi-coding-agent": "Pi Coding Agent", "pinecone": "Pinecone", "pipecat": "Pipecat",
    "portkey": "Portkey", "pydantic-ai": "Pydantic AI", "qdrant": "Qdrant",
    "redis": "Redis Vector Search", "replicate": "Replicate", "semantic-kernel": "Semantic Kernel",
    "smolagents": "Smolagents", "spring-ai": "Spring AI", "spring-boot-starter": "Spring Boot",
    "strands": "Strands Agents", "tavily": "Tavily", "together": "Together AI", "typesafe": "TypeSafe",
    "vercel": "Vercel AI SDK", "vertexai": "Vertex AI", "vllm": "vLLM", "voltagent": "VoltAgent",
    "voyage": "Voyage AI", "watsonx": "IBM watsonx", "weaviate": "Weaviate", "xai": "xAI",
}
LOGOS = {k: k for k in ["openai", "anthropic", "bedrock", "groq", "cohere", "ollama", "langchain", "llamaindex", "crewai", "mcp", "livekit", "pipecat", "pinecone", "dspy", "guardrails", "haystack", "litellm", "mastra", "vercel", "autogen", "together", "vertexai"]}
LOGOS.update({"google-genai": "google", "google-adk": "google", "azure-openai": "openai", "openai-agents": "openai", "langchain4j": "langchain", "spring-ai": "spring", "spring-boot-starter": "spring", "mistral": "mistral"})
# Classify by the integration's own README, manifest and implementation, not by
# language or branding. In particular, mcp-use traces an agent loop; MCP traces
# the protocol. Hermes is a general-purpose agent, not a coding-only SDK. TypeSafe
# emits EVALUATOR spans. Search backends and RAG pipelines belong with retrieval.
CATEGORIES = [
    ("model-providers", "Model providers",
     "Hosted and local model inference, embeddings, and reranking.",
     "anthropic azure-openai bedrock cerebras cohere deepseek fireworks google-genai "
     "groq huggingface minimax mistral ollama openai replicate together vertexai "
     "vllm voyage watsonx xai"),
    ("agent-frameworks", "Agent frameworks",
     "Agent orchestration, application frameworks, and agent-to-agent communication.",
     "a2a ag2 agent-framework agno autogen beeai crewai eino genkit google-adk "
     "hermes-agent langchain mastra mcp-use openai-agents pydantic-ai semantic-kernel "
     "smolagents strands vercel voltagent"),
    ("rag-vector-databases", "RAG & vector databases",
     "Retrieval pipelines, vector stores, and search backends.",
     "azure-search chromadb cognee discoveryengine elasticsearch haystack lancedb llamaindex "
     "milvus mongodb pgvector pinecone qdrant redis weaviate"),
    ("tools-search", "Tools & search",
     "Tool connectivity, web search, crawling, and extraction.",
     "exa firecrawl mcp parallel tavily"),
    ("voice-realtime", "Voice & realtime",
     "Voice-agent pipelines and conversations. OpenAI Realtime and Google GenAI Live "
     "are linked in their provider rows under [Model providers](#model-providers).",
     "elevenlabs-agents livekit pipecat"),
    ("gateways-proxies", "Gateways & proxies",
     "Unified access and routing across model providers.",
     "litellm portkey"),
    ("coding-agents", "Coding agents",
     "In-process SDK integrations for coding-agent runs, sessions, and tools.",
     "claude-agent-sdk codex-sdk cursor-sdk pi-coding-agent"),
    ("java-frameworks", "Java frameworks",
     "Java application-framework integrations and auto-configuration.",
     "langchain4j spring-ai spring-boot-starter"),
    ("other", "Other",
     "LLM programming, structured output, validation, and evaluation.",
     "dspy guardrails instructor typesafe"),
]
category_members = [key for _, _, _, members in CATEGORIES for key in members.split()]
assert len(category_members) == len(set(category_members)), "An integration is in multiple categories"
assert set(category_members) <= set(NAMES), "A category references an unknown integration"
FAMILY_CATEGORIES = {key: slug for slug, _, _, members in CATEGORIES for key in members.split()}
LANGS = ["Python", "TypeScript", "Java", "Go", "C#"]
rows = {}
raw = {lang: [] for lang in LANGS}
excluded = []

def add(lang, key, path, name):
    key = ALIASES.get(key, key)
    assert key in NAMES, f"Name the new integration family: {key}"
    assert key in FAMILY_CATEGORIES, f"Categorize the new integration family: {key}"
    assert path.is_file(), path
    item = {"manifest": str(path.relative_to(ROOT)), "package": name, "source": str(path.parent.relative_to(ROOT)) + "/"}
    raw[lang].append(item)
    rows.setdefault(key, {"id": key, "name": NAMES[key], "category": FAMILY_CATEGORIES[key], "languages": {}})["languages"].setdefault(lang, []).append(item)

for path in sorted(ROOT.glob("python/frameworks/*/pyproject.toml")):
    doc = tomllib.loads(path.read_text())
    package = doc.get("project", {}).get("name") or doc["tool"]["poetry"]["name"]
    add("Python", path.parent.name, path, package)
for path in sorted(ROOT.glob("typescript/packages/*/package.json")):
    doc = json.loads(path.read_text())
    name = doc["name"]
    if name in ("@traceai/fi-core", "@traceai/fi-semantic-conventions"):
        excluded.append({"manifest": str(path.relative_to(ROOT)), "reason": "shared SDK / semantic conventions"})
        continue
    add("TypeScript", name.split("/", 1)[1], path, name)
ns = {"m": "http://maven.apache.org/POM/4.0.0"}
parent = ET.parse(ROOT / "java/pom.xml").getroot()
for element in parent.findall("m:modules/m:module", ns):
    module = element.text
    path = ROOT / "java" / module / "pom.xml"
    pom = ET.parse(path).getroot()
    name = pom.findtext("m:artifactId", namespaces=ns)
    key = re.sub(r"^traceai-(?:java-)?", "", name)
    # POM-only modules manage dependencies / aggregate builds, not integrations.
    # Example and shared modules can also be jars; exclude by role, not a list
    # of current artifactIds. Unknown integration names must still fail in add().
    packaging = pom.findtext("m:packaging", default="jar", namespaces=ns)
    roles = set(re.split(r"[-_/]", module) + key.split("-"))
    if packaging == "pom":
        reason = "dependency-management / aggregator POM; not an integration"
    elif roles & {"core", "bom", "example", "examples"}:
        reason = "shared SDK / BOM / example; not an integration"
    else:
        add("Java", key, path, name)
        continue
    excluded.append({"manifest": str(path.relative_to(ROOT)), "reason": reason})
for path in sorted(ROOT.glob("go/*/go.mod")):
    name = re.search(r"^module\s+(\S+)", path.read_text(), re.M)[1]
    add("Go", path.parent.name, path, name)
for path in sorted(ROOT.glob("csharp/src/*/*.csproj")):
    excluded.append({"manifest": str(path.relative_to(ROOT)), "reason": "manual-span SDK; no framework auto-instrumentors"})

categories = [
    {"id": slug, "name": name, "description": description,
     "families": sorted(set(members.split()) & set(rows), key=lambda k: NAMES[k].casefold())}
    for slug, name, description, members in CATEGORIES
    if set(members.split()) & set(rows)
]
ordered = [key for category in categories for key in category["families"]]
assert len(ordered) == len(rows) and set(ordered) == set(rows), "Every family must appear exactly once"
provenance = json.loads((OUT / "integrations/provenance.json").read_text())

def table(keys):
    lines = ["| Integration | Python | TypeScript | Java | Go | C# |", "| :--- | :---: | :---: | :---: | :---: | :---: |"]
    for key in keys:
        row = rows[key]
        logo = LOGOS.get(key)
        file = provenance["assets"][logo]["file"] if logo else "package-tile.svg"
        label = f'<img src=".github/assets/integrations/{file}" width="24" height="24" alt=""> {row["name"]}'
        cells = []
        for lang in LANGS:
            packages = row["languages"].get(lang, [])
            links = []
            for index, item in enumerate(packages):
                title = "SDK" if index == 0 else ("Live" if "live" in item["package"].lower() else "Realtime" if "realtime" in item["package"].lower() else "Classic")
                links.append(f'[{title}]({item["source"]} "{item["package"]}")')
            cells.append(" · ".join(links) or "—")
        lines.append("| " + label + " | " + " | ".join(cells) + " |")
    return "\n".join(lines)

count = len(rows)
# Explicit anchors keep punctuation-bearing category links stable on GitHub.
# All tables are ordinary page content: no collapsed or featured-only catalog.
navigation = " · ".join(f'[{c["name"]}](#{c["id"]})' for c in categories)
grouped_tables = "\n\n".join(
    f'<a id="{c["id"]}"></a>\n\n### {c["name"]}\n\n'
    f'{c["description"]}\n\n{table(c["families"])}'
    for c in categories
)
section = f'''## Integrations

**{count} integration families, with language coverage you can inspect.** Each SDK link opens its package source. A dash means no dedicated framework package in this repository—not a promise of unsupported runtime behavior.

Python and TypeScript provide framework instrumentors; Java provides client wrappers and framework modules; Go provides middleware and framework integrations. **C# provides [manual spans and activities](csharp/src/FIInstrumentation/), not automatic framework instrumentation**, so its framework cells are intentionally empty.

All {count} families are listed below, grouped by what they integrate. Runtime variants share a row: OpenAI includes Realtime; Google GenAI includes Live; AG2 includes its classic SDK. LangGraph ships in the LangChain integration. Shared SDKs are not counted as integrations. Package availability is not a claim of complete upstream feature compatibility; check each package's source and documentation for its scope.

{navigation}

{grouped_tables}

**Bring existing telemetry:** [OpenInference](https://docs.futureagi.com/docs/integrations/import/openinference) · [OpenLLMetry](https://docs.futureagi.com/docs/integrations/otel/openllmetry)

**Source indexes:** [Python packages](python/frameworks/) · [TypeScript packages](typescript/packages/) · [Java modules](java/) · [Go modules](go/) · [C# APIs](csharp/src/FIInstrumentation/)

[Counting method and manifest inventory](.github/assets/integration-coverage.json) · Regenerate with `python3 .github/assets/generate-coverage.py` (Python 3.11+).

Brand marks identify integrations, not partnerships or endorsements. A neutral package icon is used where no brand asset is bundled. [Asset sources and licenses](.github/assets/integrations/provenance.json).

'''
readme = ROOT / "README.md"
text = readme.read_text()
start = text.index("## Integrations\n")
end = text.index("## OpenTelemetry, without a new tracing stack\n")
text = text[:start] + section + text[end:]
proof = f"**{count} integration families · 5 languages · OpenTelemetry-native · Apache 2.0**"
text = re.sub(r"\n+\*\*\d+ integration families · 5 languages · OpenTelemetry-native · Apache 2.0\*\*\n+", "\n\n", text)
tagline = "Trace LLM calls, agents, tools, and retrieval. Keep your application code and your observability stack."
text = text.replace(tagline, tagline + "\n\n" + proof)
readme.write_text(text)
summary = {
    "integration_families": count,
    "languages": LANGS,
    "method": f"Union of normalized framework families from {len(raw['Python'])} Python framework manifests, {len(raw['TypeScript'])} TypeScript instrumentor manifests, {len(raw['Java'])} Java integration modules and {len(raw['Go'])} Go module manifests. Runtime variants share a family; shared/core SDKs, BOMs, aggregators and examples do not count. C# has a manual-span core SDK and no framework auto-instrumentors.",
    "normalization": ALIASES,
    "package_counts_in_matrix": {lang: len(items) for lang, items in raw.items()},
    "excluded_shared_packages": excluded,
    "categories": categories,
    "families": [rows[k] for k in ordered],
}
(OUT / "integration-coverage.json").write_text(json.dumps(summary, indent=2) + "\n")
print(json.dumps({"families": count, "visible": len(ordered), "categories": {c["name"]: len(c["families"]) for c in categories}, "packages": summary["package_counts_in_matrix"]}))
