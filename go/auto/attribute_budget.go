package auto

import (
	"reflect"
	"sort"
	"strconv"
	"strings"

	"github.com/future-agi/traceAI/go/auto/internal/wire"
	"go.opentelemetry.io/otel/attribute"
	sdktrace "go.opentelemetry.io/otel/sdk/trace"
)

// The pinned Go SDK has no public limit getter. Read only its immutable numeric
// configuration once, before registering the agent. No unsafe, setters, probes,
// synthetic spans, sampler calls, or changes to host configuration. The ABI is
// regression-tested; an unsupported layout fails closed to a zero-slot budget.
func providerAttributeLimit(provider *sdktrace.TracerProvider) int {
	if provider != nil {
		limits := reflect.ValueOf(provider).Elem().FieldByName("spanLimits")
		if limits.IsValid() && limits.Kind() == reflect.Struct {
			count := limits.FieldByName("AttributeCountLimit")
			if count.IsValid() && count.Kind() == reflect.Int {
				return int(count.Int())
			}
		}
	}
	warn()
	return 0
}

// protectedAttributePriority is the spec's closed A7 protected set: exact
// identity, outcome, provider/model/operation, provenance and degradation keys,
// plus numeric gen_ai.usage.* counters. It is deliberately not prefix-based: an
// application attribute such as project_metadata.media.data, a string under
// gen_ai.usage.* or server.media.* is ordinary metadata, so it keeps the joint
// capture-time media scrub and the projection's MIME-companion pin (R6A B2).
func protectedAttributePriority(kv attribute.KeyValue) int {
	key := string(kv.Key)
	switch key {
	case "traceai.capture.dropped_content_count", "traceai.wire.attributes_truncated", "traceai.wire.truncated", "traceai.wire.incomplete", "traceai.wire.body_unavailable", "traceai.wire.undecoded_encoding", "traceai.wire.privacy_degraded":
		return 0
	case "user.id", "session.id", "gen_ai.span.kind", "gen_ai.response.id", "traceai.capture.layer", "project_name", "project_type", "project_version_name", "project_version_id":
		// Minimal origin is part of wire-span identity; richer endpoint/method
		// provenance can follow usage when even the protected set overflows.
		return 1
	case "error.type", "http.response.status_code", "gen_ai.response.finish_reasons":
		return 2
	case "gen_ai.provider.name", "gen_ai.operation.name", "gen_ai.request.model", "gen_ai.response.model":
		return 3
	case "traceai.wire.endpoint", "server.address", "server.port", "http.request.method":
		return 5
	}
	if strings.HasPrefix(key, "gen_ai.usage.") {
		// "Every known usage counter": a counter is a number. Unknown usage is
		// omitted upstream, never invented, so no other type is a counter.
		switch kv.Value.Type() {
		case attribute.INT64, attribute.FLOAT64:
			return 4
		}
	}
	return -1
}

type attributeGroup struct {
	name     string
	priority int
	index    int
	content  bool
	attrs    []attribute.KeyValue
	// companions are MIME/type hint keys (in other groups) governing a key in
	// this group. They are admitted with the group or the group is omitted, so
	// projection never separates media context from its payload.
	companions []attribute.KeyValue
}

func hintValue(kv attribute.KeyValue) (any, bool) {
	switch kv.Value.Type() {
	case attribute.STRING:
		return kv.Value.AsString(), true
	case attribute.STRINGSLICE:
		return kv.Value.AsStringSlice(), true
	}
	return nil, false
}

// mediaHintParent applies the catalog's one MIME/type hint predicate.
func mediaHintParent(kv attribute.KeyValue) (string, bool) {
	value, ok := hintValue(kv)
	if !ok {
		return "", false
	}
	return wire.MediaHintParent(string(kv.Key), value)
}

func contentGroup(key, operation string) attributeGroup {
	// Every named group below starts with a letter, so the NUL prefix keeps an
	// ungrouped key (e.g. an application "output" object) from merging into a
	// content group ("output") and inheriting its priority by map order.
	g := attributeGroup{name: "\x00" + key, priority: 8}
	for _, side := range []string{"output", "input"} {
		root := "gen_ai." + side + ".messages."
		if rest, ok := strings.CutPrefix(key, root); ok {
			part, _, _ := strings.Cut(rest, ".")
			if index, err := strconv.Atoi(part); err == nil && index >= 0 {
				g.name, g.index, g.content = root+part, index, true
				g.priority = 0
				if side == "input" {
					g.priority = 3
				}
				return g
			}
		}
	}
	switch {
	case strings.HasPrefix(key, "gen_ai.output."), strings.HasPrefix(key, "gen_ai.tool.call.result"):
		g.name, g.priority, g.content = "gen_ai.output", 0, true
	case strings.HasPrefix(key, "output."):
		g.name, g.priority, g.content = "output", 1, true
	case attributeFamily(key, "gen_ai.audio.transcript"):
		g.name, g.priority, g.content = "gen_ai.audio.transcript", 2, true
		if operation == "text_to_speech" {
			g.priority = 4
		}
	case strings.HasPrefix(key, "gen_ai.input."):
		g.name, g.priority, g.content = "gen_ai.input", 3, true
	case strings.HasPrefix(key, "gen_ai.tool.definitions."):
		rest := strings.TrimPrefix(key, "gen_ai.tool.definitions.")
		part, _, _ := strings.Cut(rest, ".")
		g.name, g.priority, g.content = "gen_ai.tool.definitions."+part, 5, true
		g.index, _ = strconv.Atoi(part)
	case attributeFamily(key, "gen_ai.system_instructions"), strings.HasPrefix(key, "gen_ai.tool."), attributeFamily(key, "gen_ai.request.parameters"):
		g.name, g.priority, g.content = "gen_ai.parameters", 5, true
	case strings.HasPrefix(key, "input."):
		g.name, g.priority, g.content = "input", 6, true
	}
	return g
}

// Admission occurs before the SDK container. Protected fields are reserved;
// content is grouped atomically, with output first and input newest-suffix first.
// SDK DroppedAttributes remains the SDK's independent, unmodified counter.
func budgetAttributes(kvs []attribute.KeyValue, limit int) []attribute.KeyValue {
	values := make(map[string]attribute.KeyValue, len(kvs))
	for _, kv := range kvs {
		values[string(kv.Key)] = kv // Last context override wins.
	}
	oldDropped := values["traceai.capture.dropped_content_count"].Value.AsInt64()
	if oldDropped < 0 {
		oldDropped = 0
	}
	truncated := values["traceai.wire.attributes_truncated"].Value.AsBool()
	delete(values, "traceai.capture.dropped_content_count")
	delete(values, "traceai.wire.attributes_truncated")
	operation := values["gen_ai.operation.name"].Value.AsString()
	var protected []attribute.KeyValue
	groups := map[string]*attributeGroup{}
	totalContent := 0
	for key, kv := range values {
		if protectedAttributePriority(kv) >= 0 {
			protected = append(protected, kv)
			continue
		}
		group := contentGroup(key, operation)
		g := groups[group.name]
		if g == nil {
			g = &group
			groups[group.name] = g
		}
		g.attrs = append(g.attrs, kv)
		if g.content {
			totalContent++
		}
	}
	type mediaHint struct {
		kv     attribute.KeyValue
		parent string
		group  *attributeGroup
	}
	var hints []mediaHint
	content := map[string]bool{}
	for _, g := range groups {
		for _, kv := range g.attrs {
			content[string(kv.Key)] = g.content
			if parent, ok := mediaHintParent(kv); ok {
				hints = append(hints, mediaHint{kv, parent, g})
			}
		}
	}
	sort.Slice(hints, func(i, j int) bool { return hints[i].kv.Key < hints[j].kv.Key })
	ordered := make([]*attributeGroup, 0, len(groups))
	for _, group := range groups {
		sort.Slice(group.attrs, func(i, j int) bool { return group.attrs[i].Key < group.attrs[j].Key })
		for _, h := range hints {
			if h.group == group {
				continue
			}
			for _, kv := range group.attrs {
				if h.parent == "" || strings.HasPrefix(string(kv.Key), h.parent+".") {
					group.companions = append(group.companions, h.kv)
					break
				}
			}
		}
		ordered = append(ordered, group)
	}
	sort.Slice(ordered, func(i, j int) bool {
		a, b := ordered[i], ordered[j]
		if a.priority != b.priority {
			return a.priority < b.priority
		}
		if a.index != b.index {
			if a.priority == 3 {
				return a.index > b.index
			}
			return a.index < b.index
		}
		return a.name < b.name
	})
	countMarker := oldDropped > 0
	for {
		out := append([]attribute.KeyValue(nil), protected...)
		if countMarker {
			out = append(out, attribute.Int64("traceai.capture.dropped_content_count", oldDropped))
		}
		if truncated {
			out = append(out, attribute.Bool("traceai.wire.attributes_truncated", true))
		}
		sort.Slice(out, func(i, j int) bool {
			if x, y := protectedAttributePriority(out[i]), protectedAttributePriority(out[j]); x != y {
				return x < y
			}
			return out[i].Key < out[j].Key
		})
		retainedContent, retained := 0, 0
		if limit >= 0 && len(out) > limit {
			out = out[:limit]
		} else {
			outputBlocked, inputBlocked := false, false
			admitted := map[string]bool{}
			for _, g := range ordered {
				blocked := g.content && (g.priority >= 3 && outputBlocked || g.priority >= 3 && g.priority <= 6 && inputBlocked)
				// Group attrs and companions are disjoint; a companion may
				// already be admitted with an earlier group.
				var need []attribute.KeyValue
				for _, kv := range append(append([]attribute.KeyValue(nil), g.attrs...), g.companions...) {
					if !admitted[string(kv.Key)] {
						need = append(need, kv)
					}
				}
				fits := limit < 0 || len(out)+len(need) <= limit
				if !blocked && fits {
					for _, kv := range need {
						admitted[string(kv.Key)] = true
						out = append(out, kv)
						retained++
						if content[string(kv.Key)] {
							retainedContent++
						}
					}
				} else if g.content {
					if g.priority < 3 {
						outputBlocked = true
					}
					if g.priority == 3 {
						inputBlocked = true
					}
				}
			}
		}
		dropped := totalContent - retainedContent
		// Adding the reserved markers can itself evict a whole content group.
		// Monotonic flags converge after at most two additional projections.
		if !truncated && (retained+len(protected) < len(values) || limit >= 0 && len(protected) > limit) {
			truncated = true
			continue
		}
		if dropped > 0 && !countMarker {
			countMarker = true
			continue
		}
		for i := range out {
			if out[i].Key == "traceai.capture.dropped_content_count" {
				out[i] = attribute.Int64("traceai.capture.dropped_content_count", oldDropped+int64(dropped))
			}
		}
		if limit == 0 && len(values) > 0 {
			warn() // A zero-slot provider cannot carry even a degradation marker.
		}
		return out
	}
}
