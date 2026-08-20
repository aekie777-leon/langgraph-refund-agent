"""Offline, reproducible evaluation harness for portfolio evidence."""

from agent.evals.dataset import load_evaluation_manifest
from agent.evals.runner import EvaluationAdapters, run_evaluation

__all__ = ["EvaluationAdapters", "load_evaluation_manifest", "run_evaluation"]
