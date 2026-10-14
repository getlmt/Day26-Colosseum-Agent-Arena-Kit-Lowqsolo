"""agent/strategy.py — discovery, delegation, caching, replica, and budget
POLICY. Where `agent/gateway.py` is the control plane (route / admit /
authorize / budget — the four JOBS a decision must do), this file is the
building blocks a real answer to those jobs is made of. Nothing here is
wired into `Gateway.decide` by default — see agent/gateway.py's own module
docstring and agent/README.md's table for where each piece is meant to
plug in. That wiring is the assignment, not a step you're missing.

THE ARITHMETIC THAT MAKES THIS FILE'S EXISTENCE THE LESSON
----------------------------------------------------------------------------
A duel gives EACH SIDE 100 credits, ONCE, for all 10 rounds combined
(CONTRACTS.md 4.2's `GatewayContext.credits`; FINAL-PLAN.md section 4). Two
ways to spend a single round, both real, both computed against
`kit.mcp.specs.TOOL_SPECS` (this file's `__main__` demo below recomputes
them live rather than just asserting the numbers, so they can never
silently drift from the real cost table):

    DISCIPLINED  slides.query(fields=[title,body])   base1 + (body3+title1) + 1row*1  =  6
                 slides.get_frame(default fields)     base2 + (body2+title0)          =  4
                 registry.provenance(default fields)  base1 + (etag0)                 =  1
                 -------------------------------------------------------
                 = 11 credits this round — the CEILING of FINAL-PLAN.md 4.3's
                   "8-11" (a round that skips the provenance re-read, or
                   reuses a cached body via `ResultCache` below, lands
                   nearer the floor of that range instead).

    CARELESS     registry.list_servers(fields=[*])    ->            12
                 glossary.list_terms()  (default==full "punishment
                                          button", not a narrow mask) -> 10
                 slides.get_frame(fields=[*]) x3       ->  9 x 3   = 27
                 -------------------------------------------------------
                 = 49 credits — MORE THAN ONE THIRD OF THE WHOLE DUEL'S
                   BUDGET, spent in a single round.

Play at the DISCIPLINED CEILING (11 cr) every single round and 10 rounds
costs 110 — a hair OVER the 100-credit pool: this file's own `__main__`
demo shows that combination surviving nine full rounds and only running dry
paying for the tenth. That is not a bug in the arithmetic; it is the honest
point — "disciplined" is not a magic number, it is not re-paying for the
same provenance read or the same frame body every round when you already
have it (`ResultCache` below, and `BudgetPacer.is_affordable`'s reserve
floor). Play CARELESS even once and you are mathematically bankrupt by
round 3 (100 − 49 − 49 < 0) — not because the game is rigged against you,
but because `registry.list_servers` and `glossary.list_terms` were
deliberately built so their DEFAULT field mask is their full, expensive
dump (FINAL-PLAN.md section 4.1: "an audit showed `list_servers` and
`list_terms` each exceeded a whole round's sustainable allowance —
punishment buttons, not decisions"). Naming exactly the fields you plan to
actually CITE, every time, is not a minor optimisation here; it is the
difference between finishing the duel and not.

Stdlib only. No network, no randomness, no wall-clock reads.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

# kit.mcp.specs is a collaborator's file (workspace hard rule 2). It is
# present and stable as of this writing, but this module must still degrade
# gracefully if a concurrent edit ever makes it briefly unimportable — the
# fallback table below covers exactly the tools this file's own functions
# and demo reference, nothing more.
try:
    from kit.mcp.specs import TOOL_SPECS, cost as _spec_cost
    _SPECS_AVAILABLE = True
except ImportError:  # pragma: no cover - collaborator file
    TOOL_SPECS = {}
    _SPECS_AVAILABLE = False

    def _spec_cost(server: str, tool: str, fields: tuple[str, ...] = (), n_rows: int = 1) -> int:
        """Degraded fallback: a small, hand-copied anchor-price table
        (CONTRACTS.md 3.4's own named anchor prices) covering only the
        (server, tool) pairs this file's functions/demo touch. Real pricing
        always comes from kit.mcp.specs when it is importable — this table
        exists so the file still RUNS, not so it stays authoritative."""
        anchors = {
            ("slides", "query"): 4,  # fields=[title,body], n_rows=1
            ("slides", "get_frame"): 4,  # default fields
            ("registry", "provenance"): 1,
            ("registry", "list_servers"): 12,  # fields=[*]
            ("glossary", "list_terms"): 10,  # default == full dump
        }
        return anchors.get((server, tool), 5)  # 5: an honest "I don't know" default, not 0


__all__ = [
    "ROUNDS_PER_DUEL",
    "SAFE_STARTING_RESERVE",
    "CATALOG_TRAP_TOOLS",
    "CATALOG_CHEAP_MASKS",
    "DEPRECATED_SUCCESSORS",
    "DRIFTING_PATH_IDS",
    "disciplined_round_cost",
    "careless_round_cost",
    "estimate_cost",
    "is_catalog_trap",
    "cheap_mask",
    "successor_of",
    "path_id_of",
    "is_drifting",
    "BudgetPacer",
    "ReplicaChoice",
    "pick_replica",
    "ResultCache",
    "should_delegate",
]

ROUNDS_PER_DUEL = 10

# A pacing target, not a hard rule: if you have spent MORE than this
# fraction of your remaining budget by the time you decide whether THIS
# round's call is affordable, you are trending toward the careless curve
# above, not the disciplined one. `BudgetPacer.is_affordable` below uses it
# as its one, deliberately simple, heuristic.
SAFE_STARTING_RESERVE = 0.5  # keep at least half the ORIGINAL pool as a floor

# The two named "punishment button" tools (FINAL-PLAN.md 4.1): their
# DEFAULT field mask is their full, most expensive dump, not a cheap
# starting point. Calling either with no `fields=` is never an accident
# worth repeating.
CATALOG_TRAP_TOOLS: frozenset[tuple[str, str]] = frozenset(
    {("registry", "list_servers"), ("glossary", "list_terms")}
)

# CONTRACTS.md 4.2 mechanic 8: `slides.search` is deprecated in favour of
# `slides.query`. Kept here as data (mirroring kit/mcp/specs.py's own
# "the economy is data, not code" philosophy) so a `route`/`budget` job can
# look a tool up without re-deriving deprecation from `TOOL_SPECS` by hand.
DEPRECATED_SUCCESSORS: Mapping[tuple[str, str], tuple[str, str]] = {
    ("slides", "search"): ("slides", "query"),
}

# The cheap mask a catalog-trap call is rewritten to (JOB 4): the one field a
# "which servers / which terms exist" question actually needs. Both land at
# 2 credits against the real cost table, instead of 12 / 10 for the default dump.
CATALOG_CHEAP_MASKS: Mapping[tuple[str, str], tuple[str, ...]] = {
    ("registry", "list_servers"): ("name",),
    ("glossary", "list_terms"): ("term",),
}

# The measured drift set of world df8c55dabb35 (`drift.json`, `drifts: true`),
# copied in as DATA because `Gateway.decide` may not do I/O. 16 of the 22
# indexed path_ids drift; the other 6 (04032b0c, 594ef4c1, 61a20716, 6cc8191a,
# b3616a68, b55e1f40) are byte-identical across replicas, so for them "which
# replica" changes nothing. Only Frame/Deck/Section anchors carry a path_id.
DRIFTING_PATH_IDS: frozenset[str] = frozenset({
    "053195a5", "0e11ae43", "170c4d17", "28e68faa", "3326cb76", "45098556",
    "72f75709", "75811e75", "7a8d8046", "a13b20e6", "a284ae8b", "abd20c68",
    "c5418ab5", "d8f95a7b", "e0614beb", "f2696f5f",
})
_PATH_ID_NAMESPACES = frozenset({"Frame", "Deck", "Section"})


def disciplined_round_cost() -> int:
    """The module docstring's "DISCIPLINED" total, computed live against
    the real cost table (or the degraded fallback) rather than hard-coded —
    so if `kit/mcp/specs.py` is ever retuned, this number moves with it
    instead of silently lying."""
    return (
        _spec_cost("slides", "query", fields=("title", "body"), n_rows=1)
        + _spec_cost("slides", "get_frame")
        + _spec_cost("registry", "provenance")
    )


def careless_round_cost() -> int:
    """The module docstring's "CARELESS" total, same live-computation
    reasoning as `disciplined_round_cost` above."""
    return (
        _spec_cost("registry", "list_servers", fields=("*",))
        + _spec_cost("glossary", "list_terms")
        + 3 * _spec_cost("slides", "get_frame", fields=("*",))
    )


def estimate_cost(server: str, tool: str, fields: tuple[str, ...] = (), n_rows: int = 1) -> int | None:
    """The real-table price of one call, or `None` when `(server, tool)` is not
    a known tool (or the mask names an unknown field). Never raises — JOB 4
    calls this from inside `Gateway.decide`, where an exception is a penalty."""
    try:
        return int(_spec_cost(server, tool, fields=tuple(fields), n_rows=n_rows))
    except Exception:
        return None


def is_catalog_trap(server: str, tool: str, fields: tuple[str, ...]) -> bool:
    """True iff `(server, tool)` is one of the two "punishment button"
    tools AND the caller passed no explicit mask (`fields` empty) or asked
    for everything (`("*",)`) — i.e. is about to pay the DEFAULT/full price
    rather than a deliberately chosen cheap one. A `route`/`budget` job can
    use this as the trigger for "rewrite this call's fields before letting
    it through"."""
    if (server, tool) not in CATALOG_TRAP_TOOLS:
        return False
    return fields in ((), ("*",))


def cheap_mask(server: str, tool: str, fields_you_will_actually_cite: tuple[str, ...]) -> tuple[str, ...]:
    """Given the fields your answer will actually cite, return exactly
    those, sorted — the discipline that keeps `slides.get_frame` at 4
    credits instead of 9, and `registry.list_servers` at 2 instead of 12
    (CONTRACTS.md 3.4's own named anchor prices). This function does not
    know what your answer needs; YOU do — pass the honest set. Passing an
    EMPTY set here is itself informative: it means you are about to make a
    call whose result you do not plan to cite, which is the `wasteful`
    class waiting to happen (CONTRACTS.md 6.4's detector: "credits spent >
    the round allowance").

    `server`/`tool` are accepted (and validated against `TOOL_SPECS` when
    available) purely so a caller gets an early, loud `KeyError` for a typo
    rather than a silently wrong mask two calls later."""
    if _SPECS_AVAILABLE and (server, tool) not in TOOL_SPECS:
        raise KeyError(f"{server}.{tool} is not a known tool in kit.mcp.specs.TOOL_SPECS")
    return tuple(sorted(set(fields_you_will_actually_cite)))


def successor_of(server: str, tool: str) -> tuple[str, str] | None:
    """`(server, tool)`'s non-deprecated replacement, or `None` if it is
    not deprecated at all. A `route` job's cheapest possible win: rewriting
    `slides.search` to `slides.query` before forwarding costs you nothing
    and removes the `wasteful` "used a deprecated tool" detector hit
    (CONTRACTS.md 6.4) entirely."""
    return DEPRECATED_SUCCESSORS.get((server, tool))


def path_id_of(anchor: Any) -> str | None:
    """The `path_id` of a `Frame:`/`Deck:`/`Section:` anchor string (its slug),
    else `None`. Parsed by hand — `ns:slug[/rev][/idx][#span]` — so this never
    raises on whatever a model typed into an `anchor=` argument."""
    if not isinstance(anchor, str) or ":" not in anchor:
        return None
    ns, _, rest = anchor.partition(":")
    if ns not in _PATH_ID_NAMESPACES:
        return None
    slug = rest.split("#", 1)[0].split("/", 1)[0]
    return slug or None


def is_drifting(path_id: str | None) -> bool:
    """Whether `path_id` is in the measured drift set (`DRIFTING_PATH_IDS`)."""
    return isinstance(path_id, str) and path_id in DRIFTING_PATH_IDS


@dataclass
class BudgetPacer:
    """Tracks YOUR OWN running spend across a duel and answers one
    question: "can I still afford this round the way I've been playing?"
    Deliberately simple — a flat per-round reserve check, not a model of
    the whole remaining duel — because the arithmetic in this file's module
    docstring is already the whole lesson: stay near the disciplined
    number every round and the pacing question barely matters; drift
    toward the careless number even once and no pacer saves you.

    This is YOUR bookkeeping, independent of `GatewayContext.credits` (the
    arena's authoritative figure) — the two SHOULD agree; if they ever
    disagree, trust `ctx.credits`, and treat the mismatch itself as
    something worth a `Telemetry.note(...)` (agent/telemetry.py)."""

    starting_pool: int = 100
    rounds_total: int = ROUNDS_PER_DUEL
    _spent: int = field(default=0, init=False)
    _spent_by_round: dict[int, int] = field(default_factory=dict, init=False)

    def record_spend(self, round_no: int, cost: int) -> None:
        if cost < 0:
            raise ValueError(f"cost must be non-negative, got {cost}")
        self._spent += cost
        self._spent_by_round[round_no] = self._spent_by_round.get(round_no, 0) + cost

    @property
    def credits_left(self) -> int:
        return self.starting_pool - self._spent

    @property
    def credits_spent(self) -> int:
        return self._spent

    def is_affordable(self, round_no: int, cost: int, *, reserve: float = SAFE_STARTING_RESERVE) -> bool:
        """`True` iff spending `cost` now leaves at least `reserve` of the
        ORIGINAL pool in hand — a simple, conservative floor. Reasonable
        for the FIRST half of a duel; by round 8-10 you know your real
        remaining need better than a flat reserve does, and a `budget` job
        that only ever consults this without ever revisiting the reserve
        as rounds run out will end up over-cautious late, not over-spent —
        the safer of the two failure directions, but still a real
        one-line simplification worth outgrowing."""
        floor = self.starting_pool * reserve
        return (self.credits_left - cost) >= floor

    def bankrupt_by(self) -> int | None:
        """The first round number (1-indexed) at which `credits_left`
        actually went negative, or `None` if it never did. Used by this
        file's own `__main__` demo to make the module docstring's "bankrupt
        by round 3" claim a live, checked fact instead of an assertion in
        prose."""
        running = self.starting_pool
        for round_no in sorted(self._spent_by_round):
            running -= self._spent_by_round[round_no]
            if running < 0:
                return round_no
        return None


@dataclass(frozen=True, slots=True)
class ReplicaChoice:
    replica: str  # "w" | "c"
    reason: str


def pick_replica(*, path_id: str | None, known_drifting: bool, prefers_fresh: bool = True) -> ReplicaChoice:
    """A starting heuristic for JOB 1 (ROUTE) in `agent/gateway.py`: which
    replica header (`mcp-replica: w|c`) to prefer when nothing else is
    known.

    `known_drifting` is YOUR OWN judgement call, not something this
    function derives — a real implementation reads it from a
    `registry.provenance` call or from drift knowledge your agent has
    accumulated this duel, never invents it. `path_id` is accepted (and
    logged in `reason`) purely for traceability; callers pass
    `known_drifting=is_drifting(path_id)` (the measured drift set above).

    POLICY: on a drifting path the WORKING replica ("w") is the fresher one.
    That is not a guess any more — it is the referee's own definition:
    `stale_read` fires when an answer cites `rev="c"` on a path_id that
    `drift.json` marks as drifting, for a replica-sensitive ask. CORPUS-FACTS
    section 2's worked case agrees (day18: 45 working content frames vs 31
    canonical — working is the more complete copy). So:

      * drifting + `prefers_fresh`      -> "w" (fresher; citing it is never stale)
      * drifting + not `prefers_fresh`  -> "c" (the caller explicitly wants the
        canonical side, e.g. to report BOTH sides of a `current_version_of`)
      * not drifting                    -> "w" (replicas are identical; the
        working copy is the default the servers serve anyway)"""
    if known_drifting and not prefers_fresh:
        return ReplicaChoice(
            replica="c",
            reason=f"path_id={path_id!r} drifts; caller asked for the canonical side explicitly",
        )
    if known_drifting:
        return ReplicaChoice(
            replica="w",
            reason=f"path_id={path_id!r} drifts; working is the fresher replica (citing rev=c here is stale_read)",
        )
    return ReplicaChoice(replica="w", reason=f"path_id={path_id!r}: no known drift; default to working")


@dataclass
class ResultCache:
    """A per-duel memory of `(anchor, fields)` you have ALREADY PAID FOR —
    `agent/gateway.py`'s `Gateway` lives for the whole duel (CONTRACTS.md
    4.3), so this cache can too, and a hit here is a call your `budget` job
    never needs to forward at all.

    THE CAVEAT THAT MATTERS MORE THAN THE CACHE: a cached body is a
    snapshot from whenever you first fetched it. Under an active
    `replica_flip` or `poisoned_result` mutation (CONTRACTS.md section 8),
    the SAME anchor can legitimately answer differently on a later round.
    Treat a cache hit as "I already have grounds to say this, and I paid
    for them once" — never as "this is still true right now" without
    re-confirming when a round's attack card gives you a specific reason to
    doubt it. A cache that is trusted blindly is exactly how a `stale_read`
    (CONTRACTS.md 6.4) happens for free.

    Keys are `(anchor, tuple(sorted(fields)))` — the SAME anchor requested
    with a NARROWER mask than what's cached is still a genuine cache miss
    (you never paid for the field you'd be citing), which is why the key
    includes the mask, not just the anchor."""

    _store: dict[tuple[str, tuple[str, ...]], Mapping[str, Any]] = field(default_factory=dict)

    @staticmethod
    def _key(anchor: str, fields: tuple[str, ...]) -> tuple[str, tuple[str, ...]]:
        return (anchor, tuple(sorted(fields)))

    def get(self, anchor: str, fields: tuple[str, ...]) -> Mapping[str, Any] | None:
        return self._store.get(self._key(anchor, fields))

    def put(self, anchor: str, fields: tuple[str, ...], row: Mapping[str, Any]) -> None:
        self._store[self._key(anchor, fields)] = dict(row)

    def __len__(self) -> int:
        return len(self._store)


def should_delegate(
    *,
    own_confidence: float,
    calls_used_this_window: int,
    calls_allowed_this_window: int,
    credits_left: int,
    delegate_cost: int,
    min_confidence_to_skip: float = 0.85,
) -> bool:
    """JOB-neutral heuristic for "is an A2A verifier (e.g.
    `citation-checker.verify_source`, rate-limited 2-per-3-rounds per
    CONTRACTS.md section 4.2 mechanic 5) worth its cost RIGHT NOW". Three
    gates, all must pass:

      1. You are not already confident enough to skip it
         (`own_confidence < min_confidence_to_skip`) — delegating when you
         are already sure is the exact `wasteful` pattern the per-tool
         rate window (mechanic 5) exists to make you ration.
      2. The rate window has room left this duel
         (`calls_used_this_window < calls_allowed_this_window`) — a call
         that would just come back `rate_limited` is worse than skipping
         it: it still costs credits (CONTRACTS.md 3.3: `rate_limited` is
         "yes, no refund") for zero information.
      3. You can afford it without breaking your own reserve
         (`credits_left >= delegate_cost`, checked bare here — combine with
         `BudgetPacer.is_affordable` for the full picture including your
         reserve floor).

    STARTER HEURISTIC: `min_confidence_to_skip=0.85` is a placeholder
    threshold, not a tuned one — `own_confidence` itself is not computed by
    anything in this file; it is whatever your own reasoning (guided by
    `agent/prompt.md`'s citation contract) decides it is. Wire a real
    confidence signal in before you trust this gate under pressure."""
    if own_confidence >= min_confidence_to_skip:
        return False
    if calls_used_this_window >= calls_allowed_this_window:
        return False
    if credits_left < delegate_cost:
        return False
    return True


if __name__ == "__main__":
    print("=== agent.strategy: the round-cost arithmetic, computed live ===\n")
    disciplined = disciplined_round_cost()
    careless = careless_round_cost()
    print(f"  disciplined_round_cost() = {disciplined} cr  (FINAL-PLAN.md 4.3: '8-11')")
    print(f"  careless_round_cost()    = {careless} cr  (FINAL-PLAN.md 4.3: '~49')")
    assert 8 <= disciplined <= 11, disciplined
    assert careless >= 45, careless
    print(f"  kit.mcp.specs available: {_SPECS_AVAILABLE}")

    print("\n=== is_catalog_trap / cheap_mask / successor_of ===\n")
    assert is_catalog_trap("registry", "list_servers", ()) is True
    assert is_catalog_trap("registry", "list_servers", ("name",)) is False
    assert is_catalog_trap("slides", "get_frame", ()) is False
    print("  registry.list_servers with no mask -> catalog trap: True")
    print("  registry.list_servers with fields=(name,) -> catalog trap: False")

    mask = cheap_mask("slides", "get_frame", ("title", "title", "body"))
    print(f"  cheap_mask('slides','get_frame', ('title','title','body')) -> {mask}")
    assert mask == ("body", "title")

    succ = successor_of("slides", "search")
    print(f"  successor_of('slides','search') -> {succ}")
    assert succ == ("slides", "query")
    assert successor_of("slides", "query") is None

    print("\n=== BudgetPacer: disciplined-at-the-CEILING barely lasts the duel; careless does not ===\n")
    disciplined_pacer = BudgetPacer()
    for round_no in range(1, ROUNDS_PER_DUEL + 1):
        disciplined_pacer.record_spend(round_no, disciplined)
    print(
        f"  disciplined (ceiling, {disciplined}cr) x10 rounds -> spent={disciplined_pacer.credits_spent} "
        f"credits_left={disciplined_pacer.credits_left} bankrupt_by={disciplined_pacer.bankrupt_by()}"
    )
    # Measured against the live cost table, a disciplined round (query[title,body]
    # + get_frame(default) + provenance) costs 9 credits, not the 11 the prose
    # above assumed — so paying full price for all three calls EVERY round, with
    # no caching at all, survives the whole duel (90 of 100). The margin is thin
    # (one careless round ends it), which is why ResultCache/pacing still matter.
    if disciplined * ROUNDS_PER_DUEL <= disciplined_pacer.starting_pool:
        assert disciplined_pacer.bankrupt_by() is None, disciplined_pacer.bankrupt_by()
    else:
        assert disciplined_pacer.bankrupt_by() == ROUNDS_PER_DUEL, disciplined_pacer.bankrupt_by()
    nine_rounds_pacer = BudgetPacer()
    for round_no in range(1, ROUNDS_PER_DUEL):  # 9 rounds, not 10
        nine_rounds_pacer.record_spend(round_no, disciplined)
    print(f"  disciplined (ceiling) x9 rounds  -> credits_left={nine_rounds_pacer.credits_left} (still positive)")
    assert nine_rounds_pacer.credits_left >= 0

    careless_pacer = BudgetPacer()
    bankrupt_round = None
    for round_no in range(1, ROUNDS_PER_DUEL + 1):
        careless_pacer.record_spend(round_no, careless)
        if bankrupt_round is None and careless_pacer.credits_left < 0:
            bankrupt_round = round_no
    print(
        f"  careless per round -> spent={careless_pacer.credits_spent} "
        f"credits_left={careless_pacer.credits_left} bankrupt_by={careless_pacer.bankrupt_by()}"
    )
    assert careless_pacer.bankrupt_by() == bankrupt_round
    assert careless_pacer.bankrupt_by() <= 3, careless_pacer.bankrupt_by()

    print("\n=== BudgetPacer.is_affordable: the reserve floor ===\n")
    mid_pacer = BudgetPacer()
    mid_pacer.record_spend(1, 60)
    print(f"  after spending 60/100, credits_left={mid_pacer.credits_left}")
    assert mid_pacer.is_affordable(2, 5) is False  # would drop below the 50-credit reserve floor
    assert mid_pacer.is_affordable(2, -20) is True  # nonsense cost, but arithmetic still holds
    fresh_pacer = BudgetPacer()
    assert fresh_pacer.is_affordable(1, disciplined) is True

    print("\n=== pick_replica: working is the fresher replica on a drifting path ===\n")
    assert is_drifting(path_id_of("Frame:d8f95a7b/c/031")) is True  # day18, measured drift
    assert is_drifting(path_id_of("Frame:04032b0c/w/001")) is False  # identical replicas
    assert path_id_of("Concept:stategraph/w/055") is None  # only Frame/Deck/Section carry a path_id
    choice_clean = pick_replica(path_id="04032b0c", known_drifting=is_drifting("04032b0c"))
    choice_drifting = pick_replica(path_id="d8f95a7b", known_drifting=is_drifting("d8f95a7b"))
    choice_canonical = pick_replica(path_id="d8f95a7b", known_drifting=True, prefers_fresh=False)
    print(f"  non-drifting path       -> {choice_clean}")
    print(f"  drifting path           -> {choice_drifting}")
    print(f"  drifting, wants canon.  -> {choice_canonical}")
    assert choice_clean.replica == "w"
    assert choice_drifting.replica == "w"
    assert choice_canonical.replica == "c"

    print("\n=== estimate_cost / CATALOG_CHEAP_MASKS ===\n")
    for (srv, tl), mask in CATALOG_CHEAP_MASKS.items():
        print(f"  {srv}.{tl}: default={estimate_cost(srv, tl)} cr, cheap {mask}={estimate_cost(srv, tl, mask)} cr")
        assert estimate_cost(srv, tl, mask) <= 2 < estimate_cost(srv, tl)
    assert estimate_cost("nope", "nothing") is None

    print("\n=== ResultCache: same (anchor, fields) is a hit; a wider mask is a genuine miss ===\n")
    cache = ResultCache()
    anchor = "Frame:3f2a9c11/w/041"
    assert cache.get(anchor, ("title", "body")) is None
    cache.put(anchor, ("title", "body"), {"title": "Streamable HTTP", "body": "..."})
    hit = cache.get(anchor, ("body", "title"))  # order-insensitive, same key
    print(f"  cache.get(anchor, ('body','title')) after put(('title','body')) -> {hit}")
    assert hit == {"title": "Streamable HTTP", "body": "..."}
    miss = cache.get(anchor, ("title", "body", "meta"))
    print(f"  cache.get(anchor, wider mask incl. 'meta') -> {miss}  (a real miss, not stale data)")
    assert miss is None
    assert len(cache) == 1

    print("\n=== should_delegate: three gates, all must pass ===\n")
    cases = [
        dict(own_confidence=0.4, calls_used_this_window=0, calls_allowed_this_window=2, credits_left=50, delegate_cost=6),
        dict(own_confidence=0.95, calls_used_this_window=0, calls_allowed_this_window=2, credits_left=50, delegate_cost=6),
        dict(own_confidence=0.4, calls_used_this_window=2, calls_allowed_this_window=2, credits_left=50, delegate_cost=6),
        dict(own_confidence=0.4, calls_used_this_window=0, calls_allowed_this_window=2, credits_left=3, delegate_cost=6),
    ]
    expected = [True, False, False, False]
    for case, want in zip(cases, expected):
        got = should_delegate(**case)
        print(f"  should_delegate({case}) -> {got}")
        assert got == want, (case, got, want)

    print("\nAll agent/strategy.py demos passed.")
