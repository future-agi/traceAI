package wire

import (
	"bytes"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"math"
	"sort"
	"strconv"
	"strings"
	"unicode/utf8"
)

// jsonOrder is the member order of a parsed JSON value: an object's keys as written (each once, at its first place) and the
// order of each member's and item's own value.
type jsonOrder struct {
	keys     []string
	members  map[string]*jsonOrder
	items    []*jsonOrder
	isObject bool
}

// parseStructured parses a structured string (JSON text) with its numbers as written and its member order, or reports false. It
// accepts what the shared reference's json.loads accepts: besides standard JSON, the bare literals NaN, Infinity and -Infinity,
// and numbers outside the float64 range. The whole text has to be one value, with only whitespace after it.
func parseStructured(text string) (any, *jsonOrder, bool) {
	text, literals := substituteNonFinite(text)
	decoder := json.NewDecoder(strings.NewReader(text))
	decoder.UseNumber()
	var parsed any
	if decoder.Decode(&parsed) != nil {
		return nil, nil, false
	}
	if _, err := decoder.Token(); err != io.EOF {
		return nil, nil, false // anything but whitespace after the value: not one JSON value
	}
	order := &jsonOrder{}
	tokens := json.NewDecoder(strings.NewReader(text))
	tokens.UseNumber()
	if readOrder(tokens, order) != nil {
		return nil, nil, false
	}
	if literals != nil {
		parsed = restoreNonFinite(parsed, literals)
	}
	return parsed, order, true
}

// nonFiniteLiterals maps the stand-in number written for each bare NaN, Infinity and -Infinity literal back to its spelling.
type nonFiniteLiterals map[string]string

// substituteNonFinite replaces each bare NaN, Infinity and -Infinity outside a string with a number literal that does not occur in
// the text, which the standard decoder accepts. It returns the text unchanged, and no literals, when there is none.
func substituteNonFinite(text string) (string, nonFiniteLiterals) {
	var out strings.Builder
	var literals nonFiniteLiterals
	stand := ""
	last := 0
	for i := 0; i < len(text); i++ {
		switch c := text[i]; {
		case c == '"':
			for i++; i < len(text) && text[i] != '"'; i++ {
				if text[i] == '\\' {
					i++
				}
			}
		case c == 'N' || c == 'I' || c == '-':
			spelling := ""
			for _, candidate := range [...]string{"NaN", "Infinity", "-Infinity"} {
				if strings.HasPrefix(text[i:], candidate) {
					spelling = candidate
				}
			}
			if spelling == "" {
				continue
			}
			if literals == nil {
				literals = nonFiniteLiterals{}
				stand = "0.123456789012345678901234567890"
				for strings.Contains(text, stand) {
					stand += "7"
				}
			}
			literal := stand + map[string]string{"NaN": "e1", "Infinity": "e2", "-Infinity": "e3"}[spelling]
			if spelling == "-Infinity" {
				literal = "-" + literal
			}
			literals[literal] = spelling
			out.WriteString(text[last:i])
			out.WriteString(literal)
			i += len(spelling) - 1
			last = i + 1
		}
	}
	if literals == nil {
		return text, nil
	}
	out.WriteString(text[last:])
	return out.String(), literals
}

func restoreNonFinite(value any, literals nonFiniteLiterals) any {
	switch x := value.(type) {
	case json.Number:
		if spelling, ok := literals[string(x)]; ok {
			return json.Number(spelling)
		}
	case map[string]any:
		for key, item := range x {
			x[key] = restoreNonFinite(item, literals)
		}
	case []any:
		for i, item := range x {
			x[i] = restoreNonFinite(item, literals)
		}
	}
	return value
}

func readOrder(d *json.Decoder, order *jsonOrder) error {
	token, err := d.Token()
	if err != nil {
		return err
	}
	delim, ok := token.(json.Delim)
	if !ok {
		return nil
	}
	switch delim {
	case '{':
		order.isObject, order.members = true, map[string]*jsonOrder{}
		for d.More() {
			key, err := d.Token()
			if err != nil {
				return err
			}
			name, _ := key.(string)
			if _, seen := order.members[name]; !seen {
				order.keys = append(order.keys, name)
			}
			child := &jsonOrder{}
			if err := readOrder(d, child); err != nil {
				return err
			}
			order.members[name] = child
		}
	case '[':
		for d.More() {
			child := &jsonOrder{}
			if err := readOrder(d, child); err != nil {
				return err
			}
			order.items = append(order.items, child)
		}
	}
	_, err = d.Token() // the closing delimiter
	return err
}

// exportJSON writes a scrubbed structured value as the shared reference re-dumps it (compact separators, non-ASCII characters
// as they are, the same escapes and number spellings), each object's members in their original order. mapKey gives the
// scrubbed name of an original key.
func exportJSON(value any, order *jsonOrder, mapKey func(string) string) string {
	var b bytes.Buffer
	writeExportJSON(&b, value, order, mapKey)
	return b.String()
}

