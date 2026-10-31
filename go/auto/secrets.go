package auto

import (
	"container/list"
	"os"
	"sort"
	"strings"
	"sync"
	"sync/atomic"

	"github.com/future-agi/traceAI/go/auto/internal/wire"
)

const secretLimit = 1024

// The only process-wide credential store. LRU updates take a short lock;
// sanitizers load one immutable matcher per span, never a lock per string.
var processSecrets secretRegistry

type secretRegistry struct {
	mu     sync.Mutex
	values map[string]*list.Element
	lru    list.List
	cached atomic.Pointer[secretSnapshot]
}

type secretSnapshot struct {
	values  []string // Longest first; immutable after publication.
	matcher *strings.Replacer
}

// RegisterSecret marks an additional credential for export-time redaction in
// every agent-exported span. Values shorter than eight bytes are ignored. The
// process-wide registry retains at most 1024 values, evicting least recently
// registered/observed values. It never changes application requests or responses.
func RegisterSecret(value string) {
	_ = guarded(func() error { processSecrets.register(value); return nil })
}

func (r *secretRegistry) register(value string) {
	if len(value) < 8 {
		return
	}
	r.mu.Lock()
	defer r.mu.Unlock()
	if entry := r.values[value]; entry != nil {
		r.lru.MoveToFront(entry)
		return
	}
	if r.values == nil {
		r.values = make(map[string]*list.Element)
	}
	if len(r.values) == secretLimit {
		old := r.lru.Back()
		delete(r.values, old.Value.(string))
		r.lru.Remove(old)
	}
	r.values[value] = r.lru.PushFront(value)
	r.cached.Store(nil)
}

func (r *secretRegistry) snapshot() *secretSnapshot {
	if current := r.cached.Load(); current != nil {
		return current
	}
	r.mu.Lock()
	defer r.mu.Unlock()
	if current := r.cached.Load(); current != nil {
		return current
	}
	current := &secretSnapshot{values: make([]string, 0, len(r.values))}
	for value := range r.values {
		current.values = append(current.values, value)
	}
	sort.Slice(current.values, func(i, j int) bool {
		if len(current.values[i]) == len(current.values[j]) {
			return current.values[i] < current.values[j]
		}
		return len(current.values[i]) > len(current.values[j])
	})
	pairs := make([]string, 0, 2*len(current.values))
	for _, value := range current.values {
		pairs = append(pairs, value, wire.CredentialRedacted)
	}
	current.matcher = strings.NewReplacer(pairs...)
	r.cached.Store(current)
	return current
}

// noSecrets performs structural privacy only. Capture-time value replacement
// would expose a longer secret discovered later; S1 replaces values at export.
var noSecrets = &secretSnapshot{matcher: strings.NewReplacer()}

func (s *secretSnapshot) replace(text string) string {
	// The trie avoids scanning every registered value on unrelated strings.
	if s.matcher.Replace(text) == text {
		return text
	}
	// Preserve strictly longest-first replacement even for overlapping matches
	// that start at different offsets (not just prefixes at the same offset).
	for _, value := range s.values {
		text = strings.ReplaceAll(text, value, wire.CredentialRedacted)
	}
	return text
}

func seedSecrets(c Config) {
	for _, entry := range os.Environ() {
		name, value, _ := strings.Cut(entry, "=")
		if wire.CredentialAttributeKey(name) {
			RegisterSecret(value)
		}
	}
	RegisterSecret(c.APIKey)
	RegisterSecret(c.SecretKey)
}
