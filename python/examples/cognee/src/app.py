"""Trace native Cognee operations using the maintained adapter."""
import asyncio
import os
import sys
from traceai_cognee import configure

DOCUMENT = 'Ada works on the Lighthouse project.'

def init_tracing():
    return configure(project_name=os.environ['FI_PROJECT_NAME'])

async def remember_and_ask(question, document):
    import cognee
    await cognee.add(document)
    await cognee.cognify()
    return await cognee.search(question, query_type=cognee.SearchType.GRAPH_COMPLETION)

def main(argv):
    handle = init_tracing()
    try:
        question = argv[1] if len(argv) > 1 else 'Who works on Lighthouse?'
        document = argv[2] if len(argv) > 2 else DOCUMENT
        with handle.provider.get_tracer(__name__).start_as_current_span('remember_and_ask'):
            results = asyncio.run(remember_and_ask(question, document))
        for result in results:
            print(result)
        handle.force_flush()
    finally:
        handle.shutdown()
    return 0

if __name__ == '__main__':
    sys.exit(main(sys.argv))
