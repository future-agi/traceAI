package ai.traceai.azure.search;

import ai.traceai.FISpanKind;
import ai.traceai.FITracer;
import ai.traceai.SemanticConventions;
import com.azure.search.documents.SearchClient;
import com.azure.search.documents.models.*;
import com.azure.search.documents.util.SearchPagedIterable;
import io.opentelemetry.api.common.AttributeKey;
import io.opentelemetry.api.trace.StatusCode;
import io.opentelemetry.sdk.testing.junit5.OpenTelemetryExtension;
import io.opentelemetry.sdk.trace.data.SpanData;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.extension.ExtendWith;
import org.junit.jupiter.api.extension.RegisterExtension;
import org.mockito.Mock;
import org.mockito.junit.jupiter.MockitoExtension;

import java.util.Arrays;
import java.util.Collections;
import java.util.List;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;
import static org.mockito.ArgumentMatchers.any;
import static org.mockito.ArgumentMatchers.anyString;
import static org.mockito.Mockito.*;

@ExtendWith(MockitoExtension.class)
class TracedSearchClientTest {

    @RegisterExtension
    static final OpenTelemetryExtension otelTesting = OpenTelemetryExtension.create();

    @Mock
    private SearchClient mockSearchClient;

    @Mock
    private SearchPagedIterable mockSearchResults;

    @Mock
    private IndexDocumentsResult mockIndexResult;

    private FITracer tracer;
    private TracedSearchClient tracedClient;

    private static final String INDEX_NAME = "test-index";

    @BeforeEach
    void setup() {
        tracer = new FITracer(otelTesting.getOpenTelemetry().getTracer("test"));
        tracedClient = new TracedSearchClient(mockSearchClient, INDEX_NAME, tracer);
    }

    @Test
    void shouldCreateSpanForVectorSearch() {
        List<Float> vector = Arrays.asList(0.1f, 0.2f, 0.3f);

        when(mockSearchClient.search(anyString(), any(SearchOptions.class)))
            .thenReturn(mockSearchResults);
        when(mockSearchResults.iterator()).thenReturn(Collections.emptyIterator());

        tracedClient.searchWithVector("test query", vector, "contentVector", 10);

        List<SpanData> spans = otelTesting.getSpans();
        assertThat(spans).hasSize(1);

        SpanData spanData = spans.get(0);
        assertThat(spanData.getName()).isEqualTo("Azure Search Vector Query");
        assertThat(spanData.getAttributes().get(
            AttributeKey.stringKey(SemanticConventions.FI_SPAN_KIND)
        )).isEqualTo("RETRIEVER");
        assertThat(spanData.getAttributes().get(
            AttributeKey.stringKey(SemanticConventions.LLM_SYSTEM)
        )).isEqualTo("azure-search");
        assertThat(spanData.getAttributes().get(
            AttributeKey.stringKey("db.system")
        )).isEqualTo("azure-ai-search");
        assertThat(spanData.getAttributes().get(
            AttributeKey.stringKey("azure_search.index")
        )).isEqualTo(INDEX_NAME);
        assertThat(spanData.getAttributes().get(
            AttributeKey.longKey(SemanticConventions.RETRIEVER_TOP_K)
        )).isEqualTo(10L);
        assertThat(spanData.getAttributes().get(
            AttributeKey.longKey(SemanticConventions.EMBEDDING_DIMENSIONS)
        )).isEqualTo(3L);
        assertThat(spanData.getAttributes().get(
            AttributeKey.stringKey("azure_search.search_mode")
        )).isEqualTo("vector");
        assertThat(spanData.getStatus().getStatusCode()).isEqualTo(StatusCode.OK);
    }

    @Test
    void shouldCreateSpanForFilteredVectorSearch() {
        List<Float> vector = Arrays.asList(0.1f, 0.2f, 0.3f, 0.4f, 0.5f);
        String filter = "category eq 'technology'";

        when(mockSearchClient.search(anyString(), any(SearchOptions.class)))
            .thenReturn(mockSearchResults);
        when(mockSearchResults.iterator()).thenReturn(Collections.emptyIterator());

        tracedClient.searchWithVectorAndFilter("test query", vector, "contentVector", 5, filter);

        List<SpanData> spans = otelTesting.getSpans();
        assertThat(spans).hasSize(1);

        SpanData spanData = spans.get(0);
        assertThat(spanData.getName()).isEqualTo("Azure Search Filtered Vector Query");
        assertThat(spanData.getAttributes().get(
            AttributeKey.booleanKey("azure_search.has_filter")
        )).isTrue();
        assertThat(spanData.getAttributes().get(
            AttributeKey.stringKey("azure_search.filter")
        )).isEqualTo(filter);
        assertThat(spanData.getAttributes().get(
            AttributeKey.longKey(SemanticConventions.EMBEDDING_DIMENSIONS)
        )).isEqualTo(5L);
    }

