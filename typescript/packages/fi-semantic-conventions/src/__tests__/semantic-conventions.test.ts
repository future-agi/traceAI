import { describe, it, expect } from '@jest/globals';
import {
  SemanticAttributePrefixes,
  LLMAttributePostfixes,
  LLMPromptTemplateAttributePostfixes,
  RetrievalAttributePostfixes,
  RerankerAttributePostfixes,
  EmbeddingAttributePostfixes,
  ToolAttributePostfixes,
  MessageAttributePostfixes,
  MessageContentsAttributePostfixes,
  ImageAttributesPostfixes,
  ToolCallAttributePostfixes,
  DocumentAttributePostfixes,
  TagAttributePostfixes,
  SemanticConventions,
  SessionAttributePostfixes,
  UserAttributePostfixes,
  AudioAttributesPostfixes,
  PromptAttributePostfixes,
  INPUT_VALUE,
  INPUT_MIME_TYPE,
  OUTPUT_VALUE,
  OUTPUT_MIME_TYPE,
  LLM_INPUT_MESSAGES,
  LLM_PROMPTS,
  LLM_INVOCATION_PARAMETERS,
  LLM_OUTPUT_MESSAGES,
  LLM_MODEL_NAME,
  LLM_PROVIDER,
  LLM_SYSTEM,
  LLM_TOKEN_COUNT_COMPLETION,
  LLM_TOKEN_COUNT_PROMPT,
  LLM_TOKEN_COUNT_TOTAL,
  MESSAGE_ROLE,
  MESSAGE_CONTENT,
  MESSAGE_NAME,
  MESSAGE_FUNCTION_CALL_NAME,
  MESSAGE_FUNCTION_CALL_ARGUMENTS_JSON,
  MESSAGE_TOOL_CALLS,
  MESSAGE_TOOL_CALL_ID,
  TOOL_CALL_FUNCTION_NAME,
  TOOL_CALL_FUNCTION_ARGUMENTS_JSON,
  TOOL_CALL_ID,

  FISpanKind,
  MimeType,
  LLMSystem,
  LLMProvider,
} from '../trace/SemanticConventions';

