// VoltAgent 2.11 starts an optional models.dev registry refresh outside production.
// Offline unit tests use its shipped static registry, like a production app.
// Set this BEFORE imports; do not mock the processor or lifecycle.
// Only the separately isolated resume runner has the documented test-scoped exit.
// Real agents, streams, tools, exporters, forceFlush and shutdown still execute.
process.env.NODE_ENV = 'production';
