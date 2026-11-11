"""Skill 契約 — pydantic による型とバリデーション。

## なぜ pydantic か

`idempotent: true` と書くだけでは何の保証にもならない。
宣言は**主張**であって**検証**ではない。

冪等性とは:

    f(x) = y  のとき   f(y) = y

これを**テストで実測**する。ただし pydantic はその「測る器」を
型で強制し、入口と出口の型違反を実行前に落とす。

## 層の責務

    SkillSpec   契約の宣言（入口・出口・failure・副作用）
    Skill       実行の器（型検査、failure 分離）
    validate_idempotent  冪等性の実測（宣言ではなく）

冪等性は `SkillSpec` のフィールドではない。
`validate_idempotent()` が**実行して**確かめる。
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Iterable
from enum import Enum
from pathlib import Path
from typing import Any, Generic, TypeVar

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

__all__ = [
    "FailureMode",
    "Spec",
    "SkillError",
    "SkillFailure",
    "Skill",
    "IdempotencyReport",
    "validate_idempotent",
    "digest_of",
]

InT = TypeVar("InT")
OutT = TypeVar("OutT")

VOLATILE_MANIFEST_KEYS = frozenset(
    {"started_at", "finished_at", "wall_ms", "host", "pid"}
)


def digest_of(value: Any) -> str:
    """Stable digest of *value*, with repr() fallback for opaque objects."""
    try:
        blob = json.dumps(value, sort_keys=True, ensure_ascii=False, default=repr)
    except TypeError:
        blob = repr(value)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Failure
# ---------------------------------------------------------------------------


class FailureMode(str, Enum):
    """Recoverable, *declared* conditions."""

    MISSING_MATERIAL = "missing_material"
    DEGENERATE_GEOMETRY = "degenerate_geometry"
    EMPTY_INPUT = "empty_input"
    UPSTREAM_UNREACHABLE = "upstream_unreachable"
    TIMEOUT = "timeout"
    NUMERIC_INSTABILITY = "numeric_instability"


class SkillError(RuntimeError):
    """Undeclared failure. A bug in the Skill, not a bad input."""


class SkillFailure(RuntimeError):
    """A declared failure mode fired."""

    def __init__(self, mode: FailureMode, detail: str = "") -> None:
        self.mode = mode
        super().__init__(f"{mode.value}: {detail}" if detail else mode.value)


# ---------------------------------------------------------------------------
# Spec
# ---------------------------------------------------------------------------


class Port(BaseModel):
    """One typed edge of a Skill."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(min_length=1)
    type_name: str = Field(min_length=1)
    optional: bool = False


