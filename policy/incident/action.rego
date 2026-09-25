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
# LIVE MODE — autonomous actions against the running target service.
#
# The live controller (ic/live/controller.py) acts WITHOUT a human in the
# normal path, so this block is the entire permission model for autonomy.
# The agent proposes an action; it cannot grant itself one.
#
# Design rule: the condition for autonomy is REVERSIBILITY, not confidence.
#   * Reversible, service-scoped actions may execute on their own.
#   * An action concluded only by excluding alternatives (no positive
#     signature evidence) is still allowed IF it is reversible AND an
#     auto-revert AND a fresh-telemetry verification are planned — the
#     verification step is what catches a wrong-but-defensible diagnosis.
#   * Irreversible or shared-infrastructure actions are forbidden outright.
#
# Extra input fields (live only):
#   mode                 : "live"
#   reversible           : bool
#   blast_radius         : "service" | "shared_dependency" | "platform"
#   confirmation         : "positive" | "exclusion_only" | "none"
#   verification_planned : bool
#   reverts_agent_action : bool   (auto_revert only)
# ============================================================================
posterior_threshold_remediation := 0.75
max_canary_probe_traffic_pct    := 25
max_canary_probe_duration_s     := 30

live_actions := {"rollback_deploy", "canary_probe", "enable_inventory_fallback", "auto_revert"}

# Never autonomous, whatever the confidence: irreversible, or blast radius
# beyond the incident's own service.
forbidden_actions := {
    "failover_database", "restart_database", "drop_database",
    "delete_namespace", "scale_to_zero", "flush_shared_cache",
}

is_live_action if input.action in live_actions
is_forbidden_action if input.action in forbidden_actions

confirmation_sufficient if input.confirmation == "positive"
confirmation_sufficient if {
    input.confirmation == "exclusion_only"
    input.safety_envelope.auto_revert == true
    input.verification_planned == true
}

# Roll back the most recent deploy of the incident's own service.
allow if {
    input.action == "rollback_deploy"
    input.reversible == true
    input.blast_radius == "service"
    input.posterior >= posterior_threshold_remediation
    input.deploy_recent == true
    input.upstream_outage == false
    count(input.evidence_refs) > 0
    confirmation_sufficient
}

# Serve cached inventory instead of calling a degraded dependency. Reversible
# and service-scoped, but it changes customer-visible data freshness, so it
# requires positive evidence rather than exclusion.
allow if {
    input.action == "enable_inventory_fallback"
    input.reversible == true
    input.blast_radius == "service"
    input.posterior >= posterior_threshold_remediation
    input.confirmation == "positive"
    count(input.evidence_refs) > 0
}

# Interventional probe: route a bounded slice of traffic to the previous
# version to measure a cohort difference. The service enforces the expiry.
allow if {
    input.action == "canary_probe"
    input.safety_envelope.max_traffic_pct <= max_canary_probe_traffic_pct
    input.safety_envelope.max_duration_s <= max_canary_probe_duration_s
    input.safety_envelope.auto_revert == true
    count(input.evidence_refs) > 0
}

# Undo an action this agent executed in this incident, after verification
# failed. Restoring the pre-action state is always permitted.
allow if {
    input.action == "auto_revert"
    input.reverts_agent_action == true
    count(input.evidence_refs) > 0
}

reasons contains "action_forbidden_by_policy" if is_forbidden_action

reasons contains "action_not_reversible" if {
    input.mode == "live"
    input.reversible == false
}

reasons contains "unknown_action_class" if {
    input.mode == "live"
    not is_live_action
    not is_forbidden_action
}

reasons contains "blast_radius_exceeds_autonomy_limit" if {
    input.mode == "live"
    input.blast_radius != "service"
}

reasons contains "posterior_below_remediation_threshold" if {
    input.action in {"rollback_deploy", "enable_inventory_fallback"}
    input.posterior < posterior_threshold_remediation
}

reasons contains "deploy_not_recent" if {
    input.action == "rollback_deploy"
    input.deploy_recent == false
}

reasons contains "upstream_outage_detected" if {
    input.action == "rollback_deploy"
    input.upstream_outage == true
}

reasons contains "insufficient_confirmation" if {
    input.action == "rollback_deploy"
    not confirmation_sufficient
}

reasons contains "positive_confirmation_required" if {
    input.action == "enable_inventory_fallback"
    input.confirmation != "positive"
}

reasons contains "envelope_traffic_too_wide" if {
    input.action == "canary_probe"
    input.safety_envelope.max_traffic_pct > max_canary_probe_traffic_pct
}

reasons contains "envelope_duration_too_long" if {
    input.action == "canary_probe"
    input.safety_envelope.max_duration_s > max_canary_probe_duration_s
}

reasons contains "auto_revert_not_enforced" if {
    input.action == "canary_probe"
    input.safety_envelope.auto_revert != true
}

reasons contains "revert_target_not_agent_action" if {
    input.action == "auto_revert"
    input.reverts_agent_action != true
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
        "posterior_threshold_remediation": posterior_threshold_remediation,
        "max_canary_probe_traffic_pct": max_canary_probe_traffic_pct,
        "max_canary_probe_duration_s": max_canary_probe_duration_s,
    },
}
