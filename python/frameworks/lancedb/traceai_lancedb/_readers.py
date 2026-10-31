"""Lazy reader proxies that preserve Arrow and async consumption protocols."""

from __future__ import annotations

from collections.abc import Iterator
from types import TracebackType
from typing import TYPE_CHECKING, Any
from weakref import finalize

from wrapt import ObjectProxy

if TYPE_CHECKING:
    import pyarrow as pa
    from lancedb.arrow import AsyncRecordBatchReader
    from typing_extensions import Self

    from traceai_lancedb._wrappers import Execution


class _Lifetime:
    def __init__(self, execution: Execution) -> None:
        self.execution = execution
        # A cast reader shares this lifetime. Dropping either proxy must not end
        # an execution while the other proxy (or an Arrow C stream) owns it.
        self.finalizer = finalize(self, execution.finish)


class SyncReader(ObjectProxy):
    def __init__(
        self,
        reader: pa.RecordBatchReader,
        execution: Execution,
        lifetime: _Lifetime | None = None,
    ) -> None:
        super().__init__(reader)
        self._self_lifetime = lifetime if lifetime is not None else _Lifetime(execution)

    def __iter__(self) -> SyncReader:
        return self

    def __next__(self) -> pa.RecordBatch:
        return self.read_next_batch()

    def _read(self, method: str, *, metadata: bool = False) -> Any:
        execution = self._self_lifetime.execution
        try:
            with execution.activate():
                result = getattr(self.__wrapped__, method)()
            execution.count((result[0] if metadata else result).num_rows)
            return result
        except StopIteration:
            execution.finish(complete=True)
            raise
        except BaseException as error:
            execution.finish(error=error)
            raise

    def read_next_batch(self) -> pa.RecordBatch:
        return self._read("read_next_batch")

    def read_next_batch_with_custom_metadata(self) -> Any:
        return self._read("read_next_batch_with_custom_metadata", metadata=True)

    def iter_batches_with_custom_metadata(self) -> Iterator[Any]:
        while True:
            try:
                yield self.read_next_batch_with_custom_metadata()
            except StopIteration:
                return

    def read_all(self) -> pa.Table:
        import pyarrow as pa

        return pa.Table.from_batches(list(self), schema=self.schema)

    def read_pandas(self, **kwargs: Any) -> Any:
        return self.read_all().to_pandas(**kwargs)

    def cast(self, target_schema: pa.Schema) -> SyncReader:
        return SyncReader(
            self.__wrapped__.cast(target_schema),
            self._self_lifetime.execution,
            self._self_lifetime,
        )

    def _bridge(self) -> pa.RecordBatchReader:
        import pyarrow as pa

        # C consumers would otherwise bypass __next__ and leave the span open.
        return pa.RecordBatchReader.from_batches(self.schema, self)

    def __arrow_c_stream__(self, requested_schema: Any = None) -> Any:
        return self._bridge().__arrow_c_stream__(requested_schema)

    def _export_to_c(self, out_ptr: int) -> None:
        return self._bridge()._export_to_c(out_ptr)

    def close(self) -> None:
        execution = self._self_lifetime.execution
        try:
            with execution.activate():
                return self.__wrapped__.close()
        except BaseException as error:
            execution.finish(error=error)
            raise
        finally:
            execution.finish()

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool:
        self.close()
        return False


class AsyncReader(ObjectProxy):
    def __init__(self, reader: AsyncRecordBatchReader, execution: Execution) -> None:
        super().__init__(reader)
        self._self_lifetime = _Lifetime(execution)

    def __aiter__(self) -> AsyncReader:
        return self

    async def __anext__(self) -> pa.RecordBatch:
        execution = self._self_lifetime.execution
        try:
            with execution.activate():
                batch = await self.__wrapped__.__anext__()
            execution.count(batch.num_rows)
            return batch
        except StopAsyncIteration:
            execution.finish(complete=True)
            raise
        except BaseException as error:
            execution.finish(error=error)
            raise

    async def read_all(self) -> list[pa.RecordBatch]:
        # LanceDB's async reader returns a list of RecordBatches, not a Table.
        return [batch async for batch in self]

    async def aclose(self) -> None:
        execution = self._self_lifetime.execution
        try:
            close = getattr(self.__wrapped__, "aclose", None)
            if close is None:
                close = getattr(
                    getattr(self.__wrapped__, "_inner", None), "aclose", None
                )
            if close is not None:
                with execution.activate():
                    await close()
        except BaseException as error:
            execution.finish(error=error)
            raise
        finally:
            execution.finish()
