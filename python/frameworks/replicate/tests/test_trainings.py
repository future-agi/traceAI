"""Trainings and plain reads are not wrapped (PRD R-09, AC-07)."""

from __future__ import annotations

import pytest

pytest.importorskip("replicate", reason="replicate must be installed to test its instrumentor")

from _support import TEXT_MODEL, TEXT_VERSION, FakeReplicate, instrumented, make_client  # noqa: E402


def test_trainings_create_produces_no_span():
    with FakeReplicate() as fake, instrumented() as traced:
        client = make_client(fake)
        training = client.trainings.create(
            model=TEXT_MODEL, version=TEXT_VERSION, input={}, destination="acme/tuned"
        )

    assert training.status == "starting"
    assert fake.paths("POST") == [
        "/v1/models/acme/text-model/versions/{0}/trainings".format(TEXT_VERSION)
    ]
    assert traced.spans() == []


def test_get_and_reload_produce_no_span():
    with FakeReplicate() as fake:
        prediction_id = make_client(fake).predictions.create(model=TEXT_MODEL, input={}).id
        with instrumented() as traced:
            client = make_client(fake)
            prediction = client.predictions.get(prediction_id)
            prediction.reload()

    assert fake.paths("GET").count("/v1/predictions/" + prediction_id) == 2
    assert traced.spans() == []
