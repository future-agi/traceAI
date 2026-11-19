// Node awaits --import before OpenClaw starts. Never install a competing model instrumentor.
import preload from '../dist/preload.js';
await preload.bootstrap().catch(() => {});
