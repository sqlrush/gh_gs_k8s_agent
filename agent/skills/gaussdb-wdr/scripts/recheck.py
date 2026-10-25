"""The deterministic anchoring gate — port of internal/probe/wdr/recheck.go.

Cross-checks an Interp against an Evidence pack: interp content that fails the
check is quarantined (Unanchored / Unverified / Missing) and never shown as a
formal finding. Overall severity is always taken from the evidence (never
inflated by the interp).
"""
from __future__ import annotations

from dataclasses import dataclass, field

from interp import Interp, InterpFinding, Suggestion
from model import Evidence, Finding, Severity


@dataclass
class AnchoredFinding:
    ev: Finding
    root_cause: str
    verified: list = field(default_factory=list)
    unverified: list = field(default_factory=list)


@dataclass
class Rechecked:
    overall: Severity = Severity.OK
    driver: str = ""
    anchored: list = field(default_factory=list)
    unanchored: list = field(default_factory=list)  # list[InterpFinding]
    missing: list = field(default_factory=list)      # list[Finding] ≥WARN uncovered
    sev_mismatch: bool = False
    badge: str = ""


def suggestion_verified(s: Suggestion) -> bool:
    """May this suggestion appear as a formal recommendation? Advisory ones
    (no validation, or method none/empty) are allowed; suggestions CLAIMING a
    hypopg/cost-rewrite method must have status=verified."""
    if s.validation is None:
        return True
    if s.validation.method in ("hypopg", "cost-rewrite"):
        return s.validation.status == "verified"
    return True  # none / "" / n/a


def suggestion_proven(s: Suggestion) -> bool:
    """Was this suggestion actually hypopg/cost-verified? Only proven ones count
    toward the badge's "经实证" tally, so the badge never overclaims."""
    return (s.validation is not None and
            s.validation.method in ("hypopg", "cost-rewrite") and
            s.validation.status == "verified")


def recheck(ev: Evidence, in_: Interp) -> Rechecked:
    # Index evidence findings by Code, keeping the worst severity per code.
    by_code: dict[str, Finding] = {}
    for f in ev.findings:
        cur = by_code.get(f.code)
        if cur is None or f.severity > cur.severity:
            by_code[f.code] = f

    r = Rechecked(overall=ev.overall, driver=in_.overall.driver)
    covered: set[str] = set()
    for inf in in_.findings:
        evf = by_code.get(inf.code)
        if evf is None:
            r.unanchored.append(inf)
            continue
        covered.add(inf.code)
        af = AnchoredFinding(ev=evf, root_cause=inf.root_cause)
        for s in inf.suggestions:
            if suggestion_verified(s):
                af.verified.append(s)
            else:
                af.unverified.append(s)
        r.anchored.append(af)
    # stable sort by evidence severity desc
    r.anchored.sort(key=lambda a: int(a.ev.severity), reverse=True)

    # Missing: evidence findings ≥WARN not covered by any interp finding.
    for f in ev.findings:
        if f.severity >= Severity.WARN and f.code not in covered:
            r.missing.append(f)

    r.sev_mismatch = (in_.overall.severity != "" and
                      _parse_sev(in_.overall.severity) != ev.overall)
    r.badge = build_badge(r)
    return r


def _parse_sev(s: str) -> Severity:
    from interp import parse_severity
    return parse_severity(s)


def build_badge(r: Rechecked) -> str:
    proven = advisory = 0
    for a in r.anchored:
        for s in a.verified:
            if suggestion_proven(s):
                proven += 1
            else:
                advisory += 1
    clean = not r.missing and not r.unanchored and not r.sev_mismatch
    if clean:
        return (f"✓ 已锚定（{len(r.anchored)} 条发现全覆盖、无夸大、无遗漏；"
                f"{proven} 条建议经 hypopg/cost 实证、{advisory} 条建议性）")
    msg = "⚠ 校验有偏差："
    if r.missing:
        msg += f"漏报 {len(r.missing)} 条；"
    if r.unanchored:
        msg += f"未锚定 {len(r.unanchored)} 条；"
    if r.sev_mismatch:
        msg += "总体严重度不符（以确定性为准）；"
    return msg
