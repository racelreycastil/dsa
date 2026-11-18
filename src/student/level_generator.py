"""LevelGenerator: assemble a complete, valid initial world around injected terrain.

The generator never builds its own floor. It asks the injected terrain provider
for geometry using the seeds the host allows (seed, seed + 1, ... within the
attempt budget), then checks that terrain against every constraint of the
LevelSpecification before choosing:

  * two distinct endpoints whose shortest route length is in range (and which are
    the two leaves for path topologies, leaves for trees),
  * an alternate start-to-exit path when the specification demands one,
  * enough junction tiles / few enough dead ends,
  * artifacts and enemies with in-range values and penalties, placed on floor,
    never on the start with an enemy, honouring the endpoint and overlap rules,
    and still completable within the turn cap.

The result is an initial world only: no route and no score. All choices come from
(specification, seed, terrain), so the same inputs always give the same level.
"""
from collections import deque
import heapq

from src.api import (
    ArtifactDefinition, CharacterDefinition, EnemyDefinition, GenerationFailure,
    LevelDefinition, LevelSpecification, Position, TerrainGeneratorContract,
    TerrainTopology,
)

_MASK = (1 << 64) - 1
_ORTHOGONAL = ((-1, 0), (1, 0), (0, -1), (0, 1))
_PLACEMENT_TRIES = 12


class _Rng:
    """SplitMix64: small, explicit and identical on every platform."""

    def __init__(self, *parts: int) -> None:
        state = 0xCBF29CE484222325
        for part in parts:
            state = ((state ^ (part & _MASK)) * 0x100000001B3) & _MASK
        self._state = state

    def next(self) -> int:
        self._state = (self._state + 0x9E3779B97F4A7C15) & _MASK
        z = self._state
        z = ((z ^ (z >> 30)) * 0xBF58476D1CE4E5B9) & _MASK
        z = ((z ^ (z >> 27)) * 0x94D049BB133111EB) & _MASK
        return z ^ (z >> 31)

    def below(self, bound: int) -> int:
        return self.next() % bound

    def between(self, low: int, high: int) -> int:
        return low + self.below(high - low + 1)

    def shuffle(self, items: list) -> None:
        for i in range(len(items) - 1, 0, -1):
            j = self.below(i + 1)
            items[i], items[j] = items[j], items[i]