    @Test
    void shouldCreateSpanForHybridSearch() {
        List<Float> vector = Arrays.asList(0.1f, 0.2f, 0.3f);

        when(mockSearchClient.search(anyString(), any(SearchOptions.class)))
            .thenReturn(mockSearchResults);
        when(mockSearchResults.iterator()).thenReturn(Collections.emptyIterator());

        tracedClient.hybridSearch("test query", vector, "contentVector", 10);

        List<SpanData> spans = otelTesting.getSpans();
        assertThat(spans).hasSize(1);

        SpanData spanData = spans.get(0);
        assertThat(spanData.getName()).isEqualTo("Azure Search Hybrid Query");
        assertThat(spanData.getAttributes().get(
            AttributeKey.stringKey("azure_search.search_mode")
        )).isEqualTo("hybrid");
    }

    @Test
    void shouldCreateSpanForTextSearch() {
        when(mockSearchClient.search(anyString(), any(SearchOptions.class)))
            .thenReturn(mockSearchResults);
        when(mockSearchResults.iterator()).thenReturn(Collections.emptyIterator());

        tracedClient.search("test query", 20);

        List<SpanData> spans = otelTesting.getSpans();
        assertThat(spans).hasSize(1);

        SpanData spanData = spans.get(0);
        assertThat(spanData.getName()).isEqualTo("Azure Search Text Query");
        assertThat(spanData.getAttributes().get(
            AttributeKey.stringKey("azure_search.search_mode")
        )).isEqualTo("text");
        assertThat(spanData.getAttributes().get(
            AttributeKey.longKey(SemanticConventions.RETRIEVER_TOP_K)
        )).isEqualTo(20L);
    }

    @Test
    void shouldCreateSpanForUploadDocuments() {
        List<IndexingResult> indexingResults = Arrays.asList(
            createIndexingResult("doc1", true),
            createIndexingResult("doc2", true),
            createIndexingResult("doc3", false)
        );

        when(mockIndexResult.getResults()).thenReturn(indexingResults);
        when(mockSearchClient.uploadDocuments(any())).thenReturn(mockIndexResult);

        List<String> documents = Arrays.asList("doc1", "doc2", "doc3");
        tracedClient.uploadDocuments(documents);

        List<SpanData> spans = otelTesting.getSpans();
        assertThat(spans).hasSize(1);

        SpanData spanData = spans.get(0);
        assertThat(spanData.getName()).isEqualTo("Azure Search Upload Documents");
        assertThat(spanData.getAttributes().get(
            AttributeKey.stringKey(SemanticConventions.FI_SPAN_KIND)
        )).isEqualTo("EMBEDDING");
        assertThat(spanData.getAttributes().get(
            AttributeKey.stringKey("azure_search.operation")
        )).isEqualTo("upload");
        assertThat(spanData.getAttributes().get(
            AttributeKey.longKey("azure_search.documents_count")
        )).isEqualTo(3L);
        assertThat(spanData.getAttributes().get(
            AttributeKey.longKey("azure_search.success_count")
        )).isEqualTo(2L);
        assertThat(spanData.getAttributes().get(
            AttributeKey.longKey("azure_search.failed_count")
        )).isEqualTo(1L);
    }

    @Test
    void shouldCreateSpanForMergeOrUploadDocuments() {
        List<IndexingResult> indexingResults = Collections.singletonList(
            createIndexingResult("doc1", true)
        );

        when(mockIndexResult.getResults()).thenReturn(indexingResults);
        when(mockSearchClient.mergeOrUploadDocuments(any())).thenReturn(mockIndexResult);

        List<String> documents = Collections.singletonList("doc1");
        tracedClient.mergeOrUploadDocuments(documents);

        List<SpanData> spans = otelTesting.getSpans();
        assertThat(spans).hasSize(1);

        SpanData spanData = spans.get(0);
        assertThat(spanData.getName()).isEqualTo("Azure Search Merge or Upload Documents");
        assertThat(spanData.getAttributes().get(
            AttributeKey.stringKey("azure_search.operation")
        )).isEqualTo("merge_or_upload");
    }

