package traceaia2a

import (
	"context"
	"errors"
	"iter"
	"sync"

	"github.com/a2aproject/a2a-go/v2/a2a"
	"github.com/a2aproject/a2a-go/v2/a2aclient"
	"github.com/a2aproject/a2a-go/v2/a2asrv"
)

// Client preserves the SDK client's API and observes call completion. Create
// the SDK client with [NewClientFromCard] or [NewClientFromEndpoints] and install
// [NewInterceptor]. The wrapper does not create spans: it completes the
// interceptor's existing span when unary calls return or unwind, at clean stream
// EOF, or on early iterator abandonment. Results and stream events are recorded
// after all response policies, including rejections that cause the SDK to skip
// the tracing After callback, and before events are yielded to the caller.
type Client struct {
	*a2aclient.Client
	boundary *clientBoundary
}

// WrapClient retains legacy stream-completion handling for an existing SDK client.
// Unary calls retain interceptor-only behavior: the SDK cannot expose their final
// outcome when a response policy skips After.
//
// Deprecated: use [NewClientFromCard] or [NewClientFromEndpoints] to bind completion
// to the actual SDK client and invocation before any policy can make a nested call.
// Calling the embedded Client directly bypasses completion handling.
func WrapClient(client *a2aclient.Client) *Client { return &Client{Client: client} }

// SendMessage observes the final accepted result or returned policy error.
func (c *Client) SendMessage(ctx context.Context, req *a2a.SendMessageRequest) (a2a.SendMessageResult, error) {
	return observeUnary(ctx, "SendMessage", c.boundary, func(ctx context.Context) (a2a.SendMessageResult, error) {
		return c.Client.SendMessage(ctx, req)
	})
}

// GetTask observes completion even when a response policy rejects the snapshot.
func (c *Client) GetTask(ctx context.Context, req *a2a.GetTaskRequest) (*a2a.Task, error) {
	return observeUnary(ctx, "GetTask", c.boundary, func(ctx context.Context) (*a2a.Task, error) {
		return c.Client.GetTask(ctx, req)
	})
}

// CancelTask observes completion without changing the SDK's result or error.
func (c *Client) CancelTask(ctx context.Context, req *a2a.CancelTaskRequest) (*a2a.Task, error) {
	return observeUnary(ctx, "CancelTask", c.boundary, func(ctx context.Context) (*a2a.Task, error) {
		return c.Client.CancelTask(ctx, req)
	})
}

// Never inspect or wrap the application's panic value: even an a2a.Error used
// as a panic may contain private data, unrelated to an accepted agent response.
var errCallPanic = errors.New("a2a client call panicked")

func observeUnary[T any](ctx context.Context, method string, owner *clientBoundary, call func(context.Context) (T, error)) (T, error) {
	if ctx == nil {
		ctx = context.Background()
	}
	if owner == nil {
		return call(ctx)
	}
	completion := &streamCompletion{method: method, owner: owner}
	returned := false
	defer func() {
		if !returned {
			// Leave the original panic unwinding unchanged, including panic(nil).
			completion.end(errCallPanic)
		}
	}()
	result, err := call(context.WithValue(ctx, clientCompletionKey, completion))
	returned = true
	// Policies on either side of tracing may replace or reject the response.
	completion.recordResponse(result, err)
	return result, err
}

// SendStreamingMessage observes completion without changing events or errors.
func (c *Client) SendStreamingMessage(ctx context.Context, req *a2a.SendMessageRequest) iter.Seq2[a2a.Event, error] {
	return observeStream(ctx, clientCompletionKey, "SendStreamingMessage", c.boundary, func(ctx context.Context) iter.Seq2[a2a.Event, error] {
		return c.Client.SendStreamingMessage(ctx, req)
	})
}

// SubscribeToTask observes completion, including a persisted nonterminal snapshot.
func (c *Client) SubscribeToTask(ctx context.Context, req *a2a.SubscribeToTaskRequest) iter.Seq2[a2a.Event, error] {
	return observeStream(ctx, clientCompletionKey, "SubscribeToTask", c.boundary, func(ctx context.Context) iter.Seq2[a2a.Event, error] {
		return c.Client.SubscribeToTask(ctx, req)
	})
}

