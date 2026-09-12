# Incident action policy — deterministic, auditable, versioned.
#
# The orchestrator asks this policy before executing any state-changing action
# (rollback, canary, bounded intervention). Rego decisions are separate from
# enforcement: this file *decides*, ic/policy.py *asks*, ic/orchestrator.py
# *acts on* the answer.
#
# Rule of thumb: if a check involves comparing numbers, evaluating an envelope,
# or matching an action class, it lives here. If a check requires talking to
# telemetry, running a probe, or writing to Cassandra, it lives in Python.
#
# Input contract (see ic/policy.py for the builders):
#   action                 : "rollback" | "canary" | "intervention"
#   incident_id            : string
#   posterior              : float in [0,1]         — winning hypothesis confidence
#   evidence_refs          : [string]                — cited resolvable evidence
#   deploy_recent          : bool                    — deploy within rollback window
#   upstream_outage        : bool                    — cascade detected upstream
#   observation_stagnated  : bool                    — VoI observation-EIG floor hit
#   safety_envelope        : { max_traffic_pct, max_duration_s, auto_revert }
#
# Output:
#   allow (bool), reasons ([string]), decision (composite)

package incident.action

import rego.v1

# ============================================================================
# Thresholds — externalized so a real deployment can override without editing
# rule bodies. Keep small; if this list grows, split by action type.
# ============================================================================
posterior_threshold_rollback     := 0.75
posterior_threshold_canary_low   := 0.40
posterior_threshold_canary_high  := 0.85
max_canary_traffic_pct           := 10
max_canary_duration_s            := 300
max_intervention_traffic_pct     := 5
max_intervention_duration_s      := 60

# ============================================================================
# Default: deny. Every rule must earn allow.
# ============================================================================
default allow := false

# ---------------------------------------------------------------------------
# Rollback — remove a suspect deploy.
# ---------------------------------------------------------------------------
allow if {
    input.action == "rollback"
    input.posterior >= posterior_threshold_rollback
    input.deploy_recent == true
    input.upstream_outage == false
    count(input.evidence_refs) > 0
}

# ---------------------------------------------------------------------------
# Canary — bounded partial traffic shift for hypothesis discrimination.
# Only fires when observation has stagnated AND we're in the ambiguous band
# where a causal probe is genuinely informative (very-high posterior = go
# straight to rollback; very-low = keep observing, not enough signal yet).
# ---------------------------------------------------------------------------
allow if {
    input.action == "canary"
    input.posterior >= posterior_threshold_canary_low
    input.posterior <= posterior_threshold_canary_high
    input.observation_stagnated == true
    input.safety_envelope.max_traffic_pct <= max_canary_traffic_pct
    input.safety_envelope.max_duration_s <= max_canary_duration_s
    input.safety_envelope.auto_revert == true
    count(input.evidence_refs) > 0
}

# ---------------------------------------------------------------------------
# Intervention — the tightest bounded traffic shift (5%/60s). Same shape as
# canary, tighter envelope. This is the action the VoI engine actually emits.
# ---------------------------------------------------------------------------
allow if {
    input.action == "intervention"
    input.observation_stagnated == true
    input.safety_envelope.max_traffic_pct <= max_intervention_traffic_pct
    input.safety_envelope.max_duration_s <= max_intervention_duration_s
    input.safety_envelope.auto_revert == true
    count(input.evidence_refs) > 0
}

# ============================================================================
# Reasons — every failed precondition is collected so the caller (and the
# audit log) can see WHY something was denied, not just that it was.
# Rego set-generating rules: any rule that fires contributes its string.
# ============================================================================

# Universal: no evidence cited → deny (any action).
reasons contains "no_evidence_cited" if {
    count(input.evidence_refs) == 0
}

# Rollback preconditions.
reasons contains "posterior_below_rollback_threshold" if {
    input.action == "rollback"
    input.posterior < posterior_threshold_rollback
}
reasons contains "deploy_not_recent" if {
    input.action == "rollback"
    input.deploy_recent == false
}
reasons contains "upstream_outage_detected" if {
    input.action == "rollback"
    input.upstream_outage == true
}

# Canary preconditions.
reasons contains "posterior_outside_canary_band" if {
    input.action == "canary"
    input.posterior < posterior_threshold_canary_low
}
reasons contains "posterior_outside_canary_band" if {
    input.action == "canary"
    input.posterior > posterior_threshold_canary_high
}
reasons contains "not_stagnated_yet" if {
    input.action == "canary"
    input.observation_stagnated == false
}
reasons contains "envelope_traffic_too_wide" if {
    input.action == "canary"
    input.safety_envelope.max_traffic_pct > max_canary_traffic_pct
}
reasons contains "envelope_duration_too_long" if {
    input.action == "canary"
    input.safety_envelope.max_duration_s > max_canary_duration_s
}
reasons contains "auto_revert_not_enforced" if {
    input.action == "canary"
    input.safety_envelope.auto_revert != true
}

# Intervention preconditions.
reasons contains "not_stagnated_yet" if {
    input.action == "intervention"
    input.observation_stagnated == false
}
reasons contains "envelope_traffic_too_wide" if {
    input.action == "intervention"
    input.safety_envelope.max_traffic_pct > max_intervention_traffic_pct
}
reasons contains "envelope_duration_too_long" if {
    input.action == "intervention"
    input.safety_envelope.max_duration_s > max_intervention_duration_s
}
reasons contains "auto_revert_not_enforced" if {
    input.action == "intervention"
    input.safety_envelope.auto_revert != true
}

# ============================================================================
# Composite decision — one object the caller can log verbatim into the
# evidence chain as the policy-decision leaf.
# ============================================================================
decision := {
    "allow": allow,
    "action": input.action,
    "reasons": reasons,
    "thresholds": {
        "posterior_threshold_rollback": posterior_threshold_rollback,
        "posterior_threshold_canary_low": posterior_threshold_canary_low,
        "posterior_threshold_canary_high": posterior_threshold_canary_high,
        "max_canary_traffic_pct": max_canary_traffic_pct,
        "max_canary_duration_s": max_canary_duration_s,
        "max_intervention_traffic_pct": max_intervention_traffic_pct,
        "max_intervention_duration_s": max_intervention_duration_s,
    },
}
