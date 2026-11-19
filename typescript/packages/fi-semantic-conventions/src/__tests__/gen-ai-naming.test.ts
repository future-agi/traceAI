import { describe, expect, it } from '@jest/globals';
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import * as conventions from '../index';

// These canonical names and wire values match Python's SpanAttributes.
const aliases: Array<[string, string, string]> = [
  ["LLM_INPUT_MESSAGES", "GEN_AI_INPUT_MESSAGES", "gen_ai.input.messages"],
  ["LLM_PROMPTS", "GEN_AI_PROMPTS", "gen_ai.prompts"],
  ["LLM_INVOCATION_PARAMETERS", "GEN_AI_REQUEST_PARAMETERS", "gen_ai.request.parameters"],
  ["LLM_OUTPUT_MESSAGES", "GEN_AI_OUTPUT_MESSAGES", "gen_ai.output.messages"],
  ["LLM_MODEL_NAME", "GEN_AI_REQUEST_MODEL", "gen_ai.request.model"],
  ["LLM_PROVIDER", "GEN_AI_PROVIDER_NAME", "gen_ai.provider.name"],
  ["LLM_SYSTEM", "GEN_AI_PROVIDER_NAME", "gen_ai.provider.name"],
  ["LLM_TOKEN_COUNT_COMPLETION", "GEN_AI_USAGE_OUTPUT_TOKENS", "gen_ai.usage.output_tokens"],
  ["LLM_TOKEN_COUNT_COMPLETION_DETAILS_REASONING", "GEN_AI_USAGE_OUTPUT_TOKENS_REASONING", "gen_ai.usage.output_tokens.reasoning"],
  ["LLM_TOKEN_COUNT_COMPLETION_DETAILS_AUDIO", "GEN_AI_USAGE_OUTPUT_TOKENS_AUDIO", "gen_ai.usage.output_tokens.audio"],
  ["LLM_TOKEN_COUNT_PROMPT", "GEN_AI_USAGE_INPUT_TOKENS", "gen_ai.usage.input_tokens"],
  ["LLM_TOKEN_COUNT_PROMPT_DETAILS_CACHE_WRITE", "GEN_AI_USAGE_CACHE_WRITE_TOKENS", "gen_ai.usage.cache_write_tokens"],
  ["LLM_TOKEN_COUNT_PROMPT_DETAILS_CACHE_READ", "GEN_AI_USAGE_CACHE_READ_TOKENS", "gen_ai.usage.cache_read_tokens"],
  ["LLM_TOKEN_COUNT_PROMPT_DETAILS_AUDIO", "GEN_AI_USAGE_INPUT_TOKENS_AUDIO", "gen_ai.usage.input_tokens.audio"],
  ["LLM_TOKEN_COUNT_TOTAL", "GEN_AI_USAGE_TOTAL_TOKENS", "gen_ai.usage.total_tokens"],
  ["LLM_FUNCTION_CALL", "GEN_AI_TOOL_CALL", "gen_ai.tool.call"],
  ["LLM_TOOLS", "GEN_AI_TOOL_DEFINITIONS", "gen_ai.tool.definitions"],
];

// Read the reference at test time so Python renames cannot silently drift.
const pythonSource = readFileSync(
  resolve(__dirname, '../../../../../python/fi_instrumentation/fi_types.py'),
  'utf8',
);
const pythonNames = new Map(
  [...pythonSource.matchAll(/^\s+(GEN_AI_[A-Z0-9_]+)\s*=\s*["'](gen_ai\.[^"']+)["']/gm)]
    .map(([, name, value]) => [value, name]),
);
const publicSurfaces: Array<[string, Record<string, unknown>]> = [
  ['named exports', conventions],
  ['SemanticConventions', conventions.SemanticConventions],
];

describe('GenAI constant naming', () => {
  it('loads Python canonical attributes rather than passing with an empty reference', () => {
    expect(pythonNames.size).toBeGreaterThan(0);
    expect(pythonNames.get('gen_ai.input.messages')).toBe('GEN_AI_INPUT_MESSAGES');
  });

  it.each(publicSurfaces)('matches Python for every GenAI value on %s', (_label, surface) => {
    const attributes = Object.entries(surface).filter(
      (entry): entry is [string, string] =>
        typeof entry[1] === 'string' && entry[1].startsWith('gen_ai.'),
    );
    expect(attributes.length).toBeGreaterThan(0);
    for (const [name, value] of attributes) {
      // All current TS values have a Python counterpart. A new TS-only key must
      // get an explicitly documented canonical name, not a silent fallback.
      const canonical = pythonNames.get(value);
      expect({ name, value, canonical }).toEqual({ name, value, canonical: expect.any(String) });
      expect(surface[canonical!]).toBe(value);
      if (name.startsWith('GEN_AI_')) {
        expect(name).toBe(canonical);
      } else {
        expect(aliases).toContainEqual([name, canonical, value]);
      }
    }
  });
  it.each(aliases)('%s has the canonical %s export for %s', (legacy, canonical, value) => {
    // Look up exports at runtime so missing names fail assertions, not compilation.
    const exports: Record<string, unknown> = conventions;
    const attributes: Record<string, unknown> = conventions.SemanticConventions;
    expect(exports[canonical]).toBe(value);
    expect(attributes[canonical]).toBe(value);
    expect(exports[legacy]).toBe(value);
    expect(attributes[legacy]).toBe(value);
  });
});
