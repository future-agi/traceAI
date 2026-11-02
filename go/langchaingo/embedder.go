package traceailangchaingo

import (
	"context"
	"github.com/tmc/langchaingo/embeddings"
	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/codes"
	"go.opentelemetry.io/otel/trace"
)

// Embedder wraps the upstream interface because LangChainGo has no embedding callbacks.
type Embedder struct {
	inner  embeddings.Embedder
	tracer trace.Tracer
}

var _ embeddings.Embedder = (*Embedder)(nil)

func (h *Handler) WrapEmbedder(inner embeddings.Embedder) *Embedder {
	return &Embedder{inner: inner, tracer: h.tracer}
}
func (e *Embedder) EmbedQuery(ctx context.Context, text string) ([]float32, error) {
	ctx, span := e.tracer.Start(ctx, "langchaingo.embed_query", trace.WithAttributes(attribute.String("gen_ai.operation.name", "embeddings"), attribute.String("gen_ai.span.kind", "EMBEDDING")))
	defer span.End()
	result, err := e.inner.EmbedQuery(ctx, text)
	if err != nil {
		span.SetStatus(codes.Error, "embedding failed")
	} else {
		span.SetAttributes(attribute.Int("embedding.dimensions", len(result)))
	}
	return result, err
}
func (e *Embedder) EmbedDocuments(ctx context.Context, texts []string) ([][]float32, error) {
	ctx, span := e.tracer.Start(ctx, "langchaingo.embed_documents", trace.WithAttributes(attribute.String("gen_ai.operation.name", "embeddings"), attribute.String("gen_ai.span.kind", "EMBEDDING"), attribute.Int("embedding.documents", len(texts))))
	defer span.End()
	result, err := e.inner.EmbedDocuments(ctx, texts)
	if err != nil {
		span.SetStatus(codes.Error, "embedding failed")
	}
	return result, err
}