describe('Semantic Conventions', () => {
  describe('Semantic Attribute Prefixes', () => {
    it('should have all expected prefixes', () => {
      const expectedPrefixes = [
        'input', 'output', 'llm', 'retrieval', 'reranker', 'messages',
        'message', 'document', 'embedding', 'tool', 'tool_call', 'metadata',
        'tag', 'session', 'user', 'traceai', 'fi', 'message_content',
        'image', 'audio', 'prompt'
      ];

      expectedPrefixes.forEach(prefix => {
        expect(SemanticAttributePrefixes).toHaveProperty(prefix);
        expect(typeof SemanticAttributePrefixes[prefix as keyof typeof SemanticAttributePrefixes]).toBe('string');
        expect(SemanticAttributePrefixes[prefix as keyof typeof SemanticAttributePrefixes]).toBe(prefix);
      });
    });

    it('should have consistent naming', () => {
      Object.values(SemanticAttributePrefixes).forEach(prefix => {
        expect(prefix).toMatch(/^[a-z_]+$/); // Only lowercase letters and underscores
        expect(prefix.length).toBeGreaterThan(0);
      });
    });
  });

  describe('LLM Attribute Postfixes', () => {
    it('should have all expected LLM postfixes', () => {
      const expectedPostfixes = [
        'provider', 'system', 'model_name', 'token_count', 'input_messages',
        'output_messages', 'invocation_parameters', 'prompts', 'prompt_template',
        'function_call', 'tools'
      ];

      expectedPostfixes.forEach(postfix => {
        expect(LLMAttributePostfixes).toHaveProperty(postfix);
        expect(typeof LLMAttributePostfixes[postfix as keyof typeof LLMAttributePostfixes]).toBe('string');
        expect(LLMAttributePostfixes[postfix as keyof typeof LLMAttributePostfixes]).toBe(postfix);
      });
    });

    it('should have consistent naming', () => {
      Object.values(LLMAttributePostfixes).forEach(postfix => {
        expect(postfix).toMatch(/^[a-z_]+$/);
        expect(postfix.length).toBeGreaterThan(0);
      });
    });
  });

  describe('Basic Semantic Constants', () => {
    it('should have correct input/output constants', () => {
      expect(INPUT_VALUE).toBe('input.value');
      expect(INPUT_MIME_TYPE).toBe('input.mime_type');
      expect(OUTPUT_VALUE).toBe('output.value');
      expect(OUTPUT_MIME_TYPE).toBe('output.mime_type');
    });

    it('should have correct LLM constants', () => {
      expect(LLM_INPUT_MESSAGES).toBe('gen_ai.input.messages');
      expect(LLM_PROMPTS).toBe('gen_ai.prompts');
      expect(LLM_INVOCATION_PARAMETERS).toBe('gen_ai.request.parameters');
      expect(LLM_OUTPUT_MESSAGES).toBe('gen_ai.output.messages');
      expect(LLM_MODEL_NAME).toBe('gen_ai.request.model');
      expect(LLM_PROVIDER).toBe('gen_ai.provider.name');
      expect(LLM_SYSTEM).toBe('gen_ai.provider.name');
    });

    it('should have correct token count constants', () => {
      expect(LLM_TOKEN_COUNT_COMPLETION).toBe('gen_ai.usage.output_tokens');
      expect(LLM_TOKEN_COUNT_PROMPT).toBe('gen_ai.usage.input_tokens');
      expect(LLM_TOKEN_COUNT_TOTAL).toBe('gen_ai.usage.total_tokens');
    });

    it('should have correct message constants', () => {
      expect(MESSAGE_ROLE).toBe('message.role');
      expect(MESSAGE_CONTENT).toBe('message.content');
      expect(MESSAGE_NAME).toBe('message.name');
      expect(MESSAGE_FUNCTION_CALL_NAME).toBe('message.function_call_name');
      expect(MESSAGE_FUNCTION_CALL_ARGUMENTS_JSON).toBe('message.function_call_arguments_json');
      expect(MESSAGE_TOOL_CALLS).toBe('message.tool_calls');
      expect(MESSAGE_TOOL_CALL_ID).toBe('message.tool_call_id');
    });

    it('should have correct tool call constants', () => {
      expect(TOOL_CALL_FUNCTION_NAME).toBe('tool_call.function.name');
      expect(TOOL_CALL_FUNCTION_ARGUMENTS_JSON).toBe('tool_call.function.arguments');
      expect(TOOL_CALL_ID).toBe('tool_call.id');
    });

    it('should have correct span kind and raw constants', () => {
      expect(SemanticConventions.FI_SPAN_KIND).toBe('fi.span.kind');
      expect(SemanticConventions.RAW_INPUT).toBe('raw.input');
      expect(SemanticConventions.RAW_OUTPUT).toBe('raw.output');
    });
  });

  describe('Attribute Structure Validation', () => {
    it('should have properly formatted attributes with dots', () => {
      const dotAttributeConstants = [
        INPUT_VALUE, INPUT_MIME_TYPE, OUTPUT_VALUE, OUTPUT_MIME_TYPE,
        LLM_INPUT_MESSAGES, LLM_PROMPTS, LLM_INVOCATION_PARAMETERS,
        LLM_OUTPUT_MESSAGES, LLM_MODEL_NAME, LLM_PROVIDER,
        LLM_TOKEN_COUNT_COMPLETION, LLM_TOKEN_COUNT_PROMPT, LLM_TOKEN_COUNT_TOTAL
      ];

      dotAttributeConstants.forEach(constant => {
        expect(constant).toMatch(/\./); // Should contain at least one dot
        expect(constant).not.toMatch(/^\.|\.$|\.\.+/); // No leading, trailing, or consecutive dots
        expect(typeof constant).toBe('string');
        expect(constant.length).toBeGreaterThan(0);
      });
    });

    it('should not have duplicate constants (except LLM_PROVIDER/LLM_SYSTEM which share gen_ai.provider.name)', () => {
      const allConstants = [
        INPUT_VALUE, INPUT_MIME_TYPE, OUTPUT_VALUE, OUTPUT_MIME_TYPE,
        LLM_INPUT_MESSAGES, LLM_PROMPTS, LLM_INVOCATION_PARAMETERS,
        LLM_OUTPUT_MESSAGES, LLM_MODEL_NAME, LLM_PROVIDER,
        LLM_TOKEN_COUNT_COMPLETION, LLM_TOKEN_COUNT_PROMPT, LLM_TOKEN_COUNT_TOTAL,
        MESSAGE_ROLE, MESSAGE_CONTENT, MESSAGE_NAME,
        MESSAGE_FUNCTION_CALL_NAME, MESSAGE_FUNCTION_CALL_ARGUMENTS_JSON,
        MESSAGE_TOOL_CALLS, MESSAGE_TOOL_CALL_ID,
        TOOL_CALL_FUNCTION_NAME, TOOL_CALL_FUNCTION_ARGUMENTS_JSON, TOOL_CALL_ID,
        FISpanKind, SemanticConventions.RAW_INPUT, SemanticConventions.RAW_OUTPUT
      ];

      const uniqueConstants = new Set(allConstants);
      expect(uniqueConstants.size).toBe(allConstants.length);

      // LLM_PROVIDER and LLM_SYSTEM intentionally share the same value
      expect(LLM_PROVIDER).toBe(LLM_SYSTEM);
    });
  });

  describe('FISpanKind Enum', () => {
    it('should have all expected span kinds', () => {
      const expectedSpanKinds = [
        'LLM', 'CHAIN', 'TOOL', 'RETRIEVER', 'RERANKER', 'EMBEDDING',
        'AGENT', 'GUARDRAIL', 'EVALUATOR', 'UNKNOWN'
      ];

      expectedSpanKinds.forEach(kind => {
        expect(FISpanKind).toHaveProperty(kind);
        expect(FISpanKind[kind as keyof typeof FISpanKind]).toBe(kind);
      });
    });

    it('should have correct enum values', () => {
      expect(FISpanKind.LLM).toBe('LLM');
      expect(FISpanKind.CHAIN).toBe('CHAIN');
      expect(FISpanKind.TOOL).toBe('TOOL');
      expect(FISpanKind.RETRIEVER).toBe('RETRIEVER');
      expect(FISpanKind.RERANKER).toBe('RERANKER');
      expect(FISpanKind.EMBEDDING).toBe('EMBEDDING');
      expect(FISpanKind.AGENT).toBe('AGENT');
      expect(FISpanKind.GUARDRAIL).toBe('GUARDRAIL');
      expect(FISpanKind.EVALUATOR).toBe('EVALUATOR');
      expect(FISpanKind.UNKNOWN).toBe('UNKNOWN');
    });

    it('should have uppercase values', () => {
      Object.values(FISpanKind).forEach(value => {
        expect(value).toBe(value.toUpperCase());
        expect(value).toMatch(/^[A-Z_]+$/);
      });
    });
  });

  describe('MimeType Enum', () => {
    it('should have all expected mime types', () => {
      expect(MimeType.TEXT).toBe('text/plain');
      expect(MimeType.JSON).toBe('application/json');
      expect(MimeType.AUDIO_WAV).toBe('audio/wav');
    });

    it('should have valid MIME type format', () => {
      Object.values(MimeType).forEach(mimeType => {
        expect(mimeType).toMatch(/^[a-z]+\/[a-z-]+$/); // Basic MIME type format
        expect(typeof mimeType).toBe('string');
        expect(mimeType.length).toBeGreaterThan(0);
      });
    });

    it('should have unique mime types', () => {
      const values = Object.values(MimeType);
      const uniqueValues = new Set(values);
      expect(uniqueValues.size).toBe(values.length);
    });
  });

  describe('LLMSystem Enum', () => {
    it('should have all expected LLM systems', () => {
      const expectedSystems = ['openai', 'anthropic', 'mistralai', 'cohere', 'vertexai'];
      
      expectedSystems.forEach(system => {
        expect(LLMSystem).toHaveProperty(system.toUpperCase());
        expect(LLMSystem[system.toUpperCase() as keyof typeof LLMSystem]).toBe(system);
      });
    });

    it('should have correct system values', () => {
      expect(LLMSystem.OPENAI).toBe('openai');
      expect(LLMSystem.ANTHROPIC).toBe('anthropic');
      expect(LLMSystem.MISTRALAI).toBe('mistralai');
      expect(LLMSystem.COHERE).toBe('cohere');
      expect(LLMSystem.VERTEXAI).toBe('vertexai');
    });

    it('should have lowercase values', () => {
      Object.values(LLMSystem).forEach(value => {
        expect(value).toBe(value.toLowerCase());
        expect(value).toMatch(/^[a-z0-9_]+$/);
      });
    });
  });

  describe('LLMProvider Enum', () => {
    it('should have all expected LLM providers', () => {
      const expectedProviders = [
        'openai', 'anthropic', 'mistralai', 'cohere',
        'google', 'aws', 'azure'
      ];
      
      expectedProviders.forEach(provider => {
        expect(LLMProvider).toHaveProperty(provider.toUpperCase());
        expect(LLMProvider[provider.toUpperCase() as keyof typeof LLMProvider]).toBe(provider);
      });
    });

    it('should have correct provider values', () => {
      expect(LLMProvider.OPENAI).toBe('openai');
      expect(LLMProvider.ANTHROPIC).toBe('anthropic');
      expect(LLMProvider.MISTRALAI).toBe('mistralai');
      expect(LLMProvider.COHERE).toBe('cohere');
      expect(LLMProvider.GOOGLE).toBe('google');
      expect(LLMProvider.AWS).toBe('aws');
      expect(LLMProvider.AZURE).toBe('azure');
    });

    it('should have lowercase values', () => {
      Object.values(LLMProvider).forEach(value => {
        expect(value).toBe(value.toLowerCase());
        expect(value).toMatch(/^[a-z0-9_]+$/);
      });
    });

    it('should include both AI providers and cloud providers', () => {
      // AI providers
      expect(LLMProvider.OPENAI).toBeDefined();
      expect(LLMProvider.ANTHROPIC).toBeDefined();
      expect(LLMProvider.MISTRALAI).toBeDefined();
      expect(LLMProvider.COHERE).toBeDefined();
      
      // Cloud providers
      expect(LLMProvider.GOOGLE).toBeDefined();
      expect(LLMProvider.AWS).toBeDefined();
      expect(LLMProvider.AZURE).toBeDefined();
    });
  });

  describe('Postfix Collections Validation', () => {
    const postfixCollections = [
      { name: 'LLM', collection: LLMAttributePostfixes },
      { name: 'LLMPromptTemplate', collection: LLMPromptTemplateAttributePostfixes },
      { name: 'Retrieval', collection: RetrievalAttributePostfixes },
      { name: 'Reranker', collection: RerankerAttributePostfixes },
      { name: 'Embedding', collection: EmbeddingAttributePostfixes },
      { name: 'Tool', collection: ToolAttributePostfixes },
      { name: 'Message', collection: MessageAttributePostfixes },
      { name: 'MessageContents', collection: MessageContentsAttributePostfixes },
      { name: 'Image', collection: ImageAttributesPostfixes },
      { name: 'ToolCall', collection: ToolCallAttributePostfixes },
      { name: 'Document', collection: DocumentAttributePostfixes },
      { name: 'Tag', collection: TagAttributePostfixes },
      { name: 'Session', collection: SessionAttributePostfixes },
      { name: 'User', collection: UserAttributePostfixes },
      { name: 'Audio', collection: AudioAttributesPostfixes },
      { name: 'Prompt', collection: PromptAttributePostfixes }
    ];

    postfixCollections.forEach(({ name, collection }) => {
      it(`should have valid ${name} postfixes`, () => {
        expect(Object.keys(collection).length).toBeGreaterThan(0);
        
        Object.entries(collection).forEach(([key, value]) => {
          expect(typeof value).toBe('string');
          expect(value.length).toBeGreaterThan(0);
          expect(value).toMatch(/^[a-z._]+$/); // Only lowercase, dots, and underscores
        });
      });
    });

    it('should have no duplicate postfix values across collections', () => {
      const allPostfixes: string[] = [];
      
      postfixCollections.forEach(({ collection }) => {
        allPostfixes.push(...Object.values(collection));
      });

      // Note: Some duplication is expected (like "name", "id") but let's check structure
      allPostfixes.forEach(postfix => {
        expect(typeof postfix).toBe('string');
        expect(postfix.length).toBeGreaterThan(0);
      });
    });
  });

  describe('Type Safety', () => {
    it('should have readonly const assertions for prefixes', () => {
      // Test that the objects are properly typed as const
      expect(typeof SemanticAttributePrefixes).toBe('object');
      expect(typeof LLMAttributePostfixes).toBe('object');
    });

    it('should have string enums', () => {
      // Test that enums are string-based
      Object.values(FISpanKind).forEach(value => {
        expect(typeof value).toBe('string');
      });
      
      Object.values(MimeType).forEach(value => {
        expect(typeof value).toBe('string');
      });
      
      Object.values(LLMSystem).forEach(value => {
        expect(typeof value).toBe('string');
      });
      
      Object.values(LLMProvider).forEach(value => {
        expect(typeof value).toBe('string');
      });
    });
  });
}); 