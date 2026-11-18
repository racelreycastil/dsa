"""Enemy: initial static-blocker data. Combat is resolved by the runtime."""
from src.api import EnemyDefinition, Position


class Enemy:
    """Expose read-only identity, position, and nonnegative encounter penalty."""

    __slots__ = ("_enemy_id", "_position", "_encounter_penalty")

    def __init__(self, enemy_id: str, position: Position, encounter_penalty: int) -> None:
        checked = EnemyDefinition(enemy_id, position, encounter_penalty)
        self._enemy_id = checked.enemy_id
        self._position = checked.position
        self._encounter_penalty = checked.encounter_penalty

    @property
    def enemy_id(self) -> str:
        return self._enemy_id

    @property
    def position(self) -> Position:
        return self._position

    @property
    def encounter_penalty(self) -> int:
        return self._encounter_penalty
