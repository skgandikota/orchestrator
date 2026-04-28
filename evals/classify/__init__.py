"""Classifier eval suite.

Provides a golden dataset of intent-labeled prompts and a harness that
scores a classifier callable against it (overall accuracy plus per-intent
precision and recall).

Run from the command line:

.. code-block:: shell

   python -m evals.classify --suite evals/classify/golden.jsonl
"""

from __future__ import annotations

from evals.classify.runner import (
    ClassifierFn,
    ClassifyEvalReport,
    ClassifyEvalRunner,
    GoldenCase,
    IntentMetrics,
    load_golden,
    run_classifier_eval,
)

__all__ = [
    "ClassifierFn",
    "ClassifyEvalReport",
    "ClassifyEvalRunner",
    "GoldenCase",
    "IntentMetrics",
    "load_golden",
    "run_classifier_eval",
]
