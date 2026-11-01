import { describe, it, expect } from '@jest/globals';
import { VERSION } from '../version';

describe('Version', () => {
  it('should export VERSION constant', () => {
    expect(VERSION).toBeDefined();
    expect(typeof VERSION).toBe('string');
  });

  it('should have a valid version format', () => {
    expect(VERSION).toMatch(/^\d+\.\d+\.\d+$/);
  });

  it('should match the package version', () => {
    expect(VERSION).toBe(require("../../package.json").version);
  });
});
