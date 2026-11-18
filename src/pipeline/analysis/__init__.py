"""Image analysis back-ends.

MVP: analysis is performed MANUALLY by a person in ChatGPT (prepare-batch -> upload -> import-results).
Future: an automated Vision-API analyzer can implement `ImageAnalyzer` and write records in exactly
the JSON format that `import-results` already validates, so nothing downstream changes.
"""
from __future__ import annotations

from pathlib import Path
from typing import Protocol


class ImageAnalyzer(Protocol):
    def analyze(self, batch_dir: Path) -> Path:
        """Analyze all images in a prepared batch folder; return path of a results JSON file
        in the import-results format ({"batch_id": ..., "records": [...]})."""
        ...


class VisionAPIAnalyzer:
    """Placeholder for a future automated processor. Intentionally not implemented in the MVP."""

    def analyze(self, batch_dir: Path) -> Path:  # pragma: no cover
        raise NotImplementedError("Automated Vision API processing is a future module; not part of the MVP.")
