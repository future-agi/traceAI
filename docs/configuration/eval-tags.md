# Evaluation Tags

Evaluation Tags (EvalTags) enable automated AI evaluations on your traces. They run evaluators on span data to measure quality, safety, and accuracy.

## Overview

EvalTags are only available with `ProjectType.EXPERIMENT`. They allow you to:

- Run automated quality checks on LLM outputs
- Detect toxicity, bias, and safety issues
- Measure accuracy against ground truth
- Track custom metrics

## Basic Usage

### Python

```python
from fi_instrumentation import register
from fi_instrumentation.fi_types import (
    ProjectType,
    EvalTag,
    EvalTagType,
    EvalSpanKind,
    EvalName,
    ModelChoices
)

eval_tags = [
    EvalTag(
        type=EvalTagType.OBSERVATION_SPAN,
        value=EvalSpanKind.LLM,
        eval_name=EvalName.TOXICITY,
        custom_eval_name="toxicity_check",
        mapping={"output": "raw.output"},
        model=ModelChoices.TURING_SMALL
    )
]

trace_provider = register(
    project_type=ProjectType.EXPERIMENT,
    project_name="my_experiment",
    project_version_name="v1.0",
    eval_tags=eval_tags
)
```

### TypeScript

```typescript
import {
    register,
    ProjectType,
    EvalTag,
    EvalTagType,
    EvalSpanKind,
    EvalName,
    ModelChoices
} from "@traceai/fi-core";

const evalTags = [
    new EvalTag({
        type: EvalTagType.OBSERVATION_SPAN,
        value: EvalSpanKind.LLM,
        eval_name: EvalName.TOXICITY,
        custom_eval_name: "toxicity_check",
        mapping: { output: "raw.output" },
        model: ModelChoices.TURING_SMALL
    })
];

const tracerProvider = register({
    projectName: "my_experiment",
    projectType: ProjectType.EXPERIMENT,
    projectVersionName: "v1.0",
    evalTags: evalTags,
});
```

## EvalTag Structure

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `type` | `EvalTagType` | Yes | Always `OBSERVATION_SPAN` |
| `value` | `EvalSpanKind` | Yes | Which span type to evaluate |
| `eval_name` | `EvalName` | Yes | Built-in evaluator name |
| `custom_eval_name` | `string` | Yes | Unique identifier for this eval |
| `mapping` | `dict` | Yes | Maps span attributes to eval inputs |
| `model` | `ModelChoices` | No | Evaluation model to use |
| `config` | `dict` | No | Evaluator-specific configuration |

## Span Kinds

| Kind | Description | Use For |
|------|-------------|---------|
| `LLM` | Language model calls | Chat completions, text generation |
| `AGENT` | Agent orchestration | CrewAI, AutoGen agents |
| `TOOL` | Tool/function calls | API calls, code execution |
| `RETRIEVER` | Document retrieval | RAG, vector search |
| `EMBEDDING` | Embedding generation | Text vectorization |
| `RERANKER` | Result reranking | Search optimization |

## Mapping Attributes

The `mapping` field maps span attributes to evaluator inputs:

```python
mapping = {
    "context": "raw.input",      # Use input as context
    "output": "raw.output",      # Use output for evaluation
    "reference": "metadata.expected"  # Custom field
}
```

Common mappings:
- `raw.input` - Full input value
- `raw.output` - Full output value
- `llm.input_messages` - Input messages array
- `llm.output_messages` - Output messages array
- `metadata.*` - Custom metadata fields

## Available Evaluators

### Content Quality

| Evaluator | Description | Required Mapping |
|-----------|-------------|------------------|
| `CONTEXT_ADHERENCE` | Output follows provided context | `context`, `output` |
| `CONTEXT_RELEVANCE` | Context is relevant to query | `context`, `query` |
| `COMPLETENESS` | Response is complete | `output` |
| `GROUNDEDNESS` | Claims are grounded in context | `context`, `output` |
| `SUMMARY_QUALITY` | Summary captures key points | `document`, `summary` |
| `CHUNK_ATTRIBUTION` | Output cites sources correctly | `context`, `output` |

### Safety & Compliance

| Evaluator | Description | Required Mapping |
|-----------|-------------|------------------|
| `TOXICITY` | Detects toxic content | `output` |
| `PII` | Detects personal information | `output` |
| `CONTENT_MODERATION` | General content safety | `output` |
| `PROMPT_INJECTION` | Detects injection attempts | `input` |
| `CONTENT_SAFETY_VIOLATION` | Safety policy violations | `output` |
| `DATA_PRIVACY_COMPLIANCE` | GDPR/privacy compliance | `output` |

### Accuracy

| Evaluator | Description | Required Mapping |
|-----------|-------------|------------------|
| `FACTUAL_ACCURACY` | Facts are correct | `output`, `reference` |
| `DETECT_HALLUCINATION` | Detects made-up information | `context`, `output` |
| `TRANSLATION_ACCURACY` | Translation quality | `source`, `translation` |

### Bias Detection