func writeExportJSON(b *bytes.Buffer, value any, order *jsonOrder, mapKey func(string) string) {
	switch x := value.(type) {
	case map[string]any:
		b.WriteByte('{')
		written := map[string]bool{}
		first := true
		member := func(name string, child *jsonOrder) {
			item, ok := x[name]
			if !ok || written[name] {
				return
			}
			written[name] = true
			if !first {
				b.WriteByte(',')
			}
			first = false
			writeExportString(b, name)
			b.WriteByte(':')
			writeExportJSON(b, item, child, mapKey)
		}
		if order != nil && order.isObject {
			for _, key := range order.keys {
				member(mapKey(key), order.members[key])
			}
		}
		rest := make([]string, 0, len(x))
		for name := range x {
			if !written[name] {
				rest = append(rest, name)
			}
		}
		sort.Strings(rest)
		for _, name := range rest {
			member(name, nil)
		}
		b.WriteByte('}')
	case []any:
		b.WriteByte('[')
		for i, item := range x {
			if i > 0 {
				b.WriteByte(',')
			}
			var child *jsonOrder
			if order != nil && i < len(order.items) {
				child = order.items[i]
			}
			writeExportJSON(b, item, child, mapKey)
		}
		b.WriteByte(']')
	case string:
		writeExportString(b, x)
	case json.Number:
		b.WriteString(exportNumber(string(x)))
	case float64:
		b.WriteString(pythonFloat(x))
	case int:
		b.WriteString(strconv.Itoa(x))
	case int64:
		b.WriteString(strconv.FormatInt(x, 10))
	case bool:
		b.WriteString(strconv.FormatBool(x))
	case nil:
		b.WriteString("null")
	default:
		b.WriteString(dump(x))
	}
}

// writeExportString escapes as the reference's JSON writer does: '"', '\\', \n \r \t \b \f and the other controls below U+0020
// as \u00xx; every other character is written as it is.
func writeExportString(b *bytes.Buffer, s string) {
	b.WriteByte('"')
	for _, c := range strings.ToValidUTF8(s, string(utf8.RuneError)) {
		switch c {
		case '"':
			b.WriteString(`\"`)
		case '\\':
			b.WriteString(`\\`)
		case '\n':
			b.WriteString(`\n`)
		case '\r':
			b.WriteString(`\r`)
		case '\t':
			b.WriteString(`\t`)
		case '\b':
			b.WriteString(`\b`)
		case '\f':
			b.WriteString(`\f`)
		default:
			if c < 0x20 {
				b.WriteString(`\u00`)
				b.WriteByte("0123456789abcdef"[c>>4])
				b.WriteByte("0123456789abcdef"[c&15])
			} else {
				b.WriteRune(c)
			}
		}
	}
	b.WriteByte('"')
}

// exportNumber spells a JSON number as the reference re-dumps it: an integer literal as an integer, any other as the shortest
// float spelling.
func exportNumber(literal string) string {
	switch literal {
	case "NaN", "Infinity", "-Infinity":
		return literal
	}
	if !strings.ContainsAny(literal, ".eE") {
		if n, ok := integerLiteral(literal); ok {
			return n
		}
	}
	f, err := strconv.ParseFloat(literal, 64)
	if err != nil {
		var numError *strconv.NumError
		if errors.As(err, &numError) && numError.Err == strconv.ErrRange {
			return pythonFloat(f) // outside the float64 range: Infinity or -Infinity, as the reference's float() reads it
		}
		return literal
	}
	return pythonFloat(f)
}

// integerLiteral canonicalizes an integer literal (a sign and digits; "-0" is "0").
func integerLiteral(literal string) (string, bool) {
	digits := strings.TrimPrefix(literal, "-")
	if digits == "" {
		return "", false
	}
	for i := 0; i < len(digits); i++ {
		if digits[i] < '0' || digits[i] > '9' {
			return "", false
		}
	}
	trimmed := strings.TrimLeft(digits, "0")
	if trimmed == "" {
		return "0", true
	}
	if digits != literal {
		return "-" + trimmed, true
	}
	return trimmed, true
}

// pythonFloat is the shortest round-trip spelling of a float in the reference's style: fixed notation with at least one decimal
// for exponents from -4 to 15, else d.ddde+XX.
func pythonFloat(f float64) string {
	switch {
	case math.IsInf(f, 1):
		return "Infinity"
	case math.IsInf(f, -1):
		return "-Infinity"
	case math.IsNaN(f):
		return "NaN"
	}
	e := strconv.FormatFloat(f, 'e', -1, 64)
	mantissa, exponentText, _ := strings.Cut(e, "e")
	exponent, _ := strconv.Atoi(exponentText)
	if exponent >= -4 && exponent < 16 {
		s := strconv.FormatFloat(f, 'f', -1, 64)
		if !strings.Contains(s, ".") {
			s += ".0"
		}
		return s
	}
	sign := "+"
	if exponent < 0 {
		sign, exponent = "-", -exponent
	}
	return fmt.Sprintf("%se%s%02d", mantissa, sign, exponent)
}
