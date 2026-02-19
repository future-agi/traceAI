declare enum ProjectType {
    EXPERIMENT = "experiment",
    OBSERVE = "observe"
}
declare enum EvalTagType {
    OBSERVATION_SPAN = "OBSERVATION_SPAN_TYPE"
}
declare enum ModelChoices {
    TURING_LARGE = "turing_large",
    TURING_SMALL = "turing_small",
    PROTECT = "protect",
    PROTECT_FLASH = "protect_flash",
    TURING_FLASH = "turing_flash"
}
declare enum EvalSpanKind {
    TOOL = "TOOL",
    LLM = "LLM",
    RETRIEVER = "RETRIEVER",
    EMBEDDING = "EMBEDDING",
    AGENT = "AGENT",
    RERANKER = "RERANKER"
}
declare enum EvalName {
    CONVERSATION_COHERENCE = "conversation_coherence",
    CONVERSATION_RESOLUTION = "conversation_resolution",
    CONTENT_MODERATION = "content_moderation",
    CONTEXT_ADHERENCE = "context_adherence",
    CONTEXT_RELEVANCE = "context_relevance",
    COMPLETENESS = "completeness",
    CHUNK_ATTRIBUTION = "chunk_attribution",
    CHUNK_UTILIZATION = "chunk_utilization",
    PII = "pii",
    TOXICITY = "toxicity",
    TONE = "tone",
    SEXIST = "sexist",
    PROMPT_INJECTION = "prompt_injection",
    PROMPT_INSTRUCTION_ADHERENCE = "prompt_instruction_adherence",
    DATA_PRIVACY_COMPLIANCE = "data_privacy_compliance",
    IS_JSON = "is_json",
    ONE_LINE = "one_line",
    CONTAINS_VALID_LINK = "contains_valid_link",
    IS_EMAIL = "is_email",
    NO_VALID_LINKS = "no_valid_links",
    GROUNDEDNESS = "groundedness",
    EVAL_RANKING = "eval_ranking",
    SUMMARY_QUALITY = "summary_quality",
    FACTUAL_ACCURACY = "factual_accuracy",
    TRANSLATION_ACCURACY = "translation_accuracy",
    CULTURAL_SENSITIVITY = "cultural_sensitivity",
    BIAS_DETECTION = "bias_detection",
    EVALUATE_LLM_FUNCTION_CALLING = "evaluate_llm_function_calling",
    AUDIO_TRANSCRIPTION = "audio_transcription",
    AUDIO_QUALITY = "audio_quality",
    NO_RACIAL_BIAS = "no_racial_bias",
    NO_GENDER_BIAS = "no_gender_bias",
    NO_AGE_BIAS = "no_age_bias",
    NO_OPENAI_REFERENCE = "no_openai_reference",
    NO_APOLOGIES = "no_apologies",
    IS_POLITE = "is_polite",
    IS_CONCISE = "is_concise",
    IS_HELPFUL = "is_helpful",
    IS_CODE = "is_code",
    FUZZY_MATCH = "fuzzy_match",
    ANSWER_REFUSAL = "answer_refusal",
    DETECT_HALLUCINATION = "detect_hallucination",
    NO_HARMFUL_THERAPEUTIC_GUIDANCE = "no_harmful_therapeutic_guidance",
    CLINICALLY_INAPPROPRIATE_TONE = "clinically_inappropriate_tone",
    IS_HARMFUL_ADVICE = "is_harmful_advice",
    CONTENT_SAFETY_VIOLATION = "content_safety_violation",
    IS_GOOD_SUMMARY = "is_good_summary",
    IS_FACTUALLY_CONSISTENT = "is_factually_consistent",
    IS_COMPLIANT = "is_compliant",
    IS_INFORMAL_TONE = "is_informal_tone",
    EVALUATE_FUNCTION_CALLING = "evaluate_function_calling",
    TASK_COMPLETION = "task_completion",
    CAPTION_HALLUCINATION = "caption_hallucination",
    BLEU_SCORE = "bleu_score",
    ROUGE_SCORE = "rouge_score",
    TEXT_TO_SQL = "text_to_sql",
    RECALL_SCORE = "recall_score",
    LEVENSHTEIN_SIMILARITY = "levenshtein_similarity",
    NUMERIC_SIMILARITY = "numeric_similarity",
    EMBEDDING_SIMILARITY = "embedding_similarity",
    SEMANTIC_LIST_CONTAINS = "semantic_list_contains",
    IS_AI_GENERATED_IMAGE = "is_AI_generated_image"
}
interface ConfigField {
    type: string;
    default?: any;
    required: boolean;
}
interface EvalConfig {
    [key: string]: ConfigField;
}
interface EvalMapping {
    [key: string]: ConfigField;
}
declare function getConfigForEval(evalName: EvalName): EvalConfig;
declare function getMappingForEval(evalName: EvalName): EvalMapping;
declare function validateFieldType(key: string, expectedType: string, value: any): void;
interface IEvalTag {
    type: EvalTagType;
    value: EvalSpanKind;
    eval_name: EvalName | string;
    custom_eval_name: string;
    config: Record<string, any>;
    mapping: Record<string, string>;
    score?: number;
    rationale?: string | null;
    metadata?: Record<string, any> | null;
    toDict(): Record<string, any>;
    toString(): string;
    model?: ModelChoices;
}
declare class EvalTag implements IEvalTag {
    type: EvalTagType;
    value: EvalSpanKind;
    eval_name: EvalName | string;
    custom_eval_name: string;
    config: Record<string, any>;
    mapping: Record<string, string>;
    score?: number;
    rationale?: string | null;
    metadata?: Record<string, any> | null;
    model?: ModelChoices;
    constructor(params: {
        type: EvalTagType;
        value: EvalSpanKind;
        eval_name: EvalName | string;
        custom_eval_name: string;
        config?: Record<string, any>;
        mapping?: Record<string, string>;
        score?: number;
        rationale?: string | null;
        metadata?: Record<string, any> | null;
        model?: ModelChoices;
    });
    static create(params: {
        type: EvalTagType;
        value: EvalSpanKind;
        eval_name: EvalName | string;
        custom_eval_name: string;
        config?: Record<string, any>;
        mapping?: Record<string, string>;
        score?: number;
        rationale?: string | null;
        metadata?: Record<string, any> | null;
        model?: ModelChoices;
    }): Promise<EvalTag>;
    private validate;
    toDict(): Record<string, any>;
    toString(): string;
}
declare function prepareEvalTags(evalTags: IEvalTag[]): Record<string, any>[];
export { ProjectType, EvalTagType, EvalSpanKind, EvalName, ConfigField, EvalConfig, EvalMapping, getConfigForEval, getMappingForEval, validateFieldType, prepareEvalTags, IEvalTag, EvalTag, ModelChoices, };
