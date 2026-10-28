package traceaianthropic

import "context"

type contextKey int

const (
	sessionIDKey contextKey = iota
	userIDKey
)

// ContextWithSessionID returns a copy of ctx that carries a session ID. Every
// Messages call made with that context gets session.id, which Future AGI uses to
// group the call's trace into a session. A call on a context without one gets no
// session.id: the package never invents one.
func ContextWithSessionID(ctx context.Context, sessionID string) context.Context {
	return context.WithValue(ctx, sessionIDKey, sessionID)
}

// ContextWithUserID returns a copy of ctx that carries the ID of your end user.
// Every Messages call made with that context gets user.id. Pass an opaque ID,
// not an email address or a name.
func ContextWithUserID(ctx context.Context, userID string) context.Context {
	return context.WithValue(ctx, userIDKey, userID)
}

// SessionIDFromContext returns the session ID that ContextWithSessionID put in ctx.
func SessionIDFromContext(ctx context.Context) (string, bool) {
	v, ok := ctx.Value(sessionIDKey).(string)
	return v, ok && v != ""
}

// UserIDFromContext returns the user ID that ContextWithUserID put in ctx.
func UserIDFromContext(ctx context.Context) (string, bool) {
	v, ok := ctx.Value(userIDKey).(string)
	return v, ok && v != ""
}