class LevelGenerator:
    """Use the supplied TerrainGeneratorContract; do not instantiate a replacement."""

    def __init__(self, terrain_generator: TerrainGeneratorContract) -> None:
        self._terrain_generator = terrain_generator  # retained, never replaced

    def generate_level(self, specification: LevelSpecification, seed: int) -> LevelDefinition:
        if type(specification) is not LevelSpecification:
            raise GenerationFailure("LevelGenerator", "specification must be a LevelSpecification")
        terrain_spec = specification.terrain_spec
        for attempt in range(specification.max_attempts):
            # Consecutive seeds starting at the original seed are the permitted pattern.
            terrain = self._terrain_generator.generate_terrain(terrain_spec, seed + attempt)
            level = self._assemble(specification, seed, attempt, terrain)
            if level is not None:
                return level
        raise GenerationFailure("LevelGenerator", "no acceptable level within the attempt budget")

    # --------------------------------------------------------------- assembly
    def _assemble(self, spec, seed, attempt, terrain):
        floor = [(r, c) for r, line in enumerate(terrain.terrain_rows)
                 for c, ch in enumerate(line) if ch == "."]
        if len(floor) < 2:
            return None
        floor_set = set(floor)
        graph = {cell: [(cell[0] + dr, cell[1] + dc) for dr, dc in _ORTHOGONAL
                        if (cell[0] + dr, cell[1] + dc) in floor_set] for cell in floor}
        degree = {cell: len(neighbors) for cell, neighbors in graph.items()}
        if sum(d >= 3 for d in degree.values()) < spec.min_branch_tiles:
            return None
        if sum(d == 1 for d in degree.values()) > spec.max_dead_end_tiles:
            return None

        rng = _Rng(seed, attempt, len(floor), spec.max_turns)
        endpoints = self._endpoints(spec, rng, floor, graph, degree)
        if endpoints is None:
            return None
        start, goal = endpoints

        for _ in range(_PLACEMENT_TRIES):
            artifacts, enemies = self._place(spec, rng, floor, start, goal)
            if self._completable(graph, start, goal, [e[0] for e in enemies], spec.max_turns):
                return LevelDefinition(
                    level_id=spec.level_id, profile_id=spec.profile_id, seed=seed,
                    terrain=terrain, start=Position(*start), exit=Position(*goal),
                    character=CharacterDefinition(spec.agent_id),
                    artifacts=tuple(ArtifactDefinition(f"artifact-{i}", Position(*cell), value)
                                    for i, (cell, value) in enumerate(artifacts)),
                    enemies=tuple(EnemyDefinition(f"enemy-{i}", Position(*cell), penalty)
                                  for i, (cell, penalty) in enumerate(enemies)))
        return None

    # -------------------------------------------------------------- endpoints
    def _endpoints(self, spec, rng, floor, graph, degree):
        topology = spec.terrain_spec.topology
        must_be_leaves = topology in (TerrainTopology.STRAIGHT_PATH, TerrainTopology.BENT_PATH)
        prefer_leaves = topology is TerrainTopology.BRANCHING_TREE
        bridges = self._bridges(graph, floor[0]) if spec.require_alternate_path else None
        # Leaves first for trees; if that finds nothing, any tiles are acceptable.
        for leaves_only in ((True, False) if prefer_leaves else (must_be_leaves,)):
            starts = [c for c in floor if not leaves_only or degree[c] == 1]
            rng.shuffle(starts)
            for start in starts:
                distance, parent = self._breadth_first(graph, start)
                ends = []
                for cell in floor:
                    if (cell == start or cell not in distance
                            or not spec.min_route_moves <= distance[cell] <= spec.max_route_moves):
                        continue
                    if leaves_only and degree[cell] != 1:
                        continue
                    if bridges is not None and not self._has_alternate(cell, start, parent, bridges):
                        continue
                    ends.append(cell)
                if ends:
                    return start, ends[rng.below(len(ends))]
        return None

    @staticmethod
    def _breadth_first(graph, source):
        distance, parent = {source: 0}, {source: None}
        queue = deque([source])
        while queue:
            cell = queue.popleft()
            for neighbor in graph[cell]:
                if neighbor not in distance:
                    distance[neighbor] = distance[cell] + 1
                    parent[neighbor] = cell
                    queue.append(neighbor)
        return distance, parent

    @staticmethod
    def _bridges(graph, root):
        """Edges whose removal disconnects the floor (iterative lowpoint search)."""
        order, low, parent, bridges = {root: 0}, {root: 0}, {root: None}, set()
        stack = [(root, iter(graph[root]))]
        while stack:
            cell, neighbors = stack[-1]
            nxt = next(neighbors, None)
            if nxt is not None:
                if nxt == parent[cell]:
                    continue
                if nxt in order:
                    low[cell] = min(low[cell], order[nxt])
                else:
                    parent[nxt] = cell
                    order[nxt] = low[nxt] = len(order)
                    stack.append((nxt, iter(graph[nxt])))
            else:
                stack.pop()
                above = parent[cell]
                if above is not None:
                    if low[cell] > order[above]:
                        bridges.add((above, cell))
                        bridges.add((cell, above))
                    low[above] = min(low[above], low[cell])
        return bridges

    @staticmethod
    def _has_alternate(goal, start, parent, bridges):
        """A second simple route exists iff some edge on one route is not a bridge."""
        cell = goal
        while cell != start:
            if (parent[cell], cell) not in bridges:
                return True
            cell = parent[cell]
        return False

    # -------------------------------------------------------------- placement
    @staticmethod
    def _place(spec, rng, floor, start, goal):
        # Prefer ordinary tiles; endpoints are used only if permitted and needed.
        ordinary = [c for c in floor if c != start and c != goal]
        spill = [] if not spec.allow_entities_on_endpoints else [goal, start]
        used = set()

        def draw(count, pool, spill_pool):
            chosen = []
            fresh = [c for c in pool if c not in used]
            rng.shuffle(fresh)
            extra = [c for c in spill_pool if c not in used and c not in fresh]
            for cell in fresh + extra:
                if len(chosen) == count:
                    break
                chosen.append(cell)
                used.add(cell)
            while len(chosen) < count:  # only reachable when overlap is permitted
                chosen.append((pool + spill_pool)[rng.below(len(pool + spill_pool))])
            return chosen

        # An enemy may never start on the explorer's tile.
        enemy_cells = draw(spec.enemy_count, ordinary, [c for c in spill if c != start])
        artifact_cells = draw(spec.artifact_count, ordinary, spill)
        enemies = [(cell, rng.between(spec.enemy_penalty_min, spec.enemy_penalty_max))
                   for cell in enemy_cells]
        artifacts = [(cell, rng.between(spec.artifact_value_min, spec.artifact_value_max))
                     for cell in artifact_cells]
        return artifacts, enemies

    @staticmethod
    def _completable(graph, start, goal, enemy_cells, max_turns):
        """Cheapest turn count to finish, if every enemy tile costs one ENGAGE extra."""
        extra = {}
        for cell in enemy_cells:
            extra[cell] = extra.get(cell, 0) + 1
        best = {start: 0}
        heap = [(0, start)]
        while heap:
            turns, cell = heapq.heappop(heap)
            if cell == goal:
                return turns + 1 <= max_turns  # the final EXIT is one more action
            if turns > best.get(cell, turns):
                continue
            for neighbor in graph[cell]:
                step = turns + 1 + extra.get(neighbor, 0)
                if step < best.get(neighbor, step + 1):
                    best[neighbor] = step
                    heapq.heappush(heap, (step, neighbor))
        return False
