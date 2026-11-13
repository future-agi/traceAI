package auto

import (
	"context"
	"sync"
)

// InitForTest isolates independent lifecycle scenarios in this test binary.
// This symbol exists only in _test.go, never in the shipped API. Calls within a
// live scenario still use production once-only initialization; dedicated S2
// tests call Init directly, including after shutdown.
func InitForTest(ctx context.Context, opts ...Option) (func(context.Context) error, error) {
	agent.Lock()
	if agent.current == nil {
		agent.first = nil
		agent.repeated = sync.Once{}
	}
	agent.Unlock()
	return Init(ctx, opts...)
}