    @Test
    void shouldCreateSpanForDeleteDocuments() {
        List<IndexingResult> indexingResults = Arrays.asList(
            createIndexingResult("doc1", true),
            createIndexingResult("doc2", true)
        );

        when(mockIndexResult.getResults()).thenReturn(indexingResults);
        when(mockSearchClient.deleteDocuments(any())).thenReturn(mockIndexResult);

        List<String> documents = Arrays.asList("doc1", "doc2");
        tracedClient.deleteDocuments(documents);

        List<SpanData> spans = otelTesting.getSpans();
        assertThat(spans).hasSize(1);

        SpanData spanData = spans.get(0);
        assertThat(spanData.getName()).isEqualTo("Azure Search Delete Documents");
        assertThat(spanData.getAttributes().get(
            AttributeKey.stringKey("azure_search.operation")
        )).isEqualTo("delete");
    }

    @Test
    void shouldCreateSpanForGetDocument() {
        TestDocument expectedDoc = new TestDocument("test-key", "test content");

        when(mockSearchClient.getDocument(anyString(), any())).thenReturn(expectedDoc);

        TestDocument result = tracedClient.getDocument("test-key", TestDocument.class);

        List<SpanData> spans = otelTesting.getSpans();
        assertThat(spans).hasSize(1);

        SpanData spanData = spans.get(0);
        assertThat(spanData.getName()).isEqualTo("Azure Search Get Document");
        assertThat(spanData.getAttributes().get(
            AttributeKey.stringKey("azure_search.operation")
        )).isEqualTo("get_document");
        assertThat(spanData.getAttributes().get(
            AttributeKey.stringKey("azure_search.document_key")
        )).isEqualTo("test-key");
        assertThat(spanData.getAttributes().get(
            AttributeKey.booleanKey("azure_search.document_found")
        )).isTrue();

        assertThat(result).isEqualTo(expectedDoc);
    }

    @Test
    void shouldCreateSpanForGetDocumentCount() {
        when(mockSearchClient.getDocumentCount()).thenReturn(1000L);

        long count = tracedClient.getDocumentCount();

        List<SpanData> spans = otelTesting.getSpans();
        assertThat(spans).hasSize(1);

        SpanData spanData = spans.get(0);
        assertThat(spanData.getName()).isEqualTo("Azure Search Get Document Count");
        assertThat(spanData.getAttributes().get(
            AttributeKey.stringKey("azure_search.operation")
        )).isEqualTo("get_document_count");
        assertThat(spanData.getAttributes().get(
            AttributeKey.longKey("azure_search.total_documents")
        )).isEqualTo(1000L);

        assertThat(count).isEqualTo(1000L);
    }

    @Test
    void shouldRecordErrorOnVectorSearchFailure() {
        List<Float> vector = Arrays.asList(0.1f, 0.2f, 0.3f);

        when(mockSearchClient.search(anyString(), any(SearchOptions.class)))
            .thenThrow(new RuntimeException("Search service unavailable"));

        assertThatThrownBy(() -> tracedClient.searchWithVector("test", vector, "contentVector", 10))
            .isInstanceOf(RuntimeException.class)
            .hasMessageContaining("Azure Search vector query failed");

        List<SpanData> spans = otelTesting.getSpans();
        assertThat(spans).hasSize(1);

        SpanData spanData = spans.get(0);
        assertThat(spanData.getStatus().getStatusCode()).isEqualTo(StatusCode.ERROR);
    }

    @Test
    void shouldRecordErrorOnUploadFailure() {
        when(mockSearchClient.uploadDocuments(any()))
            .thenThrow(new RuntimeException("Upload failed"));

        assertThatThrownBy(() -> tracedClient.uploadDocuments(Collections.singletonList("doc")))
            .isInstanceOf(RuntimeException.class)
            .hasMessageContaining("Azure Search upload documents failed");

        List<SpanData> spans = otelTesting.getSpans();
        assertThat(spans).hasSize(1);

        SpanData spanData = spans.get(0);
        assertThat(spanData.getStatus().getStatusCode()).isEqualTo(StatusCode.ERROR);
    }

    @Test
    void shouldReturnUnwrappedClient() {
        assertThat(tracedClient.unwrap()).isSameAs(mockSearchClient);
    }

    @Test
    void shouldReturnIndexName() {
        assertThat(tracedClient.getIndexName()).isEqualTo(INDEX_NAME);
    }

    private IndexingResult createIndexingResult(String key, boolean succeeded) {
        IndexingResult result = mock(IndexingResult.class);
        when(result.getKey()).thenReturn(key);
        when(result.isSucceeded()).thenReturn(succeeded);
        return result;
    }

    static class TestDocument {
        private final String id;
        private final String content;

        TestDocument(String id, String content) {
            this.id = id;
            this.content = content;
        }

        public String getId() {
            return id;
        }

        public String getContent() {
            return content;
        }
    }
}
