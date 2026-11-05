package traceaia2a

import (
	"crypto/sha256"
	"strings"

	"github.com/a2aproject/a2a-go/v2/a2a"
)

// Bound both the number of IDs and each artifact's retained text. Keeping a
// separate prefix per artifact lets replacements of earlier, large artifacts
// expose later ones correctly. At most 128 * maxContentLen text bytes are kept.
const maxTrackedArtifacts = 128

type artifactText struct {
	text      string
	truncated bool
}

type artifactState struct {
	// Fixed-size digests avoid retaining arbitrarily large SDK artifact IDs.
	index   map[[sha256.Size]byte]int
	entries []artifactText
	omitted bool
}

func (s *artifactState) Reset() { *s = artifactState{} }

// update follows a2a-go's artifact semantics without retaining or mutating any
// SDK objects: replace by ID unless Append is true, which appends content parts.
func (s *artifactState) update(a *a2a.Artifact, appendParts bool) {
	key := sha256.Sum256([]byte(a.ID))
	i, exists := s.index[key]
	if !exists {
		if len(s.entries) == maxTrackedArtifacts {
			s.omitted = true
			return
		}
		if s.index == nil {
			s.index = make(map[[sha256.Size]byte]int)
		}
		i = len(s.entries)
		s.index[key] = i
		s.entries = append(s.entries, artifactText{})
	}
	entry := &s.entries[i]
	if appendParts && entry.truncated {
		return // Later parts cannot change an already-truncated prefix.
	}
	text := partsText(a.Parts)
	if appendParts && entry.text != "" {
		if text == "" {
			return
		}
		text = entry.text + "\n" + text
	}
	entry.truncated = len(text) > maxContentLen
	// Clone even short values so a substring cannot retain an SDK-owned buffer.
	entry.text = strings.Clone(truncate(text))
}

func (s *artifactState) String() string {
	var out strings.Builder
	for _, entry := range s.entries {
		if entry.text == "" {
			continue
		}
		if out.Len() > 0 {
			out.WriteByte('\n')
		}
		out.WriteString(entry.text)
		if out.Len() > maxContentLen {
			break // output() applies the common final truncation and privacy rules.
		}
	}
	if s.omitted {
		out.WriteString(truncatedSuffix)
	}
	return out.String()
}
