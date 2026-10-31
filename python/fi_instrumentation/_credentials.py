"""FI authentication pairs are source-atomic; never log their values."""

import logging

logger = logging.getLogger(__name__)


def credential_pair(values, source):
    api, secret = values.get("api_key"), values.get("secret_key")
    if api and secret:
        return str(api), str(secret)
    if api or secret:
        logger.warning("Ignoring incomplete FI credential pair from %s: missing %s",
                       source, "FI_SECRET_KEY" if api else "FI_API_KEY")
    return None


def merge_headers(headers, defaults):
    """Preserve explicit complete pairs; discard a partial pair as a whole."""
    result = dict(headers)
    pair = credential_pair({"api_key": result.pop("x-api-key", None),
                            "secret_key": result.pop("x-secret-key", None)}, "exporter headers")
    if pair is None and "authorization" not in result:
        defaults = defaults() if callable(defaults) else defaults
        pair = credential_pair({"api_key": defaults.get("x-api-key"),
                                "secret_key": defaults.get("x-secret-key")}, "resolved configuration")
    if pair is not None:
        result.update({"x-api-key": pair[0], "x-secret-key": pair[1]})
    return result
