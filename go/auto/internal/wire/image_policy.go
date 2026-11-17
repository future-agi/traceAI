package wire

import (
	"encoding/json"
	"fmt"
	"strconv"
	"strings"
)

// ImageAttribute is the optional hide-images policy, not an additional raw-media
// recognizer. Names/MIME hints and flattened/indexed paths share the S6 helpers.
func ImageAttribute(path string) bool {
	name := privacyRules.normalize(path)
	return strings.Contains(name, "image") || strings.Contains(name, "fileuri") || strings.Contains(name, "fileurl")
}

func imageReferenceHint(name string, value any) bool {
	text, ok := value.(string)
	if !ok {
		return false
	}
	kind := privacyRules.normalize(text)
	return ImageHint(name, text) || privacyLeaf(name) == "type" && (kind == "image" || kind == "inputimage" || kind == "imageurl")
}

func hiddenImagePath(path string, hints map[string]bool) bool {
	if ImageAttribute(path) || hints[""] {
		return true
	}
	parts := pathParts(path)
	if len(parts) > privacyRules.Limits.Depth {
		return true // Bound flattened paths too, before building any prefixes.
	}
	var prefix []string
	for _, part := range parts {
		prefix = append(prefix, part)
		if hints[strings.Join(prefix, ".")] {
			return true
		}
	}
	return false
}

// HideImageReferences implements the stronger opt-in image policy on a detached
// object. Index hints before walking any sibling, including flattened fields;
// exact numeric parent paths prevent one image block from hiding its neighbors.
// Raw-media recognition remains exclusively in mediaString/the S6 catalog walk.
func HideImageReferences(value any, replacement string) (out any) {
	out = replacement
	defer func() {
		if recover() != nil {
			out = replacement
		}
	}()
	// Hints belong to canonical paths, not to a particular object representation.
	// Index the whole detached tree first: a MIME field inside a native or JSON
	// object must also protect its equivalent flattened sibling URL.
	hints := map[string]bool{}
	visitPrivacyHints(value, "", newPrivacyBudget(), 0, func(path string, item any) {
		if imageReferenceHint(path, item) {
			hints[hintParent(path)] = true
		}
	})
	return hideImageReferences(value, "", replacement, newPrivacyBudget(), 0, hints)
}

func hideImageReferences(value any, path, replacement string, budget *privacyBudget, depth int, hints map[string]bool) any {
	if !budget.take(0, depth) || hiddenImagePath(path, hints) {
		return replacement
	}
	switch x := value.(type) {
	case map[string]any:
		if len(x) > budget.nodes {
			return replacement
		}
		out := make(map[string]any, len(x))
		for name, item := range x {
			if !budget.take(len(name), depth) {
				return replacement
			}
			out[name] = hideImageReferences(item, joinPath(path, name), replacement, budget, depth+1, hints)
		}
		return out
	case []any:
		if len(x) > budget.nodes {
			return replacement
		}
		out := make([]any, len(x))
		for i, item := range x {
			out[i] = hideImageReferences(item, joinPath(path, strconv.Itoa(i)), replacement, budget, depth+1, hints)
		}
		return out
	case string:
		if !budget.take(len(x), depth) {
			return replacement
		}
		trim := strings.TrimSpace(x)
		if strings.HasPrefix(trim, "{") || strings.HasPrefix(trim, "[") || strings.HasPrefix(trim, `"`) {
			var decoded any
			if json.Unmarshal([]byte(x), &decoded) == nil {
				clean := hideImageReferences(decoded, path, replacement, budget, depth+1, hints)
				if dump(decoded) != dump(clean) {
					return dump(clean)
				}
			}
		}
		return x
	case nil, bool, int, int64, float64, json.Number:
		return x
	default:
		return fmt.Sprintf("%T", value)
	}
}
