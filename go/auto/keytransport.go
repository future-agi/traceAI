package auto

import (
	"errors"
	"sync"
)

var insecureWarning sync.Once
var errKeyTransport = errors.New("traceai auto: refusing to send Future AGI keys over this transport")

func checkDialAddress(written, addr string) error { return nil }
