"""The route planner: turn a validated relationship intent into an ordered list of joins.

What: plan_route() takes a RelationshipIntent and returns a Plan: which canonical
path was used, whether it is walked backwards, and one Hop per relationship, in
order from the question's source to its target, each with its direction.

How: it only looks up routes written in canonical_paths.json; it never searches
the graph or invents a route.
  1. Pick the route.
       No via:   the default route for (source, target); if that pair has none,
                 the default for (target, source), walked backwards.
       With via: every route for the pair (or the reversed pair) that passes
                 through all the via stereotypes. One left: use it. Several: the
                 default if it is one of them, otherwise the question is ambiguous.
  2. If the route is walked backwards, reverse the order of its steps.
  3. Walk the steps from the source. At each step the current stereotype is either
     the relationship's stored Start (walk forward) or its stored End (walk backward).
  4. Check every filter's and include's stereotype is on the route exactly once.
     Filters and includes never change which route is chosen: only source, target
     and via do.

Why: the joins decide whether an analysis is right, so they come from approved
metadata, never from the LLM. The planner knows nothing about views, columns,
t_connector or SQL text; sql_builder.py turns the Plan into SQL. The direction of
each hop is all the SQL builder needs to pick the Start/End columns.
"""

from __future__ import annotations

from dataclasses import dataclass

from .errors import RouteError
from .intent import RelationshipIntent
from .metadata_loader import CanonicalPath, Metadata, Relationship, Stereotype

FORWARD = "forward"  # walking from the connector's stored Start to its End
BACKWARD = "backward"  # walking from the stored End to the Start


@dataclass(frozen=True)
class Hop:
    """One relationship walked in one direction, e.g. SREQ_DATA from DATA to SREQ (backward)."""
    relationship: Relationship
    from_stereotype: Stereotype
    to_stereotype: Stereotype
    direction: str  # FORWARD or BACKWARD


@dataclass(frozen=True)
class Plan:
    """The planner's output: the route chosen and the hops to join, from the question's source to its target.

    reversed is True when the route is written in canonical_paths.json in the other
    direction (e.g. UC_DATA used for a DATA -> UC question); it is kept for logs and tests.
    """
    path: CanonicalPath
    reversed: bool
    hops: tuple[Hop, ...]

    @property
    def stereotypes(self) -> tuple[Stereotype, ...]:
        """Every stereotype along the route, in walking order, e.g. (DATA, SREQ, USTP, UC)."""
        return (self.hops[0].from_stereotype,) + tuple(h.to_stereotype for h in self.hops)


def plan_route(intent: RelationshipIntent, metadata: Metadata) -> Plan:
    """Choose the route for a relationship intent and work out the direction of every hop.

    Raises RouteError when no approved route fits, when 'via' leaves several routes
    and none of them is the default, or when a filter's or include's stereotype is not
    on the route (or is on it twice, so it's unclear which one is meant).
    """
    source, target = intent.source.code, intent.target.code
    path, is_reversed = _choose_path(source, target, intent.via, metadata)
    steps = tuple(reversed(path.steps)) if is_reversed else path.steps
    plan = Plan(path=path, reversed=is_reversed, hops=_hops(source, steps, metadata))
    _check_filters_on_route(intent, plan)
    _check_includes_on_route(intent, plan)
    return plan


def _check_filters_on_route(intent: RelationshipIntent, plan: Plan) -> None:
    """Every filter's stereotype must appear on the route exactly once."""
    codes = [s.code for s in plan.stereotypes]
    route = " -> ".join(codes)
    for i, rf in enumerate(intent.filters):
        count = codes.count(rf.stereotype.code)
        if count == 0:
            raise RouteError(
                f"filters[{i}] is on {rf.stereotype.code}, but the route from {intent.source.code} "
                f"to {intent.target.code} ({route}) doesn't pass through {rf.stereotype.code}."
            )
        if count > 1:
            raise RouteError(
                f"filters[{i}] is on {rf.stereotype.code}, but the route ({route}) passes through "
                f"{rf.stereotype.code} {count} times, so it's unclear which one the filter means."
            )


