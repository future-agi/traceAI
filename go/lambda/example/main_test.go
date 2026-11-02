package main

import (
	"context"
	"testing"

	"github.com/aws/aws-lambda-go/lambda"
)

func TestHandlerUsesLambdaSerialization(t *testing.T) {
	handler := lambda.NewHandler(handle)
	response, err := handler.Invoke(context.Background(), []byte(`{"name":"Ada"}`))
	if err != nil || string(response) != `"Hello, Ada"` {
		t.Fatalf("unexpected response: %s, error: %v", response, err)
	}
	if _, err := handler.Invoke(context.Background(), []byte(`{}`)); err == nil {
		t.Fatal("missing name should return an application error")
	}
}
