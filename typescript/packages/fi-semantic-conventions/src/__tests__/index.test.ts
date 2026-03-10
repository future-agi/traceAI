import { describe, it, expect } from '@jest/globals';
import * as semanticConventions from '../index';

// Import key exports to test integration
import {
  ATTR_PROJECT_NAME,
  ATTR_SERVICE_NAME,
  SemanticAttributePrefixes,
  LLMAttributePostfixes,
  INPUT_VALUE,
  OUTPUT_VALUE,
  LLM_MODEL_NAME,
  SemanticConventions,
  FISpanKind,
  MimeType,
  LLMSystem,
  LLMProvider,
} from '../index';

describe('Semantic Conventions Package Integration', () => {
  describe('Main Package Exports', () => {
    it('should export all expected modules', () => {
      expect(semanticConventions).toBeDefined();
      expect(typeof semanticConventions).toBe('object');
    });

    it('should have resource attributes', () => {
      expect(ATTR_PROJECT_NAME).toBeDefined();
      expect(ATTR_SERVICE_NAME).toBeDefined();
      expect(typeof ATTR_PROJECT_NAME).toBe('string');
      expect(typeof ATTR_SERVICE_NAME).toBe('string');
    });

    it('should have semantic prefixes and postfixes', () => {
      expect(SemanticAttributePrefixes).toBeDefined();
      expect(LLMAttributePostfixes).toBeDefined();
      expect(typeof SemanticAttributePrefixes).toBe('object');
      expect(typeof LLMAttributePostfixes).toBe('object');
    });

    it('should have semantic constants', () => {
      expect(INPUT_VALUE).toBeDefined();
      expect(OUTPUT_VALUE).toBeDefined();
      expect(LLM_MODEL_NAME).toBeDefined();
      expect(typeof INPUT_VALUE).toBe('string');
      expect(typeof OUTPUT_VALUE).toBe('string');
      expect(typeof LLM_MODEL_NAME).toBe('string');
    });

    it('should have enums', () => {
      expect(FISpanKind).toBeDefined();
      expect(MimeType).toBeDefined();
      expect(LLMSystem).toBeDefined();
      expect(LLMProvider).toBeDefined();
      expect(typeof FISpanKind).toBe('object');
      expect(typeof MimeType).toBe('object');
      expect(typeof LLMSystem).toBe('object');
      expect(typeof LLMProvider).toBe('object');
    });
  });

  describe('Cross-Module Integration', () => {
    it('should construct valid attributes using prefixes and postfixes', () => {
      // LLM constants now use gen_ai.* namespace, not llm.* prefix+postfix
      const constructedLLMModel = `${SemanticAttributePrefixes.llm}.${LLMAttributePostfixes.model_name}`;
      expect(constructedLLMModel).toBe('llm.model_name');
      // LLM_MODEL_NAME is now a gen_ai constant
      expect(LLM_MODEL_NAME).toBe('gen_ai.request.model');
    });

    it('should have consistent attribute naming patterns', () => {
      // Test that non-LLM constants still follow prefix patterns
      expect(INPUT_VALUE).toBe(`${SemanticAttributePrefixes.input}.value`);
      expect(OUTPUT_VALUE).toBe(`${SemanticAttributePrefixes.output}.value`);

      // LLM constants now use OTEL GenAI convention
      expect(LLM_MODEL_NAME).toBe('gen_ai.request.model');
    });

    it('should have enum values that work with semantic constants', () => {
      // Test that enum values are appropriate for use with the constants
      expect(Object.values(FISpanKind)).toContain('LLM');
      expect(Object.values(LLMSystem)).toContain('openai');
      expect(Object.values(LLMProvider)).toContain('openai');
      expect(Object.values(MimeType)).toContain('application/json');
      
      // Test that semantic conventions object has expected properties
      expect(SemanticConventions.FI_SPAN_KIND).toBeDefined();
      expect(SemanticConventions.RAW_INPUT).toBeDefined();
      expect(SemanticConventions.RAW_OUTPUT).toBeDefined();
    });
  });

  describe('Export Structure Validation', () => {
    it('should not have undefined exports', () => {
      const exportedKeys = Object.keys(semanticConventions);
      
      exportedKeys.forEach(key => {
        expect(semanticConventions[key as keyof typeof semanticConventions]).toBeDefined();
      });
    });

    it('should have expected number of exports', () => {
      const exportedKeys = Object.keys(semanticConventions);
      // Should have a substantial number of exports (all the constants and enums)
      expect(exportedKeys.length).toBeGreaterThan(50);
    });

    it('should export both constants and enums', () => {
      const exportedKeys = Object.keys(semanticConventions);
      
      // Should include resource attributes
      expect(exportedKeys).toContain('ATTR_PROJECT_NAME');
      expect(exportedKeys).toContain('ATTR_SERVICE_NAME');
      
      // Should include semantic constants
      expect(exportedKeys).toContain('INPUT_VALUE');
      expect(exportedKeys).toContain('OUTPUT_VALUE');
      expect(exportedKeys).toContain('LLM_MODEL_NAME');
      
      // Should include enums
      expect(exportedKeys).toContain('FISpanKind');
      expect(exportedKeys).toContain('MimeType');
      expect(exportedKeys).toContain('LLMSystem');
      expect(exportedKeys).toContain('LLMProvider');
    });
  });

  describe('Type Consistency', () => {
    it('should have consistent string constants', () => {
      const stringConstants = [
        ATTR_PROJECT_NAME,
        ATTR_SERVICE_NAME,
        INPUT_VALUE,
        OUTPUT_VALUE,
        LLM_MODEL_NAME,
      ];

      stringConstants.forEach(constant => {
        expect(typeof constant).toBe('string');
        expect(constant.length).toBeGreaterThan(0);
        expect(constant.trim()).toBe(constant); // No whitespace
      });
    });

    it('should have consistent object structures for prefixes/postfixes', () => {
      const objectStructures = [
        SemanticAttributePrefixes,
        LLMAttributePostfixes,
      ];

      objectStructures.forEach(obj => {
        expect(typeof obj).toBe('object');
        expect(obj).not.toBeNull();
        expect(Object.keys(obj).length).toBeGreaterThan(0);
        
        Object.values(obj).forEach(value => {
          expect(typeof value).toBe('string');
          expect(value.length).toBeGreaterThan(0);
        });
      });
    });

    it('should have consistent enum structures', () => {
      const enums = [FISpanKind, MimeType, LLMSystem, LLMProvider];

      enums.forEach(enumObj => {
        expect(typeof enumObj).toBe('object');
        expect(enumObj).not.toBeNull();
        expect(Object.keys(enumObj).length).toBeGreaterThan(0);
        
        Object.values(enumObj).forEach(value => {
          expect(typeof value).toBe('string');
          expect(value.length).toBeGreaterThan(0);
        });
      });
    });
  });

  describe('Import Performance', () => {
    it('should import quickly', () => {
      const start = process.hrtime.bigint();
      require('../index');
      const end = process.hrtime.bigint();
      
      const durationMs = Number(end - start) / 1_000_000;
      expect(durationMs).toBeLessThan(50); // Less than 50ms
    });

    it('should not cause memory leaks on repeated imports', () => {
      const initialMemory = process.memoryUsage().heapUsed;
      
      // Import multiple times
      for (let i = 0; i < 5; i++) {
        delete require.cache[require.resolve('../index')];
        require('../index');
      }
      
      // Force garbage collection if available
      if (global.gc) {
        global.gc();
      }
      
      const finalMemory = process.memoryUsage().heapUsed;
      const memoryIncrease = finalMemory - initialMemory;
      
      // Should not increase memory by more than 500KB
      expect(memoryIncrease).toBeLessThan(500 * 1024);
    });
  });

  describe('Module Isolation', () => {
    it('should work with ES6 and CommonJS imports', () => {
      // Test CommonJS require
      expect(() => {
        const cjsImport = require('../index');
        expect(cjsImport.INPUT_VALUE).toBeDefined();
      }).not.toThrow();

      // Test ES6 import (already tested above with import statements)
      expect(INPUT_VALUE).toBeDefined();
    });

    it('should maintain referential integrity across imports', () => {
      const import1 = require('../index');
      const import2 = require('../index');
      
      expect(import1.INPUT_VALUE).toBe(import2.INPUT_VALUE);
      expect(import1.FISpanKind).toBe(import2.FISpanKind);
    });
  });
}); 