package main

import (
	"github.com/future-agi/traceAI/go/launcher"
	"os"
)

func main() { os.Exit(launcher.Main(os.Args[1:])) }
