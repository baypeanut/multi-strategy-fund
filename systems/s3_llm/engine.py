"""S3Engine — discretionary book: briefing -> PM proposal -> hard wrapper.

The LLM/PM proposes; the deterministic RiskWrapper disposes. Output is a
sanitized, limit-compliant target-weight Series plus an audit trail.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd

from .pm import PM, HeuristicPM
from .wrapper import RiskWrapper


@dataclass
class S3Result:
    weights: pd.Series
    violations: list[str] = field(default_factory=list)
    raw_proposal: dict = field(default_factory=dict)
    pm_source: str = "heuristic"     # audit: which brain actually decided


@dataclass
class S3Engine:
    wrapper: RiskWrapper
    pm: PM = field(default_factory=HeuristicPM)

    def generate(
        self,
        briefing: dict,
        adv: dict[str, float] | None = None,
        current_weights: dict[str, float] | None = None,
    ) -> S3Result:
        proposal = self.pm.propose(briefing)
        result = self.wrapper.validate(proposal, adv=adv, current_weights=current_weights)
        return S3Result(weights=result.weights, violations=result.violations,
                        raw_proposal=proposal,
                        pm_source=getattr(self.pm, "last_source", "heuristic"))
