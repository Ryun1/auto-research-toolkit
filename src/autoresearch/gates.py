"""Domain-defined acceptance evidence, independent of track status."""
from __future__ import annotations

import dataclasses
import json
from dataclasses import dataclass, field

from .errors import ConfigError, SchemaError, TransitionError

STATES = ("pending", "passed", "failed", "blocked")


@dataclass
class Gate:
    name: str
    state: str = "pending"
    evidence: list[str] = field(default_factory=list)
    required: bool = True

    def __post_init__(self):
        if not isinstance(self.name, str) or not self.name.strip():
            raise SchemaError("gate name must be a non-empty string")
        if self.state not in STATES:
            raise SchemaError(f"gate {self.name!r}: state must be one of {STATES}")
        if type(self.required) is not bool:
            raise SchemaError(f"gate {self.name!r}: required must be boolean")
        if not isinstance(self.evidence, list) or any(
                not isinstance(e, str) or not e.strip() for e in self.evidence):
            raise SchemaError(f"gate {self.name!r}: evidence must be non-empty strings")
        if self.state == "passed" and not self.evidence:
            raise SchemaError(f"gate {self.name!r}: passed requires evidence")


def validate(gates):
    if not isinstance(gates, list):
        raise SchemaError("gates must be a list")
    names = set()
    for gate in gates:
        if not isinstance(gate, Gate):
            raise SchemaError("each gate must be a Gate record")
        gate.__post_init__()
        if gate.name in names:
            raise SchemaError(f"duplicate gate name {gate.name!r}")
        names.add(gate.name)


def definitions(spec) -> list[Gate]:
    """Parse [[tracks.gates]] definitions; outcomes belong only to entries."""
    if not isinstance(spec, list):
        raise ConfigError("tracks.gates must be a list of name/required definitions")
    out = []
    try:
        for item in spec:
            if not isinstance(item, dict) or set(item) - {"name", "required"}:
                raise ConfigError("gate definitions accept only name and required")
            out.append(Gate(**item))
        validate(out)
    except (SchemaError, TypeError) as exc:
        raise ConfigError(f"tracks.gates: {exc}") from exc
    return out


def configure(entry, definitions):
    """Reconcile domain definitions, retaining outcomes for unchanged names."""
    if entry.result is not None:
        raise TransitionError("reopen a closed entry before configuring its gates")
    validate(definitions)
    existing = {gate.name: gate for gate in entry.gates}
    entry.gates = [dataclasses.replace(existing.get(g.name, g),
                                      required=g.required,
                                      evidence=list(existing.get(g.name, g).evidence))
                   for g in definitions]


def update(entry, name, state, evidence):
    if entry.result is not None:
        raise TransitionError("reopen a closed entry before updating its gates")
    for index, gate in enumerate(entry.gates):
        if gate.name == name:
            entry.gates[index] = Gate(name, state, list(evidence), gate.required)
            return
    raise SchemaError(f"{entry.id}: unknown gate {name!r}; configure domain gates first")


def _command(args):
    from .claims import Claims
    from .config import DomainConfig, discover
    from .entries import Store

    config = DomainConfig.load(args.domain or discover())
    store = Store(config.paths.entries)
    config.policy.check_command(f"ar gates {args.gates_action}")
    if args.gates_action == "show":
        entry = store.load(args.id)
    else:
        claims = Claims(store, config, args.session)
        if args.gates_action == "configure":
            entry = claims.mutate(args.id, "gates-configured", args.why,
                                  lambda e: configure(e, config.track_for(e.id).gates))
        else:
            entry = claims.mutate(args.id, "gate-updated", args.why,
                                  lambda e: update(e, args.name, args.state,
                                                   args.evidence or []))
    required = config.track_for(entry.id).machine.required_gates
    print(json.dumps({"id": entry.id, "readiness": entry.gate_readiness(required),
                      "gates": [dataclasses.asdict(g) for g in entry.gates]}, indent=2))
    return 0


def register_parser(subparsers):
    parser = subparsers.add_parser("gates", help="configure and record acceptance evidence")
    actions = parser.add_subparsers(dest="gates_action", required=True)
    for action in ("configure", "update", "show"):
        command = actions.add_parser(action)
        command.add_argument("id")
        if action != "show":
            command.add_argument("--why", required=True)
        if action == "update":
            command.add_argument("name")
            command.add_argument("--state", choices=STATES, required=True)
            command.add_argument("--evidence", action="append")
        command.set_defaults(func=_command)