| Evaluator | Description | Required Mapping |
|-----------|-------------|------------------|
| `BIAS_DETECTION` | General bias detection | `output` |
| `NO_RACIAL_BIAS` | Racial bias check | `output` |
| `NO_GENDER_BIAS` | Gender bias check | `output` |
| `NO_AGE_BIAS` | Age bias check | `output` |
| `CULTURAL_SENSITIVITY` | Cultural awareness | `output` |

### Tone & Style

| Evaluator | Description | Required Mapping |
|-----------|-------------|------------------|
| `TONE` | Analyzes tone | `output` |
| `IS_POLITE` | Politeness check | `output` |
| `IS_CONCISE` | Brevity check | `output` |
| `IS_HELPFUL` | Helpfulness rating | `output` |
| `IS_INFORMAL_TONE` | Formality check | `output` |
| `NO_APOLOGIES` | Unnecessary apologies | `output` |

### Format Validation

| Evaluator | Description | Required Mapping |
|-----------|-------------|------------------|
| `IS_JSON` | Valid JSON output | `output` |
| `IS_CODE` | Valid code output | `output` |
| `ONE_LINE` | Single line output | `output` |
| `CONTAINS_VALID_LINK` | Has valid URLs | `output` |
| `IS_EMAIL` | Valid email format | `output` |

### Similarity Metrics

| Evaluator | Description | Required Mapping |
|-----------|-------------|------------------|
| `BLEU_SCORE` | BLEU similarity | `reference`, `hypothesis` |
| `ROUGE_SCORE` | ROUGE similarity | `reference`, `hypothesis` |
| `LEVENSHTEIN_SIMILARITY` | Edit distance | `text1`, `text2` |
| `EMBEDDING_SIMILARITY` | Semantic similarity | `response`, `expected_text` |
| `FUZZY_MATCH` | Fuzzy string matching | `text1`, `text2` |

### Task Completion

| Evaluator | Description | Required Mapping |
|-----------|-------------|------------------|
| `TASK_COMPLETION` | Task was completed | `task`, `output` |
| `EVALUATE_FUNCTION_CALLING` | Correct function use | `expected`, `actual` |
| `PROMPT_INSTRUCTION_ADHERENCE` | Follows instructions | `instruction`, `output` |

## Model Choices

| Model | Speed | Accuracy | Use Case |
|-------|-------|----------|----------|
| `TURING_FLASH` | Fastest | Good | High-volume, simple evals |
| `TURING_SMALL` | Fast | Better | Balanced performance |
| `TURING_LARGE` | Slower | Best | Complex evaluations |
| `PROTECT` | Fast | Good | Safety evaluations |
| `PROTECT_FLASH` | Fastest | Good | High-volume safety |

## Examples

### Quality + Safety Combo

```python
eval_tags = [
    # Check output quality
    EvalTag(
        type=EvalTagType.OBSERVATION_SPAN,
        value=EvalSpanKind.LLM,
        eval_name=EvalName.CONTEXT_ADHERENCE,
        custom_eval_name="quality_adherence",
        mapping={
            "context": "raw.input",
            "output": "raw.output"
        },
        model=ModelChoices.TURING_SMALL
    ),
    # Check for toxicity
    EvalTag(
        type=EvalTagType.OBSERVATION_SPAN,
        value=EvalSpanKind.LLM,
        eval_name=EvalName.TOXICITY,
        custom_eval_name="safety_toxicity",
        mapping={"output": "raw.output"},
        model=ModelChoices.PROTECT_FLASH
    ),
    # Check for hallucinations
    EvalTag(
        type=EvalTagType.OBSERVATION_SPAN,
        value=EvalSpanKind.LLM,
        eval_name=EvalName.DETECT_HALLUCINATION,
        custom_eval_name="accuracy_hallucination",
        mapping={
            "context": "raw.input",
            "output": "raw.output"
        },
        model=ModelChoices.TURING_LARGE
    )
]
```

### RAG Pipeline Evaluation

```python
eval_tags = [
    # Evaluate retriever
    EvalTag(
        type=EvalTagType.OBSERVATION_SPAN,
        value=EvalSpanKind.RETRIEVER,
        eval_name=EvalName.CONTEXT_RELEVANCE,
        custom_eval_name="retriever_relevance",
        mapping={
            "context": "raw.output",
            "query": "raw.input"
        },
        model=ModelChoices.TURING_SMALL
    ),
    # Evaluate LLM response
    EvalTag(
        type=EvalTagType.OBSERVATION_SPAN,
        value=EvalSpanKind.LLM,
        eval_name=EvalName.GROUNDEDNESS,
        custom_eval_name="llm_groundedness",
        mapping={
            "context": "raw.input",
            "output": "raw.output"
        },
        model=ModelChoices.TURING_SMALL
    )
]
```

## Constraints

1. **Unique custom names**: Each `custom_eval_name` must be unique
2. **EXPERIMENT only**: EvalTags require `ProjectType.EXPERIMENT`
3. **No session name**: Can't use `session_name` with experiments
4. **Version required**: Should specify `project_version_name`

## Related

- [Python Quickstart](../getting-started/quickstart-python.md) - Basic usage
- [Core Concepts](../python/core-concepts.md) - Understanding traceAI
