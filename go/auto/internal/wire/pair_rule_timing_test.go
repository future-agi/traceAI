package wire

import (
	"strings"
	"testing"
	"time"
)

// Rule group 9: the pair rule is linear on the hot path. Each adversarial input (many '?', '=', long names, flags and quoted
// names) is exported at 100,000 and at 1,600,000 characters. The larger export must finish within the stated bound and grow no
// faster than linearly (sixteen times the input, at most 40 times the time plus a fixed allowance), and every input must have
// its credential pairs replaced.
const (
	pairRuleTimingChars = 1_600_000
	pairRuleTimingBound = 30 * time.Second
)

func TestPairRuleLinearTime(t *testing.T) {
	units := []struct{ name, unit string }{
		{"question-marks", "q=" + strings.Repeat("?", 9) + "token=v&"},
		{"long-names", strings.Repeat("n", 127) + "_token=v;"},
		{"many-equals", "token=a=b=c=d=e "},
		{"flags-next-line", "--token\nv "},
		{"word-separators", "--token a=b:c%3Dd "},
		{"quoted-equals", `"token"=v `},
		{"encoded-escapes", "--token\n" + strings.Repeat("%41", 40) + "=v "},
	}
	export := func(value string) (string, time.Duration) {
		start := time.Now()
		budget := newPrivacyBudget()
		budget.nodes, budget.bytes = 1<<30, 1<<30
		got := privacyWalk(value, "", nil, budget, 0, mediaHint{}, nil, false).(string)
		return got, time.Since(start)
	}
	for _, u := range units {
		t.Run(u.name, func(t *testing.T) {
			sized := func(n int) string { return strings.Repeat(u.unit, n/len(u.unit)+1)[:n] }
			small, large := sized(pairRuleTimingChars/16), sized(pairRuleTimingChars)
			_, smallTime := export(small)
			got, largeTime := export(large)
			if got == large || !strings.Contains(got, CredentialRedacted) {
				t.Fatalf("no credential pair replaced in %d characters", len(large))
			}
			if largeTime > pairRuleTimingBound {
				t.Fatalf("%d characters took %v, over the %v bound", len(large), largeTime, pairRuleTimingBound)
			}
			if largeTime > 40*smallTime+time.Second {
				t.Fatalf("not linear: %d characters took %v, %d took %v", len(small), smallTime, len(large), largeTime)
			}
			t.Logf("%d chars %v; %d chars %v", len(small), smallTime, len(large), largeTime)
		})
	}
}
