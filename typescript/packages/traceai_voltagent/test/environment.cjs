// VoltAgent 2.11 starts an optional models.dev registry refresh outside production.
// Offline unit tests use its shipped static registry, like a production app.
// Set this BEFORE imports; do not forceExit or mock the processor/lifecycle.
// Real agents, streams, tools, exporters, forceFlush and shutdown still execute.
process.env.NODE_ENV = 'production';
