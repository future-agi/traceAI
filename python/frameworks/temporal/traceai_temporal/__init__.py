"""Replay-safe Temporal tracing; imports stay lightweight in workflow sandboxes."""

__version__ = "0.1.0"
__all__ = ["TemporalInstrumentor", "TemporalInterceptor", "__version__"]


def __getattr__(name):
    if name == "TemporalInstrumentor":
        from ._instrumentor import TemporalInstrumentor
        return TemporalInstrumentor
    if name == "TemporalInterceptor":
        from ._interceptor import TemporalInterceptor
        return TemporalInterceptor
    raise AttributeError(name)
