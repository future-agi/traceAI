package wire

import (
	"strings"
	"testing"
)

// A structured string is what the reference's json.loads accepts, NaN, Infinity, -Infinity and numbers outside the float64 range
// included. A credential-named member's value is replaced whether its name is written plainly or with a JSON escape.
func TestStructuredJSONAcceptsNonFiniteNumbers(t *testing.T) {
	type row struct {
		value, expected string
		framed          bool
	}
	rows := []row{
		{value: "\"{\\\"pwd\\\":\\\"S3KRET\\\",\\\"x\\\":NaN}\"", expected: "\"{\\\"pwd\\\":\\\"<redacted>\\\",\\\"x\\\":NaN}\"", framed: true},
		{value: "\"{\\\"pwd\\\":\\\"S3KRET\\\",\\\"x\\\":NaN}\"", expected: "\"{\\\"pwd\\\":\\\"<redacted>\\\",\\\"x\\\":NaN}\"", framed: false},
		{value: "\"{\\\"pwd\\\":\\\"S3KRET\\\",\\\"x\\\":Infinity}\"", expected: "\"{\\\"pwd\\\":\\\"<redacted>\\\",\\\"x\\\":Infinity}\"", framed: true},
		{value: "\"{\\\"pwd\\\":\\\"S3KRET\\\",\\\"x\\\":Infinity}\"", expected: "\"{\\\"pwd\\\":\\\"<redacted>\\\",\\\"x\\\":Infinity}\"", framed: false},
		{value: "\"{\\\"pwd\\\":\\\"S3KRET\\\",\\\"x\\\":-Infinity}\"", expected: "\"{\\\"pwd\\\":\\\"<redacted>\\\",\\\"x\\\":-Infinity}\"", framed: true},
		{value: "\"{\\\"pwd\\\":\\\"S3KRET\\\",\\\"x\\\":-Infinity}\"", expected: "\"{\\\"pwd\\\":\\\"<redacted>\\\",\\\"x\\\":-Infinity}\"", framed: false},
		{value: "\"{\\\"pwd\\\":\\\"S3KRET\\\",\\\"x\\\":1E+400}\"", expected: "\"{\\\"pwd\\\":\\\"<redacted>\\\",\\\"x\\\":Infinity}\"", framed: true},
		{value: "\"{\\\"pwd\\\":\\\"S3KRET\\\",\\\"x\\\":1E+400}\"", expected: "\"{\\\"pwd\\\":\\\"<redacted>\\\",\\\"x\\\":Infinity}\"", framed: false},
		{value: "\"{\\\"pwd\\\":\\\"S3KRET\\\",\\\"x\\\":-1E+400}\"", expected: "\"{\\\"pwd\\\":\\\"<redacted>\\\",\\\"x\\\":-Infinity}\"", framed: true},
		{value: "\"{\\\"pwd\\\":\\\"S3KRET\\\",\\\"x\\\":-1E+400}\"", expected: "\"{\\\"pwd\\\":\\\"<redacted>\\\",\\\"x\\\":-Infinity}\"", framed: false},
		{value: "\"{\\\"pwd\\\":\\\"S3KRET\\\",\\\"x\\\":[NaN]}\"", expected: "\"{\\\"pwd\\\":\\\"<redacted>\\\",\\\"x\\\":[NaN]}\"", framed: true},
		{value: "\"{\\\"pwd\\\":\\\"S3KRET\\\",\\\"x\\\":[NaN]}\"", expected: "\"{\\\"pwd\\\":\\\"<redacted>\\\",\\\"x\\\":[NaN]}\"", framed: false},
		{value: "\"{\\\"pwd\\\":\\\"S3KRET\\\",\\\"x\\\":{\\\"y\\\":Infinity}}\"", expected: "\"{\\\"pwd\\\":\\\"<redacted>\\\",\\\"x\\\":{\\\"y\\\":Infinity}}\"", framed: true},
		{value: "\"{\\\"pwd\\\":\\\"S3KRET\\\",\\\"x\\\":{\\\"y\\\":Infinity}}\"", expected: "\"{\\\"pwd\\\":\\\"<redacted>\\\",\\\"x\\\":{\\\"y\\\":Infinity}}\"", framed: false},
		{value: "\"{\\\"\\\\u0070wd\\\":\\\"S3KRET\\\",\\\"x\\\":NaN}\"", expected: "\"{\\\"pwd\\\":\\\"<redacted>\\\",\\\"x\\\":NaN}\"", framed: true},
		{value: "\"{\\\"\\\\u0070wd\\\":\\\"S3KRET\\\",\\\"x\\\":NaN}\"", expected: "\"{\\\"pwd\\\":\\\"<redacted>\\\",\\\"x\\\":NaN}\"", framed: false},
		{value: "\"{\\\"\\\\u0070wd\\\":\\\"S3KRET\\\",\\\"x\\\":Infinity}\"", expected: "\"{\\\"pwd\\\":\\\"<redacted>\\\",\\\"x\\\":Infinity}\"", framed: true},
		{value: "\"{\\\"\\\\u0070wd\\\":\\\"S3KRET\\\",\\\"x\\\":Infinity}\"", expected: "\"{\\\"pwd\\\":\\\"<redacted>\\\",\\\"x\\\":Infinity}\"", framed: false},
		{value: "\"{\\\"\\\\u0070wd\\\":\\\"S3KRET\\\",\\\"x\\\":-Infinity}\"", expected: "\"{\\\"pwd\\\":\\\"<redacted>\\\",\\\"x\\\":-Infinity}\"", framed: true},
		{value: "\"{\\\"\\\\u0070wd\\\":\\\"S3KRET\\\",\\\"x\\\":-Infinity}\"", expected: "\"{\\\"pwd\\\":\\\"<redacted>\\\",\\\"x\\\":-Infinity}\"", framed: false},
		{value: "\"{\\\"\\\\u0070wd\\\":\\\"S3KRET\\\",\\\"x\\\":1E+400}\"", expected: "\"{\\\"pwd\\\":\\\"<redacted>\\\",\\\"x\\\":Infinity}\"", framed: true},
		{value: "\"{\\\"\\\\u0070wd\\\":\\\"S3KRET\\\",\\\"x\\\":1E+400}\"", expected: "\"{\\\"pwd\\\":\\\"<redacted>\\\",\\\"x\\\":Infinity}\"", framed: false},
		{value: "\"{\\\"\\\\u0070wd\\\":\\\"S3KRET\\\",\\\"x\\\":-1E+400}\"", expected: "\"{\\\"pwd\\\":\\\"<redacted>\\\",\\\"x\\\":-Infinity}\"", framed: true},
		{value: "\"{\\\"\\\\u0070wd\\\":\\\"S3KRET\\\",\\\"x\\\":-1E+400}\"", expected: "\"{\\\"pwd\\\":\\\"<redacted>\\\",\\\"x\\\":-Infinity}\"", framed: false},
		{value: "\"{\\\"\\\\u0070wd\\\":\\\"S3KRET\\\",\\\"x\\\":[NaN]}\"", expected: "\"{\\\"pwd\\\":\\\"<redacted>\\\",\\\"x\\\":[NaN]}\"", framed: true},
		{value: "\"{\\\"\\\\u0070wd\\\":\\\"S3KRET\\\",\\\"x\\\":[NaN]}\"", expected: "\"{\\\"pwd\\\":\\\"<redacted>\\\",\\\"x\\\":[NaN]}\"", framed: false},
		{value: "\"{\\\"\\\\u0070wd\\\":\\\"S3KRET\\\",\\\"x\\\":{\\\"y\\\":Infinity}}\"", expected: "\"{\\\"pwd\\\":\\\"<redacted>\\\",\\\"x\\\":{\\\"y\\\":Infinity}}\"", framed: true},
		{value: "\"{\\\"\\\\u0070wd\\\":\\\"S3KRET\\\",\\\"x\\\":{\\\"y\\\":Infinity}}\"", expected: "\"{\\\"pwd\\\":\\\"<redacted>\\\",\\\"x\\\":{\\\"y\\\":Infinity}}\"", framed: false},
		{value: "\"{\\\"password\\\":\\\"S3KRET\\\",\\\"x\\\":NaN}\"", expected: "\"{\\\"password\\\":\\\"<redacted>\\\",\\\"x\\\":NaN}\"", framed: true},
		{value: "\"{\\\"password\\\":\\\"S3KRET\\\",\\\"x\\\":NaN}\"", expected: "\"{\\\"password\\\":\\\"<redacted>\\\",\\\"x\\\":NaN}\"", framed: false},
		{value: "\"{\\\"password\\\":\\\"S3KRET\\\",\\\"x\\\":Infinity}\"", expected: "\"{\\\"password\\\":\\\"<redacted>\\\",\\\"x\\\":Infinity}\"", framed: true},
		{value: "\"{\\\"password\\\":\\\"S3KRET\\\",\\\"x\\\":Infinity}\"", expected: "\"{\\\"password\\\":\\\"<redacted>\\\",\\\"x\\\":Infinity}\"", framed: false},
		{value: "\"{\\\"password\\\":\\\"S3KRET\\\",\\\"x\\\":-Infinity}\"", expected: "\"{\\\"password\\\":\\\"<redacted>\\\",\\\"x\\\":-Infinity}\"", framed: true},
		{value: "\"{\\\"password\\\":\\\"S3KRET\\\",\\\"x\\\":-Infinity}\"", expected: "\"{\\\"password\\\":\\\"<redacted>\\\",\\\"x\\\":-Infinity}\"", framed: false},
		{value: "\"{\\\"password\\\":\\\"S3KRET\\\",\\\"x\\\":1E+400}\"", expected: "\"{\\\"password\\\":\\\"<redacted>\\\",\\\"x\\\":Infinity}\"", framed: true},
		{value: "\"{\\\"password\\\":\\\"S3KRET\\\",\\\"x\\\":1E+400}\"", expected: "\"{\\\"password\\\":\\\"<redacted>\\\",\\\"x\\\":Infinity}\"", framed: false},
		{value: "\"{\\\"password\\\":\\\"S3KRET\\\",\\\"x\\\":-1E+400}\"", expected: "\"{\\\"password\\\":\\\"<redacted>\\\",\\\"x\\\":-Infinity}\"", framed: true},
		{value: "\"{\\\"password\\\":\\\"S3KRET\\\",\\\"x\\\":-1E+400}\"", expected: "\"{\\\"password\\\":\\\"<redacted>\\\",\\\"x\\\":-Infinity}\"", framed: false},
		{value: "\"{\\\"password\\\":\\\"S3KRET\\\",\\\"x\\\":[NaN]}\"", expected: "\"{\\\"password\\\":\\\"<redacted>\\\",\\\"x\\\":[NaN]}\"", framed: true},
		{value: "\"{\\\"password\\\":\\\"S3KRET\\\",\\\"x\\\":[NaN]}\"", expected: "\"{\\\"password\\\":\\\"<redacted>\\\",\\\"x\\\":[NaN]}\"", framed: false},
		{value: "\"{\\\"password\\\":\\\"S3KRET\\\",\\\"x\\\":{\\\"y\\\":Infinity}}\"", expected: "\"{\\\"password\\\":\\\"<redacted>\\\",\\\"x\\\":{\\\"y\\\":Infinity}}\"", framed: true},
		{value: "\"{\\\"password\\\":\\\"S3KRET\\\",\\\"x\\\":{\\\"y\\\":Infinity}}\"", expected: "\"{\\\"password\\\":\\\"<redacted>\\\",\\\"x\\\":{\\\"y\\\":Infinity}}\"", framed: false},
		{value: "\"{\\\"\\\\u0070assword\\\":\\\"S3KRET\\\",\\\"x\\\":NaN}\"", expected: "\"{\\\"password\\\":\\\"<redacted>\\\",\\\"x\\\":NaN}\"", framed: true},
		{value: "\"{\\\"\\\\u0070assword\\\":\\\"S3KRET\\\",\\\"x\\\":NaN}\"", expected: "\"{\\\"password\\\":\\\"<redacted>\\\",\\\"x\\\":NaN}\"", framed: false},
		{value: "\"{\\\"\\\\u0070assword\\\":\\\"S3KRET\\\",\\\"x\\\":Infinity}\"", expected: "\"{\\\"password\\\":\\\"<redacted>\\\",\\\"x\\\":Infinity}\"", framed: true},
		{value: "\"{\\\"\\\\u0070assword\\\":\\\"S3KRET\\\",\\\"x\\\":Infinity}\"", expected: "\"{\\\"password\\\":\\\"<redacted>\\\",\\\"x\\\":Infinity}\"", framed: false},
		{value: "\"{\\\"\\\\u0070assword\\\":\\\"S3KRET\\\",\\\"x\\\":-Infinity}\"", expected: "\"{\\\"password\\\":\\\"<redacted>\\\",\\\"x\\\":-Infinity}\"", framed: true},
		{value: "\"{\\\"\\\\u0070assword\\\":\\\"S3KRET\\\",\\\"x\\\":-Infinity}\"", expected: "\"{\\\"password\\\":\\\"<redacted>\\\",\\\"x\\\":-Infinity}\"", framed: false},
		{value: "\"{\\\"\\\\u0070assword\\\":\\\"S3KRET\\\",\\\"x\\\":1E+400}\"", expected: "\"{\\\"password\\\":\\\"<redacted>\\\",\\\"x\\\":Infinity}\"", framed: true},
		{value: "\"{\\\"\\\\u0070assword\\\":\\\"S3KRET\\\",\\\"x\\\":1E+400}\"", expected: "\"{\\\"password\\\":\\\"<redacted>\\\",\\\"x\\\":Infinity}\"", framed: false},
		{value: "\"{\\\"\\\\u0070assword\\\":\\\"S3KRET\\\",\\\"x\\\":-1E+400}\"", expected: "\"{\\\"password\\\":\\\"<redacted>\\\",\\\"x\\\":-Infinity}\"", framed: true},
		{value: "\"{\\\"\\\\u0070assword\\\":\\\"S3KRET\\\",\\\"x\\\":-1E+400}\"", expected: "\"{\\\"password\\\":\\\"<redacted>\\\",\\\"x\\\":-Infinity}\"", framed: false},
		{value: "\"{\\\"\\\\u0070assword\\\":\\\"S3KRET\\\",\\\"x\\\":[NaN]}\"", expected: "\"{\\\"password\\\":\\\"<redacted>\\\",\\\"x\\\":[NaN]}\"", framed: true},
		{value: "\"{\\\"\\\\u0070assword\\\":\\\"S3KRET\\\",\\\"x\\\":[NaN]}\"", expected: "\"{\\\"password\\\":\\\"<redacted>\\\",\\\"x\\\":[NaN]}\"", framed: false},
		{value: "\"{\\\"\\\\u0070assword\\\":\\\"S3KRET\\\",\\\"x\\\":{\\\"y\\\":Infinity}}\"", expected: "\"{\\\"password\\\":\\\"<redacted>\\\",\\\"x\\\":{\\\"y\\\":Infinity}}\"", framed: true},
		{value: "\"{\\\"\\\\u0070assword\\\":\\\"S3KRET\\\",\\\"x\\\":{\\\"y\\\":Infinity}}\"", expected: "\"{\\\"password\\\":\\\"<redacted>\\\",\\\"x\\\":{\\\"y\\\":Infinity}}\"", framed: false},
		{value: "\"{\\\"token\\\":\\\"S3KRET\\\",\\\"x\\\":NaN}\"", expected: "\"{\\\"token\\\":\\\"<redacted>\\\",\\\"x\\\":NaN}\"", framed: true},
		{value: "\"{\\\"token\\\":\\\"S3KRET\\\",\\\"x\\\":NaN}\"", expected: "\"{\\\"token\\\":\\\"<redacted>\\\",\\\"x\\\":NaN}\"", framed: false},
		{value: "\"{\\\"token\\\":\\\"S3KRET\\\",\\\"x\\\":Infinity}\"", expected: "\"{\\\"token\\\":\\\"<redacted>\\\",\\\"x\\\":Infinity}\"", framed: true},
		{value: "\"{\\\"token\\\":\\\"S3KRET\\\",\\\"x\\\":Infinity}\"", expected: "\"{\\\"token\\\":\\\"<redacted>\\\",\\\"x\\\":Infinity}\"", framed: false},
		{value: "\"{\\\"token\\\":\\\"S3KRET\\\",\\\"x\\\":-Infinity}\"", expected: "\"{\\\"token\\\":\\\"<redacted>\\\",\\\"x\\\":-Infinity}\"", framed: true},
		{value: "\"{\\\"token\\\":\\\"S3KRET\\\",\\\"x\\\":-Infinity}\"", expected: "\"{\\\"token\\\":\\\"<redacted>\\\",\\\"x\\\":-Infinity}\"", framed: false},
		{value: "\"{\\\"token\\\":\\\"S3KRET\\\",\\\"x\\\":1E+400}\"", expected: "\"{\\\"token\\\":\\\"<redacted>\\\",\\\"x\\\":Infinity}\"", framed: true},
		{value: "\"{\\\"token\\\":\\\"S3KRET\\\",\\\"x\\\":1E+400}\"", expected: "\"{\\\"token\\\":\\\"<redacted>\\\",\\\"x\\\":Infinity}\"", framed: false},
		{value: "\"{\\\"token\\\":\\\"S3KRET\\\",\\\"x\\\":-1E+400}\"", expected: "\"{\\\"token\\\":\\\"<redacted>\\\",\\\"x\\\":-Infinity}\"", framed: true},
		{value: "\"{\\\"token\\\":\\\"S3KRET\\\",\\\"x\\\":-1E+400}\"", expected: "\"{\\\"token\\\":\\\"<redacted>\\\",\\\"x\\\":-Infinity}\"", framed: false},
		{value: "\"{\\\"token\\\":\\\"S3KRET\\\",\\\"x\\\":[NaN]}\"", expected: "\"{\\\"token\\\":\\\"<redacted>\\\",\\\"x\\\":[NaN]}\"", framed: true},
		{value: "\"{\\\"token\\\":\\\"S3KRET\\\",\\\"x\\\":[NaN]}\"", expected: "\"{\\\"token\\\":\\\"<redacted>\\\",\\\"x\\\":[NaN]}\"", framed: false},
		{value: "\"{\\\"token\\\":\\\"S3KRET\\\",\\\"x\\\":{\\\"y\\\":Infinity}}\"", expected: "\"{\\\"token\\\":\\\"<redacted>\\\",\\\"x\\\":{\\\"y\\\":Infinity}}\"", framed: true},
		{value: "\"{\\\"token\\\":\\\"S3KRET\\\",\\\"x\\\":{\\\"y\\\":Infinity}}\"", expected: "\"{\\\"token\\\":\\\"<redacted>\\\",\\\"x\\\":{\\\"y\\\":Infinity}}\"", framed: false},
		{value: "\"{\\\"\\\\u0074oken\\\":\\\"S3KRET\\\",\\\"x\\\":NaN}\"", expected: "\"{\\\"token\\\":\\\"<redacted>\\\",\\\"x\\\":NaN}\"", framed: true},
		{value: "\"{\\\"\\\\u0074oken\\\":\\\"S3KRET\\\",\\\"x\\\":NaN}\"", expected: "\"{\\\"token\\\":\\\"<redacted>\\\",\\\"x\\\":NaN}\"", framed: false},
		{value: "\"{\\\"\\\\u0074oken\\\":\\\"S3KRET\\\",\\\"x\\\":Infinity}\"", expected: "\"{\\\"token\\\":\\\"<redacted>\\\",\\\"x\\\":Infinity}\"", framed: true},
		{value: "\"{\\\"\\\\u0074oken\\\":\\\"S3KRET\\\",\\\"x\\\":Infinity}\"", expected: "\"{\\\"token\\\":\\\"<redacted>\\\",\\\"x\\\":Infinity}\"", framed: false},
		{value: "\"{\\\"\\\\u0074oken\\\":\\\"S3KRET\\\",\\\"x\\\":-Infinity}\"", expected: "\"{\\\"token\\\":\\\"<redacted>\\\",\\\"x\\\":-Infinity}\"", framed: true},
		{value: "\"{\\\"\\\\u0074oken\\\":\\\"S3KRET\\\",\\\"x\\\":-Infinity}\"", expected: "\"{\\\"token\\\":\\\"<redacted>\\\",\\\"x\\\":-Infinity}\"", framed: false},
		{value: "\"{\\\"\\\\u0074oken\\\":\\\"S3KRET\\\",\\\"x\\\":1E+400}\"", expected: "\"{\\\"token\\\":\\\"<redacted>\\\",\\\"x\\\":Infinity}\"", framed: true},
		{value: "\"{\\\"\\\\u0074oken\\\":\\\"S3KRET\\\",\\\"x\\\":1E+400}\"", expected: "\"{\\\"token\\\":\\\"<redacted>\\\",\\\"x\\\":Infinity}\"", framed: false},
		{value: "\"{\\\"\\\\u0074oken\\\":\\\"S3KRET\\\",\\\"x\\\":-1E+400}\"", expected: "\"{\\\"token\\\":\\\"<redacted>\\\",\\\"x\\\":-Infinity}\"", framed: true},
		{value: "\"{\\\"\\\\u0074oken\\\":\\\"S3KRET\\\",\\\"x\\\":-1E+400}\"", expected: "\"{\\\"token\\\":\\\"<redacted>\\\",\\\"x\\\":-Infinity}\"", framed: false},
		{value: "\"{\\\"\\\\u0074oken\\\":\\\"S3KRET\\\",\\\"x\\\":[NaN]}\"", expected: "\"{\\\"token\\\":\\\"<redacted>\\\",\\\"x\\\":[NaN]}\"", framed: true},
		{value: "\"{\\\"\\\\u0074oken\\\":\\\"S3KRET\\\",\\\"x\\\":[NaN]}\"", expected: "\"{\\\"token\\\":\\\"<redacted>\\\",\\\"x\\\":[NaN]}\"", framed: false},
		{value: "\"{\\\"\\\\u0074oken\\\":\\\"S3KRET\\\",\\\"x\\\":{\\\"y\\\":Infinity}}\"", expected: "\"{\\\"token\\\":\\\"<redacted>\\\",\\\"x\\\":{\\\"y\\\":Infinity}}\"", framed: true},
		{value: "\"{\\\"\\\\u0074oken\\\":\\\"S3KRET\\\",\\\"x\\\":{\\\"y\\\":Infinity}}\"", expected: "\"{\\\"token\\\":\\\"<redacted>\\\",\\\"x\\\":{\\\"y\\\":Infinity}}\"", framed: false},
		{value: "\"{\\\"api_key\\\":\\\"S3KRET\\\",\\\"x\\\":NaN}\"", expected: "\"{\\\"api_key\\\":\\\"<redacted>\\\",\\\"x\\\":NaN}\"", framed: true},
		{value: "\"{\\\"api_key\\\":\\\"S3KRET\\\",\\\"x\\\":NaN}\"", expected: "\"{\\\"api_key\\\":\\\"<redacted>\\\",\\\"x\\\":NaN}\"", framed: false},
		{value: "\"{\\\"api_key\\\":\\\"S3KRET\\\",\\\"x\\\":Infinity}\"", expected: "\"{\\\"api_key\\\":\\\"<redacted>\\\",\\\"x\\\":Infinity}\"", framed: true},
		{value: "\"{\\\"api_key\\\":\\\"S3KRET\\\",\\\"x\\\":Infinity}\"", expected: "\"{\\\"api_key\\\":\\\"<redacted>\\\",\\\"x\\\":Infinity}\"", framed: false},
		{value: "\"{\\\"api_key\\\":\\\"S3KRET\\\",\\\"x\\\":-Infinity}\"", expected: "\"{\\\"api_key\\\":\\\"<redacted>\\\",\\\"x\\\":-Infinity}\"", framed: true},
		{value: "\"{\\\"api_key\\\":\\\"S3KRET\\\",\\\"x\\\":-Infinity}\"", expected: "\"{\\\"api_key\\\":\\\"<redacted>\\\",\\\"x\\\":-Infinity}\"", framed: false},
		{value: "\"{\\\"api_key\\\":\\\"S3KRET\\\",\\\"x\\\":1E+400}\"", expected: "\"{\\\"api_key\\\":\\\"<redacted>\\\",\\\"x\\\":Infinity}\"", framed: true},
		{value: "\"{\\\"api_key\\\":\\\"S3KRET\\\",\\\"x\\\":1E+400}\"", expected: "\"{\\\"api_key\\\":\\\"<redacted>\\\",\\\"x\\\":Infinity}\"", framed: false},
		{value: "\"{\\\"api_key\\\":\\\"S3KRET\\\",\\\"x\\\":-1E+400}\"", expected: "\"{\\\"api_key\\\":\\\"<redacted>\\\",\\\"x\\\":-Infinity}\"", framed: true},
		{value: "\"{\\\"api_key\\\":\\\"S3KRET\\\",\\\"x\\\":-1E+400}\"", expected: "\"{\\\"api_key\\\":\\\"<redacted>\\\",\\\"x\\\":-Infinity}\"", framed: false},
		{value: "\"{\\\"api_key\\\":\\\"S3KRET\\\",\\\"x\\\":[NaN]}\"", expected: "\"{\\\"api_key\\\":\\\"<redacted>\\\",\\\"x\\\":[NaN]}\"", framed: true},
		{value: "\"{\\\"api_key\\\":\\\"S3KRET\\\",\\\"x\\\":[NaN]}\"", expected: "\"{\\\"api_key\\\":\\\"<redacted>\\\",\\\"x\\\":[NaN]}\"", framed: false},
		{value: "\"{\\\"api_key\\\":\\\"S3KRET\\\",\\\"x\\\":{\\\"y\\\":Infinity}}\"", expected: "\"{\\\"api_key\\\":\\\"<redacted>\\\",\\\"x\\\":{\\\"y\\\":Infinity}}\"", framed: true},
		{value: "\"{\\\"api_key\\\":\\\"S3KRET\\\",\\\"x\\\":{\\\"y\\\":Infinity}}\"", expected: "\"{\\\"api_key\\\":\\\"<redacted>\\\",\\\"x\\\":{\\\"y\\\":Infinity}}\"", framed: false},
		{value: "\"{\\\"\\\\u0061pi_key\\\":\\\"S3KRET\\\",\\\"x\\\":NaN}\"", expected: "\"{\\\"api_key\\\":\\\"<redacted>\\\",\\\"x\\\":NaN}\"", framed: true},
		{value: "\"{\\\"\\\\u0061pi_key\\\":\\\"S3KRET\\\",\\\"x\\\":NaN}\"", expected: "\"{\\\"api_key\\\":\\\"<redacted>\\\",\\\"x\\\":NaN}\"", framed: false},
		{value: "\"{\\\"\\\\u0061pi_key\\\":\\\"S3KRET\\\",\\\"x\\\":Infinity}\"", expected: "\"{\\\"api_key\\\":\\\"<redacted>\\\",\\\"x\\\":Infinity}\"", framed: true},
		{value: "\"{\\\"\\\\u0061pi_key\\\":\\\"S3KRET\\\",\\\"x\\\":Infinity}\"", expected: "\"{\\\"api_key\\\":\\\"<redacted>\\\",\\\"x\\\":Infinity}\"", framed: false},
		{value: "\"{\\\"\\\\u0061pi_key\\\":\\\"S3KRET\\\",\\\"x\\\":-Infinity}\"", expected: "\"{\\\"api_key\\\":\\\"<redacted>\\\",\\\"x\\\":-Infinity}\"", framed: true},
		{value: "\"{\\\"\\\\u0061pi_key\\\":\\\"S3KRET\\\",\\\"x\\\":-Infinity}\"", expected: "\"{\\\"api_key\\\":\\\"<redacted>\\\",\\\"x\\\":-Infinity}\"", framed: false},
		{value: "\"{\\\"\\\\u0061pi_key\\\":\\\"S3KRET\\\",\\\"x\\\":1E+400}\"", expected: "\"{\\\"api_key\\\":\\\"<redacted>\\\",\\\"x\\\":Infinity}\"", framed: true},
		{value: "\"{\\\"\\\\u0061pi_key\\\":\\\"S3KRET\\\",\\\"x\\\":1E+400}\"", expected: "\"{\\\"api_key\\\":\\\"<redacted>\\\",\\\"x\\\":Infinity}\"", framed: false},
		{value: "\"{\\\"\\\\u0061pi_key\\\":\\\"S3KRET\\\",\\\"x\\\":-1E+400}\"", expected: "\"{\\\"api_key\\\":\\\"<redacted>\\\",\\\"x\\\":-Infinity}\"", framed: true},
		{value: "\"{\\\"\\\\u0061pi_key\\\":\\\"S3KRET\\\",\\\"x\\\":-1E+400}\"", expected: "\"{\\\"api_key\\\":\\\"<redacted>\\\",\\\"x\\\":-Infinity}\"", framed: false},
		{value: "\"{\\\"\\\\u0061pi_key\\\":\\\"S3KRET\\\",\\\"x\\\":[NaN]}\"", expected: "\"{\\\"api_key\\\":\\\"<redacted>\\\",\\\"x\\\":[NaN]}\"", framed: true},
		{value: "\"{\\\"\\\\u0061pi_key\\\":\\\"S3KRET\\\",\\\"x\\\":[NaN]}\"", expected: "\"{\\\"api_key\\\":\\\"<redacted>\\\",\\\"x\\\":[NaN]}\"", framed: false},
		{value: "\"{\\\"\\\\u0061pi_key\\\":\\\"S3KRET\\\",\\\"x\\\":{\\\"y\\\":Infinity}}\"", expected: "\"{\\\"api_key\\\":\\\"<redacted>\\\",\\\"x\\\":{\\\"y\\\":Infinity}}\"", framed: true},
		{value: "\"{\\\"\\\\u0061pi_key\\\":\\\"S3KRET\\\",\\\"x\\\":{\\\"y\\\":Infinity}}\"", expected: "\"{\\\"api_key\\\":\\\"<redacted>\\\",\\\"x\\\":{\\\"y\\\":Infinity}}\"", framed: false},
	}
	var cases []pairRuleCase
	for i, r := range rows {
		cases = append(cases, pairRuleCase{name: "case-" + itoa(i), value: r.value, expected: r.expected, framed: r.framed})
	}
	runPairRuleCases(t, cases)
}

