"""Character: the explorer's immutable, canonical public identity."""
from src.api import CharacterDefinition


class Character:
    """Character(agent_id); expose a read-only str agent_id property."""

    __slots__ = ("_agent_id",)

    def __init__(self, agent_id: str) -> None:
        # CharacterDefinition applies the public rule: a nonempty, already-trimmed
        # str. It raises TypeError/ValueError for anything else (bool, None, "  ").
        checked = CharacterDefinition(agent_id)
        self._agent_id = checked.agent_id
        
    @property
    def agent_id(self) -> str:
        return self._agent_id
