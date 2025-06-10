import {
  checkCustomEvalTemplateExists,
  CheckCustomEvalTemplateExistsResponse,
} from "./otel";

enum ProjectType {
  EXPERIMENT = "experiment",
  OBSERVE = "observe",
}

enum EvalTagType {
  OBSERVATION_SPAN = "OBSERVATION_SPAN_TYPE",
}

enum ModelChoices {
  TURING_LARGE = "turing_large",
  TURING_SMALL = "turing_small",
  PROTECT = "protect",
  PROTECT_FLASH = "protect_flash",
  TURING_FLASH = "turing_flash",
}

enum EvalSpanKind {
  TOOL = "TOOL",
  LLM = "LLM",
  RETRIEVER = "RETRIEVER",
  EMBEDDING = "EMBEDDING",
  AGENT = "AGENT",
  RERANKER = "RERANKER",
}

enum EvalName {
  CONVERSATION_COHERENCE = "conversation_coherence",
  CONVERSATION_RESOLUTION = "conversation_resolution",
  CONTENT_MODERATION = "content_moderation",
  CONTEXT_ADHERENCE = "context_adherence",
  PROMPT_PERPLEXITY = "prompt_perplexity",
  CONTEXT_RELEVANCE = "context_relevance",
  COMPLETENESS = "completeness",
  CONTEXT_SIMILARITY = "context_similarity",
  PII = "pii",
  TOXICITY = "toxicity",
  TONE = "tone",
  SEXIST = "sexist",
  PROMPT_INJECTION = "prompt_injection",
  NOT_GIBBERISH_TEXT = "not_gibberish_text",
  SAFE_FOR_WORK_TEXT = "safe_for_work_text",
  PROMPT_INSTRUCTION_ADHERENCE = "prompt_instruction_adherence",
  DATA_PRIVACY_COMPLIANCE = "data_privacy_compliance",
  IS_JSON = "is_json",
  ENDS_WITH = "ends_with",
  EQUALS = "equals",
  CONTAINS_ALL = "contains_all",
  LENGTH_LESS_THAN = "length_less_than",
  CONTAINS_NONE = "contains_none",
  REGEX = "regex",
  STARTS_WITH = "starts_with",
  API_CALL = "api_call",
  LENGTH_BETWEEN = "length_between",
  CUSTOM_CODE_EVALUATION = "custom_code_evaluation",
  AGENT_AS_JUDGE = "agent_as_judge",
  ONE_LINE = "one_line",
  CONTAINS_VALID_LINK = "contains_valid_link",
  IS_EMAIL = "is_email",
  LENGTH_GREATER_THAN = "length_greater_than",
  NO_VALID_LINKS = "no_valid_links",
  CONTAINS = "contains",
  CONTAINS_ANY = "contains_any",
  GROUNDEDNESS = "groundedness",
  ANSWER_SIMILARITY = "answer_similarity",
  EVAL_OUTPUT = "eval_output",
  EVAL_CONTEXT_RETRIEVAL_QUALITY = "eval_context_retrieval_quality",
  EVAL_IMAGE_INSTRUCTION = "eval_image_instruction",
  SCORE_EVAL = "score_eval",
  SUMMARY_QUALITY = "summary_quality",
  FACTUAL_ACCURACY = "factual_accuracy",
  TRANSLATION_ACCURACY = "translation_accuracy",
  CULTURAL_SENSITIVITY = "cultural_sensitivity",
  BIAS_DETECTION = "bias_detection",
  EVALUATE_LLM_FUNCTION_CALLING = "evaluate_llm_function_calling",
  AUDIO_TRANSCRIPTION = "audio_transcription",
  EVAL_AUDIO_DESCRIPTION = "eval_audio_description",
  AUDIO_QUALITY = "audio_quality",
  JSON_SCHEMA_VALIDATION = "json_schema_validation",
  CHUNK_ATTRIBUTION = "chunk_attribution",
  CHUNK_UTILIZATION = "chunk_utilization",
  EVAL_RANKING = "eval_ranking",
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

function getConfigForEval(evalName: EvalName): EvalConfig {
  const configs: Partial<Record<EvalName, EvalConfig>> = {
    [EvalName.CONVERSATION_COHERENCE]: {
      model: { type: "string", default: "gpt-4o-mini", required: false },
    },
    [EvalName.CONVERSATION_RESOLUTION]: {
      model: { type: "string", default: "gpt-4o-mini", required: false },
    },
    [EvalName.CONTENT_MODERATION]: {},
    [EvalName.CONTEXT_ADHERENCE]: {
      criteria: {
        type: "string",
        default:
          "check whether output contains any information which was not provided in the context.",
        required: false,
      },
    },
    [EvalName.PROMPT_PERPLEXITY]: {
      model: { type: "string", default: "gpt-4o-mini", required: false },
    },
    [EvalName.CONTEXT_RELEVANCE]: {
      check_internet: { type: "boolean", default: false, required: false },
    },
    [EvalName.COMPLETENESS]: {},
    [EvalName.CONTEXT_SIMILARITY]: {
      comparator: {
        type: "string",
        default: "CosineSimilarity",
        required: false,
      },
      failure_threshold: { type: "number", default: 0.5, required: false },
    },
    [EvalName.PII]: {},
    [EvalName.TOXICITY]: {},
    [EvalName.TONE]: {},
    [EvalName.SEXIST]: {},
    [EvalName.PROMPT_INJECTION]: {},
    [EvalName.NOT_GIBBERISH_TEXT]: {},
    [EvalName.SAFE_FOR_WORK_TEXT]: {},
    [EvalName.PROMPT_INSTRUCTION_ADHERENCE]: {},
    [EvalName.DATA_PRIVACY_COMPLIANCE]: {
      check_internet: { type: "boolean", default: false, required: false },
    },
    [EvalName.IS_JSON]: {},
    [EvalName.ENDS_WITH]: {
      case_sensitive: { type: "boolean", default: true, required: false },
      substring: { type: "string", default: null, required: true },
    },
    [EvalName.EQUALS]: {
      case_sensitive: { type: "boolean", default: true, required: false },
    },
    [EvalName.CONTAINS_ALL]: {
      case_sensitive: { type: "boolean", default: true, required: false },
      keywords: { type: "array", default: [], required: false },
    },
    [EvalName.LENGTH_LESS_THAN]: {
      max_length: { type: "number", default: 200, required: false },
    },
    [EvalName.CONTAINS_NONE]: {
      case_sensitive: { type: "boolean", default: true, required: false },
      keywords: { type: "array", default: [], required: false },
    },
    [EvalName.REGEX]: {
      pattern: { type: "string", default: "", required: false },
    },
    [EvalName.STARTS_WITH]: {
      substring: { type: "string", default: null, required: true },
      case_sensitive: { type: "boolean", default: true, required: false },
    },
    [EvalName.API_CALL]: {
      url: { type: "string", default: null, required: true },
      payload: { type: "object", default: {}, required: false },
      headers: { type: "object", default: {}, required: false },
    },
    [EvalName.LENGTH_BETWEEN]: {
      max_length: { type: "number", default: 200, required: false },
      min_length: { type: "number", default: 50, required: false },
    },
    [EvalName.CUSTOM_CODE_EVALUATION]: {
      code: { type: "string", default: null, required: false },
    },
    [EvalName.AGENT_AS_JUDGE]: {
      model: { type: "string", default: "gpt-4o-mini", required: false },
      eval_prompt: { type: "string", default: null, required: true },
      system_prompt: { type: "string", default: "", required: false },
    },
    [EvalName.ONE_LINE]: {},
    [EvalName.CONTAINS_VALID_LINK]: {},
    [EvalName.IS_EMAIL]: {},
    [EvalName.LENGTH_GREATER_THAN]: {
      min_length: { type: "number", default: 50, required: false },
    },
    [EvalName.NO_VALID_LINKS]: {},
    [EvalName.CONTAINS]: {
      case_sensitive: { type: "boolean", default: true, required: false },
      keyword: { type: "string", default: null, required: true },
    },
    [EvalName.CONTAINS_ANY]: {
      case_sensitive: { type: "boolean", default: true, required: false },
      keywords: { type: "array", default: [], required: false },
    },
    [EvalName.GROUNDEDNESS]: {},
    [EvalName.ANSWER_SIMILARITY]: {
      comparator: {
        type: "string",
        default: "CosineSimilarity",
        required: false,
      },
      failure_threshold: { type: "number", default: 0.5, required: false },
    },
    [EvalName.EVAL_OUTPUT]: {
      check_internet: { type: "boolean", default: false, required: false },
      criteria: {
        type: "string",
        default:
          "Check if the output follows the given input instructions, checking for completion of all requested tasks and adherence to specified constraints or formats.",
        required: false,
      },
    },
    [EvalName.EVAL_CONTEXT_RETRIEVAL_QUALITY]: {
      criteria: {
        type: "string",
        default:
          "Evaluate if the context is relevant and sufficient to support the output.",
        required: false,
      },
    },
    [EvalName.EVAL_IMAGE_INSTRUCTION]: {
      criteria: {
        type: "string",
        default:
          "Check if the output follows the given input instructions, checking for completion of all requested tasks and adherence to specified constraints or formats.",
        required: false,
      },
    },
    [EvalName.SCORE_EVAL]: {
      rule_prompt: {
        type: "string",
        default:
          "Check if the output follows the given input instructions, checking for completion of all requested tasks and adherence to specified constraints or formats.",
        required: false,
      },
      criteria: { type: "string", default: "", required: false },
      input: { type: "array", default: [], required: false },
    },
    [EvalName.SUMMARY_QUALITY]: {
      check_internet: { type: "boolean", default: false, required: false },
      criteria: {
        type: "string",
        default:
          "Check if the summary concisely captures the main points while maintaining accuracy and relevance to the original content.",
        required: false,
      },
    },
    [EvalName.FACTUAL_ACCURACY]: {
      criteria: {
        type: "string",
        default:
          "Check if the provided output is factually accurate based on the given information or the absence thereof.",
        required: false,
      },
      check_internet: { type: "boolean", default: false, required: false },
    },
    [EvalName.TRANSLATION_ACCURACY]: {
      check_internet: { type: "boolean", default: false, required: false },
      criteria: {
        type: "string",
        default:
          "Check if the language translation accurately conveys the meaning and context of the input in the output.",
        required: false,
      },
    },
    [EvalName.CULTURAL_SENSITIVITY]: {
      criteria: {
        type: "string",
        default: "Assesses given text for inclusivity and cultural awareness.",
        required: false,
      },
    },
    [EvalName.BIAS_DETECTION]: {
      criteria: {
        type: "string",
        default:
          "check whether given text has any forms of bias, promoting unfairness and unneutrality in it. Looking that input and context if provided.. If it is biased then return Failed else return Passed",
        required: false,
      },
    },
    [EvalName.EVALUATE_LLM_FUNCTION_CALLING]: {
      criteria: {
        type: "string",
        default:
          "Assess whether the output correctly identifies the need for a tool call and accurately includes the tool with the appropriate parameters extracted from the input.",
        required: false,
      },
    },
    [EvalName.AUDIO_TRANSCRIPTION]: {
      criteria: {
        type: "string",
        default:
          "determine the accuracy of the transcription of the given audio",
        required: false,
      },
    },
    [EvalName.EVAL_AUDIO_DESCRIPTION]: {
      criteria: {
        type: "string",
        default:
          "determine the if the description of the given audio matches the given audio",
        required: false,
      },
      model: { type: "string", default: "gemini-2.0-flash", required: false },
    },
    [EvalName.AUDIO_QUALITY]: {
      criteria: {
        type: "string",
        default: "determine the quality of the given audio",
        required: false,
      },
      model: { type: "string", default: "gemini-2.0-flash", required: false },
    },
    [EvalName.JSON_SCHEMA_VALIDATION]: {
      validations: { type: "array", default: [], required: false },
    },
    [EvalName.CHUNK_ATTRIBUTION]: {},
    [EvalName.CHUNK_UTILIZATION]: {},
    [EvalName.EVAL_RANKING]: {
      criteria: {
        type: "string",
        default:
          "Check if the summary concisely captures the main points while maintaining accuracy and relevance to the original content.",
        required: false,
      },
    },
  };

  if (!(evalName in configs)) {
    throw new Error(`No eval found with the following name: ${evalName}`);
  }

  return configs[evalName]!;
}

function getMappingForEval(evalName: EvalName): EvalMapping {
  const mappings: Partial<Record<EvalName, EvalMapping>> = {
    [EvalName.CONVERSATION_COHERENCE]: {
      output: { type: "string", required: true },
    },
    [EvalName.CONVERSATION_RESOLUTION]: {
      output: { type: "string", required: true },
    },
    [EvalName.CONTENT_MODERATION]: {
      text: { type: "string", required: true },
    },
    [EvalName.CONTEXT_ADHERENCE]: {
      context: { type: "string", required: true },
      output: { type: "string", required: true },
    },
    [EvalName.PROMPT_PERPLEXITY]: {
      input: { type: "string", required: true },
    },
    [EvalName.CONTEXT_RELEVANCE]: {
      context: { type: "string", required: true },
      input: { type: "string", required: true },
    },
    [EvalName.COMPLETENESS]: {
      input: { type: "string", required: true },
      output: { type: "string", required: true },
    },
    [EvalName.CONTEXT_SIMILARITY]: {
      context: { type: "string", required: true },
      response: { type: "string", required: true },
    },
    [EvalName.PII]: {
      input: { type: "string", required: true },
    },
    [EvalName.TOXICITY]: {
      input: { type: "string", required: true },
    },
    [EvalName.TONE]: {
      input: { type: "string", required: true },
    },
    [EvalName.SEXIST]: {
      input: { type: "string", required: true },
    },
    [EvalName.PROMPT_INJECTION]: {
      input: { type: "string", required: true },
    },
    [EvalName.SAFE_FOR_WORK_TEXT]: {
      response: { type: "string", required: true },
    },
    [EvalName.NOT_GIBBERISH_TEXT]: {
      response: { type: "string", required: true },
    },
    [EvalName.PROMPT_INSTRUCTION_ADHERENCE]: {
      output: { type: "string", required: true },
    },
    [EvalName.DATA_PRIVACY_COMPLIANCE]: {
      input: { type: "string", required: true },
    },
    [EvalName.IS_JSON]: {
      text: { type: "string", required: true },
    },
    [EvalName.ENDS_WITH]: {
      text: { type: "string", required: true },
    },
    [EvalName.EQUALS]: {
      text: { type: "string", required: true },
      expected_text: { type: "string", default: "expected", required: true },
    },
    [EvalName.CONTAINS_ALL]: {
      text: { type: "string", required: true },
    },
    [EvalName.LENGTH_LESS_THAN]: {
      text: { type: "string", required: true },
    },
    [EvalName.CONTAINS_NONE]: {
      text: { type: "string", required: true },
    },
    [EvalName.REGEX]: {
      text: { type: "string", required: true },
    },
    [EvalName.STARTS_WITH]: {
      text: { type: "string", required: true },
    },
    [EvalName.LENGTH_BETWEEN]: {
      text: { type: "string", required: true },
    },
    [EvalName.ONE_LINE]: {
      text: { type: "string", required: true },
    },
    [EvalName.CONTAINS_VALID_LINK]: {
      text: { type: "string", required: true },
    },
    [EvalName.IS_EMAIL]: {
      text: { type: "string", required: true },
    },
    [EvalName.LENGTH_GREATER_THAN]: {
      text: { type: "string", required: true },
    },
    [EvalName.NO_VALID_LINKS]: {
      text: { type: "string", required: true },
    },
    [EvalName.CONTAINS]: {
      text: { type: "string", required: true },
    },
    [EvalName.CONTAINS_ANY]: {
      text: { type: "string", required: true },
    },
    [EvalName.GROUNDEDNESS]: {
      output: { type: "string", required: true },
      input: { type: "string", required: true },
    },
    [EvalName.ANSWER_SIMILARITY]: {
      response: { type: "string", required: true },
      expected_response: { type: "string", required: true },
    },
    [EvalName.EVAL_OUTPUT]: {
      input: { type: "string", required: false },
      output: { type: "string", required: true },
      context: { type: "string", required: false },
    },
    [EvalName.EVAL_CONTEXT_RETRIEVAL_QUALITY]: {
      context: { type: "string", required: false },
      input: { type: "string", required: false },
      output: { type: "string", required: false },
    },
    [EvalName.EVAL_IMAGE_INSTRUCTION]: {
      input: { type: "string", required: true },
      image_url: { type: "string", required: true },
    },
    [EvalName.SCORE_EVAL]: {},
    [EvalName.SUMMARY_QUALITY]: {
      input: { type: "string", required: false },
      output: { type: "string", required: true },
      context: { type: "string", required: false },
    },
    [EvalName.FACTUAL_ACCURACY]: {
      input: { type: "string", required: false },
      output: { type: "string", required: true },
      context: { type: "string", required: false },
    },
    [EvalName.TRANSLATION_ACCURACY]: {
      input: { type: "string", required: true },
      output: { type: "string", required: true },
    },
    [EvalName.CULTURAL_SENSITIVITY]: {
      input: { type: "string", required: true },
    },
    [EvalName.BIAS_DETECTION]: {
      input: { type: "string", required: true },
    },
    [EvalName.EVALUATE_LLM_FUNCTION_CALLING]: {
      input: { type: "string", required: true },
      output: { type: "string", required: true },
    },
    [EvalName.API_CALL]: {
      response: { type: "string", required: true },
    },
    [EvalName.CUSTOM_CODE_EVALUATION]: {},
    [EvalName.AGENT_AS_JUDGE]: {},
    [EvalName.AUDIO_TRANSCRIPTION]: {
      "input audio": { type: "string", required: true },
      "input transcription": { type: "string", required: true },
    },
    [EvalName.EVAL_AUDIO_DESCRIPTION]: {
      "input audio": { type: "string", required: true },
    },
    [EvalName.AUDIO_QUALITY]: {
      "input audio": { type: "string", required: true },
    },
    [EvalName.JSON_SCHEMA_VALIDATION]: {
      actual_json: { type: "object", required: true },
      expected_json: { type: "string", required: true },
    },
    [EvalName.CHUNK_ATTRIBUTION]: {
      input: { type: "string", required: false },
      output: { type: "string", required: true },
      context: { type: "string", required: true },
    },
    [EvalName.CHUNK_UTILIZATION]: {
      input: { type: "string", required: false },
      output: { type: "string", required: true },
      context: { type: "string", required: true },
    },
    [EvalName.EVAL_RANKING]: {
      input: { type: "string", required: true },
      context: { type: "string", required: true },
    },
  };

  if (!(evalName in mappings)) {
    throw new Error(`No mapping definition found for eval: ${evalName}`);
  }

  return mappings[evalName]!;
}

function validateFieldType(
  key: string,
  expectedType: string,
  value: any
): void {
  const typeMap: Record<string, string> = {
    string: "string",
    number: "number",
    boolean: "boolean",
    object: "object",
    array: "object", // Arrays are objects in JavaScript
  };

  const expectedJsType = typeMap[expectedType];
  if (!expectedJsType) {
    throw new Error(`Unknown type '${expectedType}' for field '${key}'`);
  }

  if (expectedType === "array" && !Array.isArray(value)) {
    throw new Error(`Field '${key}' must be an array, got ${typeof value}`);
  } else if (expectedType !== "array" && typeof value !== expectedJsType) {
    throw new Error(
      `Field '${key}' must be of type '${expectedType}', got '${typeof value}'`
    );
  }
}

// Export both the interface and the class
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

class EvalTag implements IEvalTag {
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

