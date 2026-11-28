// Package zerocode runs an Eino program whose only tracing code is the blank
// import of github.com/future-agi/traceAI/go/eino/auto against fake Future AGI
// and model endpoints. It is a separate, unpublished module so that the
// releasable module requires only Eino, go/auto, go/eino and OpenTelemetry, and
// never the Eino extension packages the test program uses.
package zerocode
