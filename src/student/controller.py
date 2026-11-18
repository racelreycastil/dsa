"""TODO: select one public action from each immutable world observation."""
from src.api import WorldAction, WorldSnapshot


class AgentController:
    """A new controller is constructed for each run; arrival still needs EXIT."""

    def __init__(self) -> None:
        self._plan = []        # [(WorldAction, expected_snapshot_key_or_None), ...]
        self._expected = None  # snapshot key predicted after the last returned action

    # ------------------------------------------------------------------ public
    def choose_action(self, state: WorldSnapshot, agent_id: str) -> WorldAction:
        key = self._observe(state)
        if not self._plan or self._expected is None or key != self._expected:
            self._plan = self._make_plan(state)
        action, expected = self._plan.pop(0)
        self._expected = expected
        return action

    # ---------------------------------------------------------------- planning
    @staticmethod
    def _observe(state):
        position = state.agent_position
        return (position.row, position.column, state.turn,
                tuple(state.collected_artifact_ids), tuple(state.defeated_enemy_ids))

    def _make_plan(self, state):
        plan = self._search(state)
        return plan if plan else self._fallback(state)

    def _search(self, s):
        started = time.monotonic()
        level = s.level
        allowed = set(s.allowed_actions)
        floor = {(r, c) for r, line in enumerate(level.terrain.terrain_rows)
                 for c, ch in enumerate(line) if ch == "."}
        exit_cell = (level.exit.row, level.exit.column)

        patrol_ids = {p.enemy_id for p in level.patrols}
        artifacts = list(level.artifacts)
        enemies = [e for e in level.enemies if e.enemy_id not in patrol_ids]
        artifact_at = {}
        for i, a in enumerate(artifacts):
            artifact_at.setdefault((a.position.row, a.position.column), []).append(i)
        enemy_at = {}
        for i, e in enumerate(enemies):
            enemy_at.setdefault((e.position.row, e.position.column), []).append(i)
        engage_from = {}  # cell -> indexes of static enemies on an adjacent tile
        for i, e in enumerate(enemies):
            for _, dr, dc in _MOVES:
                engage_from.setdefault((e.position.row + dr, e.position.column + dc), []).append(i)

        collected, defeated = set(s.collected_artifact_ids), set(s.defeated_enemy_ids)
        cm0 = sum(1 << i for i, a in enumerate(artifacts) if a.artifact_id in collected)
        dm0 = sum(1 << i for i, e in enumerate(enemies) if e.enemy_id in defeated)

        patrols = [(tuple((p.row, p.column) for p in pt.positions), pt.initial_phase)
                   for pt in level.patrols]
        period = 1
        for cycle, _ in patrols:
            period = period * len(cycle) // gcd(period, len(cycle))
        blocked_cache = {}

        def patrol_cells(t):
            slot = t % period
            if slot not in blocked_cache:
                # Patrol positions depend on accepted actions: phase = initial + turn.
                blocked_cache[slot] = {cycle[(phase + t) % len(cycle)] for cycle, phase in patrols}
            return blocked_cache[slot]

        # Arrival at turn >= min(cap, deadline) ends the run, but EXIT can still
        # be accepted from any earlier turn.
        limit = s.max_turns if s.deadline_turn is None else min(s.max_turns, s.deadline_turn)

        base, rate, cutoff, baseline = (s.base_points, s.deduction_rate,
                                        s.zero_score_excess, s.baseline_cost)
        reward_cache, penalty_cache = {}, {}

        def reward_of(mask):
            if mask not in reward_cache:
                reward_cache[mask] = sum(a.value for i, a in enumerate(artifacts) if mask >> i & 1)
            return reward_cache[mask]

        def penalty_of(mask):
            if mask not in penalty_cache:
                penalty_cache[mask] = sum(e.encounter_penalty for i, e in enumerate(enemies) if mask >> i & 1)
            return penalty_cache[mask]

        def evaluate(cm, dm, turns_with_exit):
            cost = turns_with_exit + penalty_of(dm)
            excess = max(0, cost - baseline)
            score = 0 if excess >= cutoff else max(0, base + reward_of(cm) - rate * excess)
            return score, cost

        t0 = s.turn
        start = (s.agent_position.row, s.agent_position.column, cm0, dm0)
        first_key = start + (t0 % period,)
        info = {first_key: (t0, None, None)}  # key -> (turn, parent key, action)
        queue = deque([start + (t0,)])
        best = None  # (score, -cost, key)
        pops = 0

        def push(r, c, cm, dm, t, parent, action):
            key = (r, c, cm, dm, t % period)
            if key not in info:
                info[key] = (t, parent, action)
                queue.append((r, c, cm, dm, t))

        while queue:
            pops += 1
            if pops % 256 == 0 and time.monotonic() - started > _SEARCH_SECONDS:
                break
            r, c, cm, dm, t = queue.popleft()
            here = (r, c, cm, dm, t % period)
            if (r, c) == exit_cell and t < s.max_turns:
                score, cost = evaluate(cm, dm, t + 1)
                if best is None or (score, -cost) > best[:2]:
                    best = (score, -cost, here)
            nt = t + 1
            if nt >= limit:
                continue
            now_blocked, next_blocked = patrol_cells(t), patrol_cells(nt)
            for action_type, dr, dc in _MOVES:
                if action_type not in allowed:
                    continue
                cell = (r + dr, c + dc)
                if cell not in floor or cell in now_blocked or cell in next_blocked:
                    continue
                if any(not dm >> i & 1 for i in enemy_at.get(cell, ())):
                    continue
                push(cell[0], cell[1], cm, dm, nt, here, WorldAction(action_type))
            if (r, c) in next_blocked:
                continue  # standing still here would meet a patrol after the action
            if WorldActionType.COLLECT in allowed:
                for i in artifact_at.get((r, c), ()):
                    if not cm >> i & 1:
                        push(r, c, cm | 1 << i, dm, nt, here,
                             WorldAction(WorldActionType.COLLECT, target_id=artifacts[i].artifact_id))
            if WorldActionType.ENGAGE in allowed:
                for i in engage_from.get((r, c), ()):
                    if not dm >> i & 1:
                        push(r, c, cm, dm | 1 << i, nt, here,
                             WorldAction(WorldActionType.ENGAGE, target_id=enemies[i].enemy_id))
            if patrols and WorldActionType.WAIT in allowed:
                push(r, c, cm, dm, nt, here, WorldAction.wait())

        if best is None:
            return None
        steps, key = [], best[2]
        while info[key][1] is not None:
            turn, parent, action = info[key]
            steps.append((action, key, turn))
            key = parent
        steps.reverse()
        plan = []
        for action, key, turn in steps:
            r, c, cm, dm, _ = key
            expected = (r, c, turn,
                        tuple(sorted(artifacts[i].artifact_id for i in range(len(artifacts)) if cm >> i & 1)),
                        tuple(sorted(enemies[i].enemy_id for i in range(len(enemies)) if dm >> i & 1)))
            plan.append((action, expected))
        plan.append((WorldAction(WorldActionType.EXIT), None))
        return plan

    # ---------------------------------------------------------------- fallback
    @staticmethod
    def _fallback(s):
        """Used only if no legal complete plan exists: take any safe, useful step."""
        here = (s.agent_position.row, s.agent_position.column)
        exit_cell = (s.exit_position.row, s.exit_position.column)
        if here == exit_cell:
            return [(WorldAction(WorldActionType.EXIT), None)]
        allowed = set(s.allowed_actions)
        distance = {exit_cell: 0}
        frontier = deque([exit_cell])
        while frontier:
            r, c = frontier.popleft()
            for _, dr, dc in _MOVES:
                cell = (r + dr, c + dc)
                if cell not in distance and s.terrain.is_traversable(Position(cell[0], cell[1])):
                    distance[cell] = distance[(r, c)] + 1
                    frontier.append(cell)
        blocked = {(e.position.row, e.position.column) for e in s.level.enemies
                   if e.enemy_id not in set(s.defeated_enemy_ids)}
        moves = []
        for action_type, dr, dc in _MOVES:
            cell = (here[0] + dr, here[1] + dc)
            if action_type in allowed and cell in distance and cell not in blocked:
                moves.append((distance[cell], action_type))
        if moves:
            return [(WorldAction(min(moves, key=lambda m: m[0])[1]), None)]
        if WorldActionType.WAIT in allowed:
            return [(WorldAction.wait(), None)]
        return [(WorldAction(WorldActionType.EXIT), None)]
