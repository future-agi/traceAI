"""Test fixture: ``src/app.py`` after ``mlflow.tracing.set_destination``.

MLflow documents that a destination set with this call takes precedence over
the OTLP environment variables. The contract test runs this with the
recipe's environment to show what that means for the export. Not part of the
recipe.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import mlflow  # noqa: E402
from mlflow.entities.trace_location import MlflowExperimentLocation  # noqa: E402

from app import main  # noqa: E402

# Experiment 0 is the "Default" experiment MLflow creates in a new store.
mlflow.tracing.set_destination(MlflowExperimentLocation(experiment_id="0"))
sys.exit(main(sys.argv))