def _check_includes_on_route(intent: RelationshipIntent, plan: Plan) -> None:
    """Every included stereotype must appear on the route exactly once.

    TODO(include, step 6): an include off the route will extend it with LEFT joins.
    """
    codes = [s.code for s in plan.stereotypes]
    route = " -> ".join(codes)
    for i, inc in enumerate(intent.include):
        code = inc.stereotype.code
        count = codes.count(code)
        if count == 0:
            raise RouteError(
                f"include[{i}] is {code}, but the route from {intent.source.code} to {intent.target.code} "
                f"({route}) doesn't pass through {code}. Showing stereotypes off the route is not supported yet."
            )
        if count > 1:
            raise RouteError(
                f"include[{i}] is {code}, but the route ({route}) passes through {code} {count} times, "
                "so it's unclear which one is meant."
            )


# ---------------------------------------------------------------- choosing the route


def _choose_path(
    source: str, target: str, via: tuple[Stereotype, ...], metadata: Metadata
) -> tuple[CanonicalPath, bool]:
    """Return (path, reversed) for the question's source and target, honouring via."""
    if not via:
        if (source, target) in metadata.default_path:
            return metadata.default_path[(source, target)], False
        if (target, source) in metadata.default_path:
            return metadata.default_path[(target, source)], True
        raise RouteError(f"There is no supported route between {source} and {target}.")

    if (source, target) in metadata.paths_by_pair:
        routes, is_reversed = metadata.paths_by_pair[(source, target)], False
    elif (target, source) in metadata.paths_by_pair:
        routes, is_reversed = metadata.paths_by_pair[(target, source)], True
    else:
        raise RouteError(f"There is no supported route between {source} and {target}.")

    wanted = {s.code for s in via}
    matching = [p for p in routes if wanted <= _route_stereotypes(p, metadata)]
    through = ", ".join(s.code for s in via)
    if not matching:
        raise RouteError(f"There is no supported route from {source} to {target} through {through}.")
    if len(matching) == 1:
        return matching[0], is_reversed
    defaults = [p for p in matching if p.is_default]
    if defaults:
        return defaults[0], is_reversed
    raise RouteError(
        f"Several routes from {source} to {target} go through {through}; "
        "the question needs to say more precisely which way to go."
    )


def _route_stereotypes(path: CanonicalPath, metadata: Metadata) -> set[str]:
    """Every stereotype code a path passes through, ends included. Direction doesn't matter here."""
    codes = {path.source}
    current = path.source
    for step in path.steps:
        rel = metadata.relationships[step]
        current = rel.target if current == rel.source else rel.source
        codes.add(current)
    return codes


# ---------------------------------------------------------------- directions


def _hops(source: str, steps: tuple[str, ...], metadata: Metadata) -> tuple[Hop, ...]:
    """Walk the steps from the question's source and give each one its direction.

    At each step the current stereotype must be the relationship's stored Start
    (forward) or End (backward). metadata_checks guarantees the steps connect and
    that no relationship is a self-loop, so exactly one of the two is true.
    """
    hops = []
    current = source
    for step in steps:
        rel = metadata.relationships[step]
        if current == rel.source:
            direction, nxt = FORWARD, rel.target
        elif current == rel.target:
            direction, nxt = BACKWARD, rel.source
        else:  # unreachable with checked metadata; fail loudly rather than build wrong joins
            raise RouteError(f"Route step {step} ({rel.source} -> {rel.target}) does not connect to {current}.")
        hops.append(Hop(
            relationship=rel,
            from_stereotype=metadata.stereotypes[current],
            to_stereotype=metadata.stereotypes[nxt],
            direction=direction,
        ))
        current = nxt
    return tuple(hops)
