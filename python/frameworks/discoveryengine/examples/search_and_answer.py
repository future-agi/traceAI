"""Trace one Discovery Engine search and one grounded answer.

Set DISCOVERY_ENGINE_SERVING_CONFIG to a serving config resource name:

    projects/PROJECT/locations/LOCATION/collections/default_collection/engines/ENGINE/servingConfigs/default_search

The Google clients read Application Default Credentials (ADC) themselves:
run ``gcloud auth application-default login`` or set
GOOGLE_APPLICATION_CREDENTIALS. register() reads FI_API_KEY, FI_SECRET_KEY
and FI_BASE_URL. The Future AGI project is not your GCP project.
"""

import os
from typing import Any, Dict, Optional

from fi_instrumentation import register
from fi_instrumentation.fi_types import ProjectType
from google.cloud import discoveryengine_v1 as discoveryengine

from traceai_discoveryengine import DiscoveryEngineInstrumentor

QUERY = "open telemetry retrieval"
QUESTION = "What does OpenTelemetry trace?"


def client_options(serving_config: str) -> Dict[str, Any]:
    """A regional engine (``locations/eu`` or ``locations/us``) needs its regional endpoint."""
    location = serving_config.split("/locations/", 1)[-1].split("/", 1)[0]
    if location and location != "global" and "/locations/" in serving_config:
        return {"api_endpoint": "{0}-discoveryengine.googleapis.com".format(location)}
    return {}


def main(search_client: Optional[Any] = None, answer_client: Optional[Any] = None) -> None:
    tracer_provider = register(
        project_name="discoveryengine-example",
        project_type=ProjectType.OBSERVE,
        batch=False,
        verbose=False,
    )
    DiscoveryEngineInstrumentor().instrument(tracer_provider=tracer_provider)

    serving_config = os.environ["DISCOVERY_ENGINE_SERVING_CONFIG"]
    options = client_options(serving_config)
    search_client = search_client or discoveryengine.SearchServiceClient(client_options=options)
    answer_client = answer_client or discoveryengine.ConversationalSearchServiceClient(
        client_options=options
    )

    pager = search_client.search(
        request={"serving_config": serving_config, "query": QUERY, "page_size": 5}
    )
    print("{0} results on the first page".format(len(pager.results)))

    response = answer_client.answer_query(
        request={"serving_config": serving_config, "query": {"text": QUESTION}}
    )
    print("answer state: {0}".format(response.answer.state.name))
    tracer_provider.force_flush()


if __name__ == "__main__":
    main()
