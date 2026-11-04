package traceaia2a

import (
	"context"

	"go.opentelemetry.io/otel/attribute"
)

type ctxKey int

const (
	sessionKey ctxKey = iota
	userKey
)

// ContextWithSessionID returns a context that carries a session id. Every A2A
// call span started from it, or from a context derived from it, gets
// session.id, and the session id wins over the A2A context id (which is then
// kept as gen_ai.a2a.context.id). Future AGI groups traces with the same
// session.id into one session.
//
// OpenTelemetry attributes are not inherited by child spans, so the id rides
// on the context, not on a parent span. Spans your own code starts get it from
// [ContextAttributes].
func ContextWithSessionID(ctx context.Context, id string) context.Context {
	if id == "" {
		return ctx
	}
	return context.WithValue(ctx, sessionKey, id)
}

// ContextWithUserID returns a context that carries an end-user id, written as
// user.id the same way as [ContextWithSessionID]. Use an opaque id, not an
// email address.
func ContextWithUserID(ctx context.Context, id string) context.Context {
	if id == "" {
		return ctx
	}
	return context.WithValue(ctx, userKey, id)
}

// SessionIDFromContext returns the id set with [ContextWithSessionID].
func SessionIDFromContext(ctx context.Context) (string, bool) {
	if ctx == nil {
		return "", false
	}
	v, ok := ctx.Value(sessionKey).(string)
	return v, ok && v != ""
}

// UserIDFromContext returns the id set with [ContextWithUserID].
func UserIDFromContext(ctx context.Context) (string, bool) {
	if ctx == nil {
		return "", false
	}
	v, ok := ctx.Value(userKey).(string)
	return v, ok && v != ""
}

// ContextAttributes returns session.id and user.id for the ids ctx carries,
// and nothing for a bare context. It never invents an id. Add them to spans
// your own code starts so the whole trace carries the session:
//
//	ctx, span := tracer.Start(ctx, "agent", trace.WithAttributes(traceaia2a.ContextAttributes(ctx)...))
func ContextAttributes(ctx context.Context) []attribute.KeyValue {
	var kvs []attribute.KeyValue
	if id, ok := SessionIDFromContext(ctx); ok {
		kvs = append(kvs, attribute.String(SessionID, id))
	}
	if id, ok := UserIDFromContext(ctx); ok {
		kvs = append(kvs, attribute.String(UserID, id))
	}
	return kvs
}