func itoa(n int) string {
	if n == 0 {
		return "0"
	}
	var d []byte
	for ; n > 0; n /= 10 {
		d = append([]byte{byte('0' + n%10)}, d...)
	}
	return string(d)
}

// The value of a credential-named member whose name is written with a JSON escape is replaced even when the text is no complete
// JSON value, so no pair rule read the name; a stand-in for a bare literal is never mistaken for a number the text holds.
func TestEscapedCredentialMemberInUnparsedJSON(t *testing.T) {
	for i, value := range []string{
		`{"\u0070wd":"S3KRET","x":`,
		`{"\u0070assword":S3KRET,"y":1`,
		`{"api\u005fkey":"S3KRET"`,
		`{"\u0074oken":{"a":"S3KRET"},"z":[1,`,
		`{"x":nan,"\u0070wd":"S3KRET"}`,
		`{"x":NaNx,"\u0070wd":"S3KRET"}`,
	} {
		for _, framed := range []bool{true, false} {
			got := privacyWalk(value, "", nil, newPrivacyBudget(), 0, mediaHint{}, nil, framed)
			if text, _ := got.(string); strings.Contains(text, "S3KRET") {
				t.Fatalf("case %d framed=%v: %q", i, framed, text)
			}
		}
	}
}

func TestStandInNumberInTheTextIsKept(t *testing.T) {
	stand := "0.123456789012345678901234567890e1"
	value := `{"x":NaN,"y":` + stand + `,"pwd":"S3KRET"}`
	got, _ := privacyWalk(value, "", nil, newPrivacyBudget(), 0, mediaHint{}, nil, true).(string)
	want := `{"x":NaN,"y":1.2345678901234567,"pwd":"<redacted>"}`
	if got != want {
		t.Fatalf("got %q; want %q", got, want)
	}
}
