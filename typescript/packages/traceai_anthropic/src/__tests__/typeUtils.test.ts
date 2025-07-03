import { describe, it, expect } from '@jest/globals';
import { assertUnreachable, isString } from '../typeUtils';

describe('Type Utils', () => {
  describe('assertUnreachable', () => {
    it('should throw an error when called', () => {
      expect(() => {
        assertUnreachable('invalid' as never);
      }).toThrow('Unreachable');
    });

    it('should provide type safety in switch statements', () => {
      // This is a compile-time check - the function ensures exhaustive switch handling
      const testValue = 'option1' as 'option1' | 'option2';
      
      let result: string;
      switch (testValue) {
        case 'option1':
          result = 'first';
          break;
        case 'option2':
          result = 'second';
          break;
        default:
          // This line should cause a TypeScript error if switch is not exhaustive
          assertUnreachable(testValue);
          result = 'unreachable';
      }
      
      expect(result).toBe('first');
    });
  });

  describe('isString', () => {
    it('should return true for string values', () => {
      expect(isString('hello')).toBe(true);
      expect(isString('')).toBe(true);
      expect(isString('123')).toBe(true);
      expect(isString(' ')).toBe(true);
    });

    it('should return false for non-string values', () => {
      expect(isString(123)).toBe(false);
      expect(isString(null)).toBe(false);
      expect(isString(undefined)).toBe(false);
      expect(isString({})).toBe(false);
      expect(isString([])).toBe(false);
      expect(isString(true)).toBe(false);
      expect(isString(false)).toBe(false);
      expect(isString(Symbol('test'))).toBe(false);
    });

    it('should provide proper type narrowing', () => {
      const value: unknown = 'test';
      
      if (isString(value)) {
        // TypeScript should now know that value is a string
        expect(value.length).toBe(4);
        expect(value.toUpperCase()).toBe('TEST');
      } else {
        // This should not execute for our test case
        throw new Error('Type guard failed');
      }
    });

    it('should handle edge cases', () => {
      expect(isString(new String('test'))).toBe(false); // String object vs primitive
      expect(isString(String('test'))).toBe(true); // String constructor as function
    });
  });
});
