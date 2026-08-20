"""Load and fingerprint the packaged synthetic evaluation manifest."""

import hashlib
import json
from importlib import resources
from pathlib import Path

from agent.evals.models import EvaluationManifest


def load_evaluation_manifest(path: str | Path | None = None) -> EvaluationManifest:
    """Load the strict manifest from a supplied path or packaged data."""
    if path is None:
        raw = (
            resources.files("agent")
            .joinpath("data", "eval_scenarios.json")
            .read_text(encoding="utf-8")
        )
    else:
        raw = Path(path).read_text(encoding="utf-8")
    return EvaluationManifest.model_validate_json(raw)


def evaluation_manifest_digest(manifest: EvaluationManifest) -> str:
    """Return a stable digest over the validated dataset and gate contract."""
    canonical = json.dumps(
        manifest.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(canonical).hexdigest()
