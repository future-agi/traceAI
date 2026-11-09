import { request } from 'node:http';
import { request as secureRequest } from 'node:https';
// Named ESM bindings captured before manual init must reach the same commit hook.
globalThis.__f4NamedRequest = process.env.F4_TRANSPORT === 'https' ? secureRequest : request;
await import('./f4-outgoing.cjs');
