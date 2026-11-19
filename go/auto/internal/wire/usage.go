package wire

import "math"

// Usage events are cumulative snapshots, not deltas. Merge only supplied fields,
// including explicit null/zero and nested details, then normalize once at Build.
func mergeUsage(target, source object) {
	for key, value := range source {
		if fields, ok := value.(map[string]any); ok {
			child, ok := target[key].(map[string]any)
			if !ok {
				child = object{}
				target[key] = child
			}
			mergeUsage(child, fields)
		} else {
			target[key] = value
		}
	}
}
func (p *parsed) observeUsage(family string, value any) {
	fields, ok := value.(map[string]any)
	if !ok {
		return
	}
	if p.rawUsage == nil {
		p.rawUsage = object{}
	}
	p.usageFamily = family
	mergeUsage(p.rawUsage, fields)
}
func usageNumber(fields object, key string, optional bool) (int64, bool) {
	value, exists := fields[key]
	if !exists {
		return 0, optional
	}
	if f, ok := value.(float64); ok && (math.Trunc(f) != f || f >= math.MaxInt64) {
		return 0, false
	}
	count, known := integer(value)
	return count, known && count >= 0
}
func inclusiveUsage(fields object, base string, additions ...string) (int64, bool) {
	count, known := usageNumber(fields, base, false)
	if !known {
		return 0, false
	}
	for _, key := range additions {
		extra, known := usageNumber(fields, key, true)
		if !known || count > math.MaxInt64-extra {
			return 0, false
		}
		count += extra
	}
	return count, true
}
func (p *parsed) normalizedUsage() map[string]int64 {
	u := p.Usage
	if p.rawUsage != nil {
		u = map[string]int64{}
		raw := p.rawUsage
		putCount := func(key string, count int64, known bool) {
			if known {
				u[key] = count
			}
		}
		copyCount := func(key string, fields object, name string) {
			n, ok := usageNumber(fields, name, false)
			putCount(key, n, ok)
		}
		inclusive := func(key, base string, additions ...string) {
			n, ok := inclusiveUsage(raw, base, additions...)
			putCount(key, n, ok)
		}
		switch p.usageFamily {
		case "anthropic":
			inclusive("input", "input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")
			copyCount("output", raw, "output_tokens")
			copyCount("total", raw, "total_tokens")
			copyCount("cache_read", raw, "cache_read_input_tokens")
			copyCount("cache_write", raw, "cache_creation_input_tokens")
		case "bedrock.converse":
			inclusive("input", "inputTokens", "cacheReadInputTokens", "cacheWriteInputTokens")
			copyCount("output", raw, "outputTokens")
			copyCount("total", raw, "totalTokens")
			copyCount("cache_read", raw, "cacheReadInputTokens")
			copyCount("cache_write", raw, "cacheWriteInputTokens")
		case "gemini":
			inclusive("input", "promptTokenCount", "toolUsePromptTokenCount")
			inclusive("output", "candidatesTokenCount", "thoughtsTokenCount")
			copyCount("total", raw, "totalTokenCount")
			copyCount("cache_read", raw, "cachedContentTokenCount")
			copyCount("reasoning", raw, "thoughtsTokenCount")
		default:
			ikey, okey := "input_tokens", "output_tokens"
			_, prompt := raw["prompt_tokens"]
			_, completion := raw["completion_tokens"]
			if prompt || completion {
				ikey, okey = "prompt_tokens", "completion_tokens"
			}
			copyCount("input", raw, ikey)
			copyCount("output", raw, okey)
			copyCount("total", raw, "total_tokens")
			inputDetails, outputDetails := obj(raw[ikey+"_details"]), obj(raw[okey+"_details"])
			copyCount("cache_read", inputDetails, "cached_tokens")
			copyCount("reasoning", outputDetails, "reasoning_tokens")
			copyCount("input_audio", inputDetails, "audio_tokens")
			copyCount("output_audio", outputDetails, "audio_tokens")
		}
	}
	if in, known := u["input"]; known {
		if out, known := u["output"]; known && in <= math.MaxInt64-out {
			if raw, known := u["total"]; known && raw != in+out {
				u["provider_total"] = raw
			}
			u["total"] = in + out
		}
	}
	return u
}
