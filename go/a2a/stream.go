package traceaia2a

import (
	"context"
	"iter"
	"sync"

	"github.com/a2aproject/a2a-go/v2/a2a"
	"github.com/a2aproject/a2a-go/v2/a2aclient"
	"github.com/a2aproject/a2a-go/v2/a2asrv"
)

// Client preserves the SDK client's API and observes stream completion. Create
// the SDK client with [NewInterceptor], then call [WrapClient]. The wrapper does
// not create spans: it completes the interceptor's existing span at clean EOF,
// including snapshots with no terminal event, or on early iterator abandonment.
// All non-streaming methods are delegated unchanged to the embedded client.
type Client struct{ *a2aclient.Client }

// WrapClient adds iterator-completion handling to an instrumented SDK client.
// Use the returned client for streaming calls; calling its embedded Client
// directly bypasses completion handling. Install [NewInterceptor] only once.
func WrapClient(client *a2aclient.Client) *Client { return &Client{Client: client} }

// SendStreamingMessage observes completion without changing events or errors.
func (c *Client) SendStreamingMessage(ctx context.Context, req *a2a.SendMessageRequest) iter.Seq2[a2a.Event, error] {
	return observeStream(ctx, clientCompletionKey, "SendStreamingMessage", func(ctx context.Context) iter.Seq2[a2a.Event, error] {
		return c.Client.SendStreamingMessage(ctx, req)
	})
}

// SubscribeToTask observes completion, including a persisted nonterminal snapshot.
func (c *Client) SubscribeToTask(ctx context.Context, req *a2a.SubscribeToTaskRequest) iter.Seq2[a2a.Event, error] {
	return observeStream(ctx, clientCompletionKey, "SubscribeToTask", func(ctx context.Context) iter.Seq2[a2a.Event, error] {
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
	return observeStream(ctx, serverCompletionKey, "SendStreamingMessage", func(ctx context.Context) iter.Seq2[a2a.Event, error] {
		return h.RequestHandler.SendStreamingMessage(ctx, req)
	})
}

func (h *completionHandler) SubscribeToTask(ctx context.Context, req *a2a.SubscribeToTaskRequest) iter.Seq2[a2a.Event, error] {
	return observeStream(ctx, serverCompletionKey, "SubscribeToTask", func(ctx context.Context) iter.Seq2[a2a.Event, error] {
		return h.RequestHandler.SubscribeToTask(ctx, req)
	})
}

type completionKey int

const (
	clientCompletionKey completionKey = iota
	serverCompletionKey
)

// Each iteration owns a slot. Only its first matching Before can claim it, so
// nested calls inheriting this context cannot overwrite the outer call's state.
// Separate keys isolate inbound and outbound calls on the same context.
type streamCompletion struct {
	mu     sync.Mutex
	method string
	state  *callState
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

func observeStream(ctx context.Context, key completionKey, method string, stream func(context.Context) iter.Seq2[a2a.Event, error]) iter.Seq2[a2a.Event, error] {
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
		completion := &streamCompletion{method: method}
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
			if !yield(event, err) {
				return
			}
		}
		completed = true
	}
}