class Spec(BaseModel, Generic[InT, OutT]):
    """Contract of a Skill: one job, typed in and out.

    Note what is **absent**: there is no `idempotent` field. Idempotency is
    established by `validate_idempotent()` running the Skill twice and
    comparing. Asserting it here would be a claim, not a guarantee.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(min_length=1)
    description: str = Field(min_length=1)

    inputs: tuple[Port, ...] = ()
    outputs: tuple[Port, ...] = ()

    # mode -> action. Actions: raise | skip_frame | use_default | retry
    failure_actions: dict[FailureMode, str] = Field(default_factory=dict)

    # Declared side-effect paths (relative to an instance root).
    writes_to: tuple[str, ...] = ()

    manifest_fields: tuple[str, ...] = ()

    @field_validator("failure_actions")
    @classmethod
    def _known_actions(cls, v: dict[FailureMode, str]) -> dict[FailureMode, str]:
        allowed = {"raise", "skip_frame", "use_default", "retry"}
        bad = {k: a for k, a in v.items() if a not in allowed}
        if bad:
            raise ValueError(
                f"unknown failure action(s): "
                + ", ".join(f"{k.value}={a!r}" for k, a in bad.items())
            )
        return v

    @model_validator(mode="after")
    def _needs_a_job(self) -> "Spec":
        if not self.outputs and not self.writes_to:
            raise ValueError(
                f"{self.name}: a Skill must declare at least one output "
                "or one side-effect path, otherwise it does nothing"
            )
        return self

    @property
    def required_inputs(self) -> tuple[Port, ...]:
        return tuple(p for p in self.inputs if not p.optional)

    def input_type(self, name: str) -> str | None:
        for p in self.inputs:
            if p.name == name:
                return p.type_name
        return None

    def spec_hash(self) -> str:
        """Fingerprint of the *contract*, not the behaviour."""
        return digest_of({
            "name": self.name,
            "inputs": [p.model_dump() for p in self.inputs],
            "outputs": [p.model_dump() for p in self.outputs],
            "failure_actions": {k.value: v for k, v in self.failure_actions.items()},
            "writes_to": sorted(self.writes_to),
        })


# ---------------------------------------------------------------------------
# Skill
# ---------------------------------------------------------------------------


class Skill(Generic[InT, OutT]):
    """Execution wrapper.

    Guarantees the author does not have to remember:

    - required inputs are present (checked before the call)
    - a declared failure with action ``raise`` becomes ``SkillFailure``
    - an undeclared exception becomes ``SkillError`` so it cannot be
      mistaken for expected behaviour
    """

    def __init__(
        self,
        spec: Spec[InT, OutT],
        fn: Callable[[InT], OutT],
    ) -> None:
        self.spec = spec
        self.fn = fn

    def __call__(self, payload: InT) -> OutT:
        self._check_inputs(payload)
        try:
            return self.fn(payload)
        except (SkillFailure, SkillError):
            raise
        except Exception as exc:  # noqa: BLE001
            raise SkillError(
                f"{self.spec.name}: undeclared failure "
                f"{type(exc).__name__}: {exc}"
            ) from exc

    def _check_inputs(self, payload: Any) -> None:
        missing = [
            p.name for p in self.spec.required_inputs
            if getattr(payload, p.name, None) is None
        ]
        if missing:
            mode = FailureMode.MISSING_MATERIAL
            if mode in self.spec.failure_actions:
                if self.spec.failure_actions[mode] == "raise":
                    raise SkillFailure(mode, ", ".join(missing))
                return
            raise SkillError(
                f"{self.spec.name}: required input(s) missing: {', '.join(missing)}"
            )

    def failed(self, mode: FailureMode, detail: str = "") -> None:
        """Raise a declared failure. Use inside the Skill body."""
        raise SkillFailure(mode, detail)

    def action_for(self, mode: FailureMode) -> str | None:
        return self.spec.failure_actions.get(mode)


# ---------------------------------------------------------------------------
# Idempotency validation
# ---------------------------------------------------------------------------


class IdempotencyReport(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    skill: str
    output_stable: bool
    manifest_stable: bool
    no_side_effect: bool
    digest_run1: str
    digest_run2: str
    notes: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _explain(self) -> "IdempotencyReport":
        return self

    @property
    def idempotent(self) -> bool:
        return self.output_stable and self.manifest_stable and self.no_side_effect

    def render(self) -> str:
        mark = "PASS" if self.idempotent else "FAIL"
        body = [
            f"[{mark}] {self.skill}",
            f"  output stable   : {self.output_stable}",
            f"  manifest stable : {self.manifest_stable}",
            f"  no side effect  : {self.no_side_effect}",
            f"  digest run1     : {self.digest_run1[:16]}",
            f"  digest run2     : {self.digest_run2[:16]}",
        ]
        if self.notes:
            body.append("  notes:")
            body.extend(f"    - {n}" for n in self.notes)
        return "\n".join(body)


def _snapshot(paths: Iterable[Path]) -> dict[str, str]:
    return {
        str(p): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in paths
        if p.is_file()
    }


def _strip_volatile(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            k: _strip_volatile(v)
            for k, v in value.items()
            if k not in VOLATILE_MANIFEST_KEYS
        }
    if isinstance(value, list):
        return [_strip_volatile(v) for v in value]
    return value


def validate_idempotent(
    skill: Skill[InT, OutT],
    payload: InT,
    *,
    manifest_of: Callable[[Any], Any] | None = None,
    observed_paths: tuple[Path, ...] = (),
) -> IdempotencyReport:
    """Measure idempotency by **running the Skill twice**, not by reading a flag.

    Checks
    ------
    1. returned payload is byte-stable across two runs
    2. manifest is stable after removing ``VOLATILE_MANIFEST_KEYS``
       (``started_at`` and friends are allowed to differ)
    3. observed paths did not change between runs, and did not appear
       only on the first run

    A Skill that writes a file only on the first call is **not**
    idempotent even if the returned payload is stable.
    """
    notes: list[str] = []

    r1 = skill(payload)
    mid = _snapshot(observed_paths)
    r2 = skill(payload)
    after = _snapshot(observed_paths)

    d1, d2 = digest_of(r1), digest_of(r2)
    output_stable = d1 == d2
    if not output_stable:
        notes.append("returned payload differs between runs")

    manifest_stable = True
    if manifest_of is not None:
        m1 = digest_of(_strip_volatile(manifest_of(r1)))
        m2 = digest_of(_strip_volatile(manifest_of(r2)))
        manifest_stable = m1 == m2
        if not manifest_stable:
            notes.append(
                "manifest still differs after removing "
                + ", ".join(sorted(VOLATILE_MANIFEST_KEYS))
            )

    changed_between = mid != after
    appeared_on_first = set(after) - set(mid)
    no_side_effect = not changed_between and not appeared_on_first
    if changed_between:
        notes.append("observed paths changed on the second run")
    if appeared_on_first:
        notes.append("observed paths appeared only on the first run")

    return IdempotencyReport(
        skill=skill.spec.name,
        output_stable=output_stable,
        manifest_stable=manifest_stable,
        no_side_effect=no_side_effect,
        digest_run1=d1,
        digest_run2=d2,
        notes=tuple(notes),
    )
