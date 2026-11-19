import { describe, it, expect } from '@jest/globals';
import { ATTR_PROJECT_NAME, ATTR_SERVICE_NAME } from '../index';

// Offline fake of OTel resourceFromAttributes: preserve the caller's keys and values.
// No provider registration, resource overlay, or project header supplies missing keys.
function fakeResourceFromAttributes(attributes: Record<string, string>) {
  return { attributes: { ...attributes } };
}

function customProviderResource() {
  return fakeResourceFromAttributes({
    [ATTR_PROJECT_NAME]: 'demo-project',
    [ATTR_SERVICE_NAME]: 'demo-service',
    project_type: 'observe',
  });
}

describe('Resource Attributes', () => {
  describe('Project Attributes', () => {
    it('should have correct project name attribute', () => {
      expect(ATTR_PROJECT_NAME).toBe('project_name');
      expect(typeof ATTR_PROJECT_NAME).toBe('string');
    });

    it('should not be empty', () => {
      expect(ATTR_PROJECT_NAME.length).toBeGreaterThan(0);
    });

    it('T6-TS-CORE-AUTO-003: supplies the collector project key without a project header', () => {
      const resource = customProviderResource();

      // The collector reads the literal project_name key, not fi.project.name.
      expect(resource.attributes['project_name']).toBe('demo-project');
      expect(resource.attributes['project_type']).toBe('observe');
      expect(Object.keys(resource.attributes)).not.toContain('fi.project.name');
    });
  });

  describe('Service Attributes', () => {
    it('should have correct service name attribute', () => {
      expect(ATTR_SERVICE_NAME).toBe('service.name');
      expect(typeof ATTR_SERVICE_NAME).toBe('string');
    });

    it('should not be empty', () => {
      expect(ATTR_SERVICE_NAME.length).toBeGreaterThan(0);
    });

    it('T6-TS-CORE-AUTO-003: supplies the standard OpenTelemetry service key', () => {
      const resource = customProviderResource();

      expect(resource.attributes['service.name']).toBe('demo-service');
      expect(Object.keys(resource.attributes)).not.toContain('SERVICE_NAME');
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