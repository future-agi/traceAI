# @traceai/fi-semantic-conventions

Semantic conventions for OpenTelemetry instrumentation attributes used in TraceAI prototype and observe projects.

## Installation

```bash
npm install @traceai/fi-semantic-conventions
# or
yarn add @traceai/fi-semantic-conventions
# or
pnpm add @traceai/fi-semantic-conventions
```

## Module System Support

This package supports both **CommonJS** and **ESM** module systems for maximum compatibility.

### ESM (ES Modules)
```typescript
import { /* semantic convention constants */ } from '@traceai/fi-semantic-conventions';
```

### CommonJS
```typescript
const { /* semantic convention constants */ } = require('@traceai/fi-semantic-conventions');
```

### TypeScript Configuration

For optimal compatibility, ensure your `tsconfig.json` includes:

```json
{
  "compilerOptions": {
    "moduleResolution": "node",
    "esModuleInterop": true,
    "allowSyntheticDefaultImports": true
  }
}
```

The `module` setting can be `"commonjs"`, `"esnext"`, or any other module system your project requires.



