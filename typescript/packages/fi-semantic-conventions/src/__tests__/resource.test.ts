import { describe, it, expect } from '@jest/globals';
import { ATTR_PROJECT_NAME, ATTR_SERVICE_NAME } from '../resource/ResourceAttributes';

describe('Resource Attributes', () => {
  describe('Project Attributes', () => {
    it('should have correct project name attribute', () => {
      expect(ATTR_PROJECT_NAME).toBe('fi.project.name');
      expect(typeof ATTR_PROJECT_NAME).toBe('string');
    });

    it('should not be empty', () => {
      expect(ATTR_PROJECT_NAME.length).toBeGreaterThan(0);
    });

    it('should follow semantic naming convention', () => {
      expect(ATTR_PROJECT_NAME).toMatch(/^fi\./);
    });
  });

  describe('Service Attributes', () => {
    it('should have correct service name attribute', () => {
      expect(ATTR_SERVICE_NAME).toBe('SERVICE_NAME');
      expect(typeof ATTR_SERVICE_NAME).toBe('string');
    });

    it('should not be empty', () => {
      expect(ATTR_SERVICE_NAME.length).toBeGreaterThan(0);
    });

    it('should be uppercase', () => {
      expect(ATTR_SERVICE_NAME).toBe(ATTR_SERVICE_NAME.toUpperCase());
    });
  });

  describe('Attribute Consistency', () => {
    it('should have unique attribute names', () => {
      const attributes = [ATTR_PROJECT_NAME, ATTR_SERVICE_NAME];
      const uniqueAttributes = new Set(attributes);
      expect(uniqueAttributes.size).toBe(attributes.length);
    });

    it('should all be strings', () => {
      const attributes = [ATTR_PROJECT_NAME, ATTR_SERVICE_NAME];
      attributes.forEach(attr => {
        expect(typeof attr).toBe('string');
      });
    });

    it('should all be non-empty', () => {
      const attributes = [ATTR_PROJECT_NAME, ATTR_SERVICE_NAME];
      attributes.forEach(attr => {
        expect(attr.length).toBeGreaterThan(0);
        expect(attr.trim()).toBe(attr); // No leading/trailing whitespace
      });
    });
  });
}); 