import { describe, it, expect } from '@jest/globals';
import { assertUnreachable, isString } from '../typeUtils';

describe('Type Utils', () => {
  describe('isString', () => {
    it('should return true for string values', () => {
      expect(isString('hello')).toBe(true);
      expect(isString('')).toBe(true);
      expect(isString('123')).toBe(true);
    });

    it('should return false for non-string values', () => {
      expect(isString(123)).toBe(false);
      expect(isString(true)).toBe(false);
      expect(isString(null)).toBe(false);
      expect(isString(undefined)).toBe(false);
      expect(isString({})).toBe(false);
      expect(isString([])).toBe(false);
    });
  });

  describe('assertUnreachable', () => {
    it('should throw an error when called', () => {
      expect(() => {
        assertUnreachable('unexpected' as never);
      }).toThrow('Unreachable');
    });

    it('should handle null values', () => {
      expect(() => {
        assertUnreachable(null as never);
      }).toThrow('Unreachable');
    });

    it('should be used for exhaustive type checking', () => {
      type Status = 'pending' | 'success' | 'error';
      
      const getStatusMessage = (status: Status): string => {
        switch (status) {
          case 'pending':
            return 'Loading...';
          case 'success':
            return 'Complete!';
          case 'error':
            return 'Failed!';
          default:
            // TypeScript will catch if we miss a case
            return assertUnreachable(status);
        }
      };

      expect(getStatusMessage('pending')).toBe('Loading...');
      expect(getStatusMessage('success')).toBe('Complete!');
      expect(getStatusMessage('error')).toBe('Failed!');
    });
  });
}); 