package traceaigenai

import (
	"encoding/json"
	"slices"
	"sort"
)

const (
	maxGroundingCandidates = 4
	maxGroundingSources    = 16
	maxGroundingQueries    = 8
	maxGroundingCitations  = 16
)

// These are deliberately partial provider types. Search HTML/blobs, retrieved
// document text, opaque metadata and unknown fields never become attributes.
// citationMetadata has already been normalized from citationSources by decode.go.
type groundingMetadata struct {
	Chunks []*struct {
		Web              *sourceReference `json:"web"`
		RetrievedContext *sourceReference `json:"retrievedContext"`
	} `json:"groundingChunks"`
	Queries []string `json:"webSearchQueries"`
}

type sourceReference struct {
	URI   string `json:"uri"`
	Title string `json:"title"`
}

type citationMetadata struct {
	Citations []*citationEvidence `json:"citations"`
}

type citationEvidence struct {
	URI        string `json:"uri,omitempty"`
	StartIndex *int   `json:"startIndex,omitempty"`
	EndIndex   *int   `json:"endIndex,omitempty"`
	License    string `json:"license,omitempty"`
}

type groundingSource struct {
	Type  string `json:"type"`
	URI   string `json:"uri,omitempty"`
	Title string `json:"title,omitempty"`
}

type groundingCandidate struct {
	Index     int                `json:"candidate_index"`
	Sources   []groundingSource  `json:"sources,omitempty"`
	Queries   []string           `json:"web_search_queries,omitempty"`
	Citations []citationEvidence `json:"citations,omitempty"`
	Truncated bool               `json:"truncated,omitempty"`
}

// Retained evidence is bounded across the entire stream, not per chunk. At most
// four candidates, 16 sources, eight queries and 16 citations per candidate,
// with each string capped at 512 bytes. The JSON uses one OTel attribute slot.
// Repeated cumulative metadata is deduplicated; a later empty chunk cannot
// erase earlier evidence. We do not export grounding-support indices because
// they reference chunk-local source arrays that may change between events.
type groundingCapture struct {
	Candidates []*groundingCandidate `json:"candidates"`
	Truncated  bool                  `json:"truncated,omitempty"`
}

func (g *groundingCapture) add(index int, metadata *groundingMetadata, citations *citationMetadata) {
	if metadata == nil && citations == nil {
		return
	}
	var c *groundingCandidate
	for _, candidate := range g.Candidates {
		if candidate.Index == index {
			c = candidate
			break
		}
	}
	fresh := c == nil
	if fresh {
		c = &groundingCandidate{Index: index}
	}
	if metadata != nil {
		for _, chunk := range metadata.Chunks {
			if chunk == nil {
				continue
			}
			c.addSource("web", chunk.Web)
			c.addSource("retrievedContext", chunk.RetrievedContext)
		}
		for _, query := range metadata.Queries {
			if query == "" {
				continue
			}
			query = boundedMetadataString(query, &c.Truncated)
			c.Queries = appendBoundedUnique(c.Queries, query, maxGroundingQueries, &c.Truncated)
		}
	}
	if citations != nil {
		for _, citation := range citations.Citations {
			if citation == nil || citation.URI == "" {
				continue
			}
			value := *citation
			value.URI = boundedMetadataString(value.URI, &c.Truncated)
			value.License = boundedMetadataString(value.License, &c.Truncated)
			if slices.ContainsFunc(c.Citations, func(existing citationEvidence) bool {
				return existing.URI == value.URI && existing.License == value.License &&
					sameIndex(existing.StartIndex, value.StartIndex) && sameIndex(existing.EndIndex, value.EndIndex)
			}) {
				continue
			}
			if len(c.Citations) == maxGroundingCitations {
				c.Truncated = true
				break
			}
			c.Citations = append(c.Citations, value)
		}
	}
	if fresh && (len(c.Sources) > 0 || len(c.Queries) > 0 || len(c.Citations) > 0) {
		if len(g.Candidates) == maxGroundingCandidates {
			g.Truncated = true
			return
		}
		g.Candidates = append(g.Candidates, c)
	}
}

func (c *groundingCandidate) addSource(kind string, ref *sourceReference) {
	if ref == nil || (ref.URI == "" && ref.Title == "") {
		return
	}
	value := groundingSource{Type: kind,
		URI:   boundedMetadataString(ref.URI, &c.Truncated),
		Title: boundedMetadataString(ref.Title, &c.Truncated)}
	c.Sources = appendBoundedUnique(c.Sources, value, maxGroundingSources, &c.Truncated)
}

func appendBoundedUnique[T comparable](values []T, value T, limit int, truncated *bool) []T {
	if slices.Contains(values, value) {
		return values
	}
	if len(values) == limit {
		*truncated = true
		return values
	}
	return append(values, value)
}

func sameIndex(a, b *int) bool {
	return (a == nil && b == nil) || (a != nil && b != nil && *a == *b)
}

func (g *groundingCapture) json() string {
	if len(g.Candidates) == 0 {
		return ""
	}
	sort.Slice(g.Candidates, func(i, j int) bool { return g.Candidates[i].Index < g.Candidates[j].Index })
	b, _ := json.Marshal(g)
	return string(b)
}
