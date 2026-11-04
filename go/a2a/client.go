package traceaia2a

import (
	"context"

	"github.com/a2aproject/a2a-go/v2/a2a"
	"github.com/a2aproject/a2a-go/v2/a2aclient"
)

// NewClientFromCard creates an SDK client with call-completion handling. Pass
// NewInterceptor through a2aclient.WithCallInterceptors as usual, in any position
// among your policies. All SDK factory options and transport selection apply.
//
// A private entry interceptor binds each completion to the SDK's request before
// any policy runs. Unlike wrapping an already-created SDK client, this can safely
// distinguish nested calls even when they share a context, payload or agent card.
func NewClientFromCard(ctx context.Context, card *a2a.AgentCard, opts ...a2aclient.FactoryOption) (*Client, error) {
	boundary := &clientBoundary{}
	client, err := a2aclient.NewFromCard(ctx, card, boundary.options(opts)...)
	if err != nil {
		return nil, err
	}
	return &Client{Client: client, boundary: boundary}, nil
}

// NewClientFromEndpoints is NewClientFromCard for clients created without an
// agent card. It preserves the SDK's endpoint selection and factory options.
func NewClientFromEndpoints(ctx context.Context, endpoints []*a2a.AgentInterface, opts ...a2aclient.FactoryOption) (*Client, error) {
	boundary := &clientBoundary{}
	client, err := a2aclient.NewFromEndpoints(ctx, endpoints, boundary.options(opts)...)
	if err != nil {
		return nil, err
	}
	return &Client{Client: client, boundary: boundary}, nil
}

// This non-zero-sized value is unique to one SDK client. Request pointers are
// unique to one SDK invocation, including recursive calls on that same client.
type clientBoundary struct {
	a2aclient.PassthroughInterceptor
	identity byte
}

func (b *clientBoundary) options(opts []a2aclient.FactoryOption) []a2aclient.FactoryOption {
	return append([]a2aclient.FactoryOption{a2aclient.WithCallInterceptors(b)}, opts...)
}

func (b *clientBoundary) Before(ctx context.Context, req *a2aclient.Request) (context.Context, any, error) {
	if ctx != nil && req != nil {
		if completion, _ := ctx.Value(clientCompletionKey).(*streamCompletion); completion != nil {
			completion.mu.Lock()
			if completion.owner == b && completion.request == nil && completion.method == req.Method {
				completion.request = req
			}
			completion.mu.Unlock()
		}
	}
	return ctx, nil, nil
}

func attachClientCompletion(ctx context.Context, req *a2aclient.Request, st *callState) bool {
	completion, _ := ctx.Value(clientCompletionKey).(*streamCompletion)
	if completion == nil {
		return false
	}
	completion.mu.Lock()
	defer completion.mu.Unlock()
	if completion.owner != nil {
		if completion.request != req {
			return false
		}
	} else if !methods[req.Method].streaming {
		// Legacy WrapClient cannot identify a unary call before its policies run.
		// Never defer its After callback based on an inherited method-only slot.
		return false
	}
	if completion.state == nil && completion.method == st.method {
		completion.state = st
		return true
	}
	return false
}
