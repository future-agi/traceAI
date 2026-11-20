package auto

import (
	"context"
	"sync"
	"sync/atomic"
	"testing"
)

// everySpanForTest keeps the FI exporter forwarding every span, as it did
// before it forwarded only AI spans from an application-owned provider. Tests
// of the export sanitizer use ordinary application spans as convenient input:
// the sanitizer is the same for the AI spans that reach Future AGI, and their
// attribute layouts are tuned to exact SDK limits, so a span-kind attribute
// would change what they test. The AI-span rule has its own tests, which never
// set this.
var everySpanForTest atomic.Bool

// exportEverySpanForTest sets everySpanForTest for the rest of one test.
func exportEverySpanForTest(t *testing.T) {
	t.Helper()
	everySpanForTest.Store(true)
	t.Cleanup(func() { everySpanForTest.Store(false) })
}

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
	resetModulesForTest()
	shutdown, err := Init(ctx, opts...)
	if everySpanForTest.Load() {
		agent.Lock()
		if agent.current != nil {
			agent.current.registration.aiOnly.Store(false)
		}
		agent.Unlock()
	}
	return shutdown, err
}

// resetModulesForTest forgets what dedicated modules registered, so scenarios
// do not depend on test order, -count or -shuffle.
func resetModulesForTest() {
	attached.Range(func(k, _ any) bool { attached.Delete(k); return true })
	shutdownHooks.Lock()
	shutdownHooks.fns = nil
	shutdownHooks.Unlock()
	agent.Lock()
	moduleProbe.tried, moduleProbe.seen = false, nil
	agent.Unlock()
	moduleState.Store(nil)
}

// ResetModulesForTest is resetModulesForTest for the external test package.
func ResetModulesForTest() { resetModulesForTest() }
