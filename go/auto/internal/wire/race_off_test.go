//go:build !race

package wire

// raceEnabled is false when the tests run without the race detector.
const raceEnabled = false
