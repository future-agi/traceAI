package traceaibedrock

import (
	"context"
	"sync"
)

type reader[T any] interface {
	Events() <-chan T
	Close() error
	Err() error
}

// A separate reader avoids modifying AWS's Reader field, which is concurrently
// accessed by its waitStreamClose goroutine. One unbuffered forwarding channel
// adds at most one prefetched event; no event history is retained.
type stream[T any] struct {
	inner    reader[T]
	events   chan T
	done     chan struct{}
	stopped  chan struct{}
	once     sync.Once
	closeErr error

	errMu       sync.Mutex
	terminalErr error
}

func observeStream[T any](ctx context.Context, inner reader[T], r *record, observe func(T)) *stream[T] {
	s := &stream[T]{inner: inner, events: make(chan T), done: make(chan struct{}), stopped: make(chan struct{})}
	go func() {
		early := false
		defer func() {
			// Closing the SDK reader can suppress its decoder's cancellation
			// error. Retain the context error at termination, including when
			// EOF or Close wins the select, but prefer genuine SDK errors.
			err := inner.Err()
			if err == nil {
				err = ctx.Err()
			}
			s.errMu.Lock()
			s.terminalErr = err
			s.errMu.Unlock()
			r.finish(err, early)
			close(s.events)
			close(s.stopped)
		}()
		input := inner.Events()
		for {
			select {
			case <-s.done:
				early = true
				return
			case <-ctx.Done():
				early = true
				s.closeInner()
				return
			case ev, ok := <-input:
				if !ok {
					return
				}
				r.mu.Lock()
				observe(ev)
				r.mu.Unlock()
				select {
				case s.events <- ev:
				case <-s.done:
					early = true
					return
				case <-ctx.Done():
					early = true
					s.closeInner()
					return
				}
			}
		}
	}()
	return s
}
func (s *stream[T]) Events() <-chan T { return s.events }
func (s *stream[T]) Err() error {
	if err := s.inner.Err(); err != nil {
		return err
	}
	s.errMu.Lock()
	defer s.errMu.Unlock()
	return s.terminalErr
}
func (s *stream[T]) closeInner()  { s.once.Do(func() { s.closeErr = s.inner.Close(); close(s.done) }) }
func (s *stream[T]) Close() error { s.closeInner(); <-s.stopped; return s.closeErr }