// WrapHandler adds iterator-completion handling to an SDK handler configured
// with [NewServerInterceptor]. Wrap the outermost RequestHandler, after any
// InterceptedHandler layers, and pass the result to the HTTP/gRPC transport or
// direct caller. It completes existing spans, never creates an additional span,
// and delegates non-streaming methods unchanged. Without this wrapper the SDK
// provides no After callback on clean EOF or early iterator abandonment.
func WrapHandler(handler a2asrv.RequestHandler) a2asrv.RequestHandler {
	return &completionHandler{RequestHandler: handler}
}

type completionHandler struct{ a2asrv.RequestHandler }

func (h *completionHandler) SendStreamingMessage(ctx context.Context, req *a2a.SendMessageRequest) iter.Seq2[a2a.Event, error] {
	return observeStream(ctx, serverCompletionKey, "SendStreamingMessage", nil, func(ctx context.Context) iter.Seq2[a2a.Event, error] {
		return h.RequestHandler.SendStreamingMessage(ctx, req)
	})
}

func (h *completionHandler) SubscribeToTask(ctx context.Context, req *a2a.SubscribeToTaskRequest) iter.Seq2[a2a.Event, error] {
	return observeStream(ctx, serverCompletionKey, "SubscribeToTask", nil, func(ctx context.Context) iter.Seq2[a2a.Event, error] {
		return h.RequestHandler.SubscribeToTask(ctx, req)
	})
}

type completionKey int

const (
	clientCompletionKey completionKey = iota
	serverCompletionKey
)

// Each call or iteration owns a slot. Bound clients also capture the SDK request
// at entry, before user policies can nest calls. Server and legacy stream wrappers
// keep their first-matching-Before behavior. Separate keys isolate inbound/outbound.
type streamCompletion struct {
	mu      sync.Mutex
	method  string
	state   *callState
	owner   *clientBoundary
	request *a2aclient.Request
}

// attachCompletion reports whether this call claimed its wrapper's completion
// slot. An inherited slot already owned by another call is not a completion hook.
func attachCompletion(ctx context.Context, key completionKey, st *callState) bool {
	if completion, _ := ctx.Value(key).(*streamCompletion); completion != nil {
		completion.mu.Lock()
		defer completion.mu.Unlock()
		if completion.state == nil && completion.method == st.method {
			completion.state = st
			return true
		}
	}
	return false
}

func (c *streamCompletion) recordResponse(payload any, err error) {
	c.mu.Lock()
	st := c.state
	c.mu.Unlock()
	if st != nil {
		st.recordResponse(payload, err)
	}
}

func (c *streamCompletion) end(err error) {
	c.mu.Lock()
	st := c.state
	c.mu.Unlock()
	if st == nil {
		return // No tracing interceptor was installed, or Before was skipped.
	}
	st.mu.Lock()
	defer st.mu.Unlock()
	if st.ended {
		return
	}
	if err != nil {
		st.fail(err)
	} else {
		st.finish()
	}
}

func observeStream(ctx context.Context, key completionKey, method string, owner *clientBoundary, stream func(context.Context) iter.Seq2[a2a.Event, error]) iter.Seq2[a2a.Event, error] {
	return func(yield func(a2a.Event, error) bool) {
		parent := ctx
		if parent == nil {
			parent = context.Background()
		}
		// Cancel only our child context on early abandonment, never the caller's.
		// Complete the span before canceling on clean EOF, otherwise its context
		// watcher would incorrectly turn a successful snapshot into an error.
		callCtx, cancel := context.WithCancel(parent)
		defer cancel()
		completion := &streamCompletion{method: method, owner: owner}
		callCtx = context.WithValue(callCtx, key, completion)
		var streamErr error
		completed := false
		defer func() {
			if streamErr == nil {
				streamErr = callCtx.Err()
			}
			if streamErr == nil && !completed {
				streamErr = context.Canceled
			}
			completion.end(streamErr)
		}()
		for event, err := range stream(callCtx) {
			if err != nil {
				streamErr = err
			}
			if owner != nil {
				// Bound clients defer After recording until every SDK policy has
				// accepted this event. Record before yield so caller mutations cannot
				// change telemetry. Legacy clients and servers still record in After.
				completion.recordResponse(event, err)
			}
			if !yield(event, err) {
				return
			}
		}
		completed = true
	}
}
