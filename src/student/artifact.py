"""Artifact: initial reward data. The runtime, not the artifact, awards points."""
from src.api import ArtifactDefinition, Position

#hello this is daniel

class Artifact:
    """Expose read-only canonical identity, position, and nonnegative value."""

    __slots__ = ("_artifact_id", "_position", "_value")

    def __init__(self, artifact_id: str, position: Position, value: int) -> None:
        # The public definition enforces: trimmed nonempty str id, exact Position,
        # exact int value >= 0 (no bool, no float). It also detaches the Position.
        checked = ArtifactDefinition(artifact_id, position, value)
        self._artifact_id = checked.artifact_id
        self._position = checked.position
        self._value = checked.value

    @property
    def artifact_id(self) -> str:
        return self._artifact_id

    @property
    def position(self) -> Position:
        return self._position

    @property
    def value(self) -> int:
        return self._value
