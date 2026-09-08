import asyncio
from semantic_kernel import Kernel
from traceai_semantic_kernel import SemanticKernelInstrumentor

SemanticKernelInstrumentor().instrument()

async def main():
    kernel = Kernel()
    print("Semantic Kernel initialized and instrumented!")

if __name__ == "__main__":
    asyncio.run(main())
