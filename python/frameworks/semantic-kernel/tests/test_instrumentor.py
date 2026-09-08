import pytest
from traceai_semantic_kernel import SemanticKernelInstrumentor
from semantic_kernel.kernel import Kernel

def test_instrumentor():
    instrumentor = SemanticKernelInstrumentor()
    instrumentor.instrument()
    assert instrumentor._original_kernel_invoke is not None
    instrumentor.uninstrument()
    assert instrumentor._original_kernel_invoke is None