  private constructor(params: {
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
  }) {
    this.type = params.type;
    this.value = params.value;
    this.eval_name = params.eval_name;
    this.custom_eval_name =
      params.custom_eval_name ?? (params.eval_name as string);
    this.config = params.config || {};
    this.mapping = params.mapping || {};
    this.score = params.score;
    this.rationale = params.rationale;
    this.metadata = params.metadata;
    this.model = params.model;
    // Don't call validate() here - use static factory method instead
  }

  // Static factory method for async creation with validation
  static async create(params: {
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
  }): Promise<EvalTag> {
    const tag = new EvalTag(params);
    await tag.validate();
    return tag;
  }

  private async validate(): Promise<void> {
    // Basic validation checks
    if (!Object.values(EvalSpanKind).includes(this.value)) {
      throw new Error(
        `value must be a EvalSpanKind enum, got ${typeof this.value}`
      );
    }

    if (!Object.values(EvalTagType).includes(this.type)) {
      throw new Error(
        `type must be an EvalTagType enum, got ${typeof this.type}`
      );
    }

    const customEvalTemplate: CheckCustomEvalTemplateExistsResponse =
      await checkCustomEvalTemplateExists(this.eval_name as string);

    if (!customEvalTemplate.result?.isUserEvalTemplate) {
      if (!Object.values(EvalName).includes(this.eval_name as EvalName)) {
        throw new Error(
          `eval_name must be a valid EvalName enum if its not a custom eval template, got ${
            this.eval_name
          }. Expected values are: ${Object.values(EvalName).join(", ")}`
        );
      }

      if (!this.model || !Object.values(ModelChoices).includes(this.model)) {
        throw new Error(
          `Model must be provied in case of fagi evals. Model must be a valid model name, got ${
            this.model
          }. Expected values are: ${Object.values(ModelChoices).join(", ")}`
        );
      }

      // Get expected config for this eval type
      const expectedConfig = getConfigForEval(this.eval_name as EvalName);

      // Validate config fields
      for (const [key, fieldConfig] of Object.entries(expectedConfig)) {
        if (!(key in this.config)) {
          if (fieldConfig.required) {
            throw new Error(
              `Required field '${key}' is missing from config for ${this.eval_name}`
            );
          }
          this.config[key] = fieldConfig.default;
        } else {
          validateFieldType(key, fieldConfig.type, this.config[key]);
        }
      }

      // Check for unexpected config fields
      for (const key in this.config) {
        if (!(key in expectedConfig)) {
          throw new Error(
            `Unexpected field '${key}' in config for ${
              this.eval_name
            }. Allowed fields are: ${Object.keys(expectedConfig).join(", ")}`
          );
        }
      }
    }

    // Get expected mapping for this eval type
    let expectedMapping = null;
    let requiredKeys: string[] = [];
    if (customEvalTemplate.result?.isUserEvalTemplate) {
      requiredKeys =
        customEvalTemplate.result?.evalTemplate?.config?.requiredKeys ?? [];
    } else {
      expectedMapping = getMappingForEval(this.eval_name as EvalName);
      for (const [key, fieldConfig] of Object.entries(expectedMapping)) {
        if (fieldConfig.required) {
          requiredKeys.push(key);
        }
      }
    }

    // Validate mapping fields
    for (const key of requiredKeys) {
      if (!(key in this.mapping)) {
        throw new Error(
          `Required mapping field '${key}' is missing for ${this.eval_name}`
        );
      }
    }

    // Check for unexpected mapping fields
    for (const key in this.mapping) {
      if (
        !customEvalTemplate.result?.isUserEvalTemplate &&
        expectedMapping &&
        !(key in expectedMapping)
      ) {
        throw new Error(
          `Unexpected mapping field '${key}' for ${
            this.eval_name
          }. Allowed fields are: ${Object.keys(expectedMapping).join(", ")}`
        );
      }
      if (typeof key !== "string") {
        throw new Error(`All mapping keys must be strings, got ${typeof key}`);
      }
      if (typeof this.mapping[key] !== "string") {
        throw new Error(
          `All mapping values must be strings, got ${typeof this.mapping[key]}`
        );
      }
    }
  }

  toDict(): Record<string, any> {
    return {
      type: this.type,
      value: this.value,
      eval_name: this.eval_name,
      config: this.config,
      mapping: this.mapping,
      custom_eval_name: this.custom_eval_name,
      score: this.score,
      rationale: this.rationale,
      metadata: this.metadata,
      model: this.model,
    };
  }

  toString(): string {
    return `EvalTag(type=${this.type}, value=${this.value}, eval_name=${this.eval_name})`;
  }
}

function prepareEvalTags(evalTags: IEvalTag[]): Record<string, any>[] {
  return evalTags.map((tag) => tag.toDict());
}

export {
  ProjectType,
  EvalTagType,
  EvalSpanKind,
  EvalName,
  ConfigField,
  EvalConfig,
  EvalMapping,
  getConfigForEval,
  getMappingForEval,
  validateFieldType,
  prepareEvalTags,
  // Export both the interface and the class
  IEvalTag,
  EvalTag,
  ModelChoices,
};
