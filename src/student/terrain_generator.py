"""TerrainGenerator: deterministic floor geometry from a specification and a seed.

Supports every public topology:
  STRAIGHT_PATH   a single row or column of open tiles
  BENT_PATH       a simple path (no side contacts) that turns at least once
  BRANCHING_TREE  a connected, cycle-free layout with at least one junction
  GENERAL         any connected layout with the permitted number of open tiles

The generator keeps no state between calls, so identical (specification, seed)
inputs agree across calls and across fresh instances. Randomness comes from a
tiny explicit generator, not from the `random` module or the clock.
"""
from src.api import GenerationFailure, Terrain, TerrainGenerationSpec, TerrainTopology

_MASK = (1 << 64) - 1
_ORTHOGONAL = ((-1, 0), (1, 0), (0, -1), (0, 1))
_TOPOLOGY_CODE = {TerrainTopology.GENERAL: 0, TerrainTopology.STRAIGHT_PATH: 1,
                  TerrainTopology.BENT_PATH: 2, TerrainTopology.BRANCHING_TREE: 3}


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

    def shuffle(self, items: list) -> None:
        for i in range(len(items) - 1, 0, -1):
            j = self.below(i + 1)
            items[i], items[j] = items[j], items[i]


class TerrainGenerator:
    """Keep state local; identical inputs must also agree across fresh instances."""

    def __init__(self) -> None:
        pass  # deliberately stateless

    def generate_terrain(self, specification: TerrainGenerationSpec, seed: int) -> Terrain:
        if type(specification) is not TerrainGenerationSpec:
            raise GenerationFailure("TerrainGenerator", "specification must be a TerrainGenerationSpec")
        if type(seed) is not int or seed < 0:
            raise GenerationFailure("TerrainGenerator", "seed must be a nonnegative int")
        rows, columns = specification.rows, specification.columns
        low, high = specification.min_open_tiles, specification.max_open_tiles
        topology = specification.topology
        rng = _Rng(seed, rows, columns, low, high, _TOPOLOGY_CODE[topology])

        if topology is TerrainTopology.STRAIGHT_PATH:
            low, high = max(low, 2), min(high, max(rows, columns))
            build = self._straight
        elif topology is TerrainTopology.BENT_PATH:
            low = max(low, 3)
            build = self._bent
        elif topology is TerrainTopology.BRANCHING_TREE:
            low = max(low, 4)
            build = self._tree
        else:
            build = self._general
        if low > high:
            raise GenerationFailure("TerrainGenerator", "no tile count satisfies this topology")

        # Try the randomly preferred size first, then every other permitted size.
        preferred = low + rng.below(high - low + 1)
        sizes = [preferred] + [n for n in range(high, low - 1, -1) if n != preferred]
        for count in sizes:
            cells = build(rows, columns, count, rng)
            if cells is not None:
                open_cells = set(cells)
                return Terrain(tuple(
                    "".join("." if (r, c) in open_cells else "#" for c in range(columns))
                    for r in range(rows)))
        raise GenerationFailure("TerrainGenerator", "no layout found for this specification")

    # --------------------------------------------------------------- builders
    @staticmethod
    def _straight(rows, columns, count, rng):
        options = []
        if count <= columns:
            options.append("row")
        if count <= rows:
            options.append("column")
        if not options:
            return None
        if options[rng.below(len(options))] == "row":
            r, c0 = rng.below(rows), rng.below(columns - count + 1)
            return [(r, c0 + i) for i in range(count)]
        c, r0 = rng.below(columns), rng.below(rows - count + 1)
        return [(r0 + i, c) for i in range(count)]

    @staticmethod
    def _collinear(cells):
        return len({r for r, _ in cells}) == 1 or len({c for _, c in cells}) == 1

    def _bent(self, rows, columns, count, rng):
        if rows < 2 or columns < 2:
            return None
        # 1. random self-avoiding walk that never touches itself sideways
        for _ in range(80):
            tail = (rng.below(rows), rng.below(columns))
            path, taken = [tail], {tail}
            while len(path) < count:
                options = []
                for dr, dc in _ORTHOGONAL:
                    cell = (tail[0] + dr, tail[1] + dc)
                    if 0 <= cell[0] < rows and 0 <= cell[1] < columns and cell not in taken:
                        touching = sum((cell[0] + a, cell[1] + b) in taken for a, b in _ORTHOGONAL)
                        if touching == 1:
                            options.append(cell)
                if not options:
                    break
                tail = options[rng.below(len(options))]
                path.append(tail)
                taken.add(tail)
            if len(path) == count and not self._collinear(path):
                return path
        # 2. an L shape: a tiles across the top row, then b tiles down the side
        shapes = [(a, count + 1 - a) for a in range(2, columns + 1)
                  if 2 <= count + 1 - a <= rows]
        if shapes:
            a, b = shapes[rng.below(len(shapes))]
            return [(0, c) for c in range(a)] + [(r, a - 1) for r in range(1, b)]
        # 3. a serpentine, truncated to the requested length
        for horizontal in (True, False):
            snake = self._serpentine(rows, columns, horizontal)
            if len(snake) >= count and not self._collinear(snake[:count]):
                return snake[:count]
        return None

    @staticmethod
    def _serpentine(rows, columns, horizontal):
        lanes, length = (rows, columns) if horizontal else (columns, rows)
        order = []
        for lane in range(0, lanes, 2):
            run = range(length) if (lane // 2) % 2 == 0 else range(length - 1, -1, -1)
            order.extend((lane, i) for i in run)
            if lane + 2 < lanes:
                order.append((lane + 1, run[-1]))
        return order if horizontal else [(b, a) for a, b in order]

    def _tree(self, rows, columns, count, rng):
        can_branch = (rows >= 2 and columns >= 3) or (rows >= 3 and columns >= 2)
        if not can_branch:
            return None
        # 1. random growth where each new tile touches exactly one existing tile,
        #    which can never close a cycle
        for _ in range(120):
            first = (rng.below(rows), rng.below(columns))
            order, taken = [first], {first}
            while len(order) < count:
                options = []
                for r, c in order:
                    for dr, dc in _ORTHOGONAL:
                        cell = (r + dr, c + dc)
                        if 0 <= cell[0] < rows and 0 <= cell[1] < columns and cell not in taken:
                            touching = sum((cell[0] + a, cell[1] + b) in taken for a, b in _ORTHOGONAL)
                            if touching == 1 and cell not in options:
                                options.append(cell)
                if not options:
                    break
                cell = options[rng.below(len(options))]
                order.append(cell)
                taken.add(cell)
            if len(order) == count and self._has_junction(taken):
                return order
        # 2. a comb (spine plus teeth), visited breadth-first from a junction
        for transpose in (False, True):
            span, depth = (rows, columns) if transpose else (columns, rows)
            if span < 3 or depth < 2:
                continue
            comb = {(0, i) for i in range(span)} | {(d, i) for i in range(1, span, 2) for d in range(depth)}
            if len(comb) < count:
                continue
            seen, queue, order = {(0, 1)}, [(0, 1)], []
            while queue:
                cell = queue.pop(0)
                order.append(cell)
                for dr, dc in _ORTHOGONAL:
                    nxt = (cell[0] + dr, cell[1] + dc)
                    if nxt in comb and nxt not in seen:
                        seen.add(nxt)
                        queue.append(nxt)
            chosen = order[:count]
            return [(b, a) for a, b in chosen] if transpose else chosen
        return None

    @staticmethod
    def _has_junction(cells):
        return any(sum((r + a, c + b) in cells for a, b in _ORTHOGONAL) >= 3 for r, c in cells)

    @staticmethod
    def _general(rows, columns, count, rng):
        if count == 0:
            return []
        first = (rng.below(rows), rng.below(columns))
        taken, frontier = {first}, set()

        def add_neighbors(cell):
            for dr, dc in _ORTHOGONAL:
                nxt = (cell[0] + dr, cell[1] + dc)
                if 0 <= nxt[0] < rows and 0 <= nxt[1] < columns and nxt not in taken:
                    frontier.add(nxt)

        add_neighbors(first)
        while len(taken) < count:
            if not frontier:
                return None
            options = sorted(frontier)
            cell = options[rng.below(len(options))]
            frontier.discard(cell)
            taken.add(cell)
            add_neighbors(cell)
        return sorted(taken)
