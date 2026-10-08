"""
Present: turn each method's full result into the one compact contract result Claude receives.

Complete for evidence, compact for Claude. The full result is already recorded in the evidence
store (test run, finding, evidence links, the query text); what leaves here is counts, a few
top signals and pointers to those records. Nothing here can put a raw identifier in the output:
a signal's subject is a masked reference (keyed hash), a group label or a row index. When no
masking key is available, subjects fall back to rank labels and the summary says `values: withheld`.

Raw values can therefore never appear in a contract result, and `--reveal-values` has no effect
on it. A person who needs the rows follows the evidence links in the database.
"""
from __future__ import annotations

from typing import Any

from . import masking
from .contract import (Classification, DatasetVersion, EvidenceInfo, MethodRef, MethodResult, RowIdentitySpec,
                       Signal, Subject, Verdict)
from .suitability import Assessment

SUITABILITY_OVERRIDDEN = "SUITABILITY_OVERRIDDEN"


def dataset_version(ref) -> DatasetVersion:
    """The dataset version a run cited (a `base.DatasetRef`)."""
    return DatasetVersion(ref.dataset_id, ref.basis, ref.fingerprint)


def _evidence(identity, links: int, truncated: bool) -> EvidenceInfo:
    return EvidenceInfo(True, RowIdentitySpec(tuple(identity.columns), identity.kind), links, truncated)


def _key_subjects(keys: list, salt: bytes | None) -> tuple[list[Subject], str]:
    """Masked subjects for identifier values, or rank labels when there is no masking key."""
    if salt is None:
        return [Subject.group(f"key_{i + 1}") for i in range(len(keys))], "withheld"
    return [Subject.masked(masking.mask_value(k, salt)) for k in keys], "masked"


def _ratio(numerator: float, denominator: float) -> float:
    return 0.0 if not denominator else max(0.0, min(1.0, numerator / denominator))


def duplicate_result(method: MethodRef, dataset: DatasetVersion, result, *, run_id: int, finding_id: int,
                     links: int, truncated: bool, params: dict, salt: bytes | None) -> MethodResult:
    subjects, mode = _key_subjects([g["key_value"] for g in result.top_groups], salt)
    kind = "shared_identifier" if result.mode == "shared_identifier" else "duplicate_key"
    signals = [Signal(kind, _ratio(g["row_count"], result.records_examined), s,
                      {"rows": g["row_count"], "distinct": g["measure_value"]})
               for g, s in zip(result.top_groups, subjects)]
    return MethodResult.build(
        method=method, dataset=dataset, verdict=Verdict.SUPPORTED,
        classification=Classification.ANOMALY if result.flagged_keys else Classification.OBSERVATION,
        records_scanned=result.records_examined, findings_count=1,
        summary={"mode": result.mode, "flagged_keys": result.flagged_keys, "flagged_rows": result.flagged_rows,
                 "max_group": result.max_group_size, "keys_examined": result.keys_examined,
                 "placeholders_excluded": result.placeholders_excluded_rows, "values": mode},
        parameters=params, signals=signals, evidence=_evidence(result.identity, links, truncated),
        finding_ids=(finding_id,), run_id=run_id)


def fuzzy_result(method: MethodRef, dataset: DatasetVersion, result, *, run_id: int, finding_id: int,
                 links: int, truncated: bool, params: dict, salt: bytes | None) -> MethodResult:
    subjects, mode = _key_subjects([g["key_value"] for g in result.top_groups], salt)
    signals = [Signal("shared_identifier", _ratio(g["distinct_entities"], g["raw_distinct_names"]), s,
                      {"entities": g["distinct_entities"], "raw_names": g["raw_distinct_names"]})
               for g, s in zip(result.top_groups, subjects)]
    return MethodResult.build(
        method=method, dataset=dataset, verdict=Verdict.SUPPORTED,
        classification=Classification.ANOMALY if result.flagged_keys else Classification.OBSERVATION,
        records_scanned=result.records_examined, findings_count=1,
        summary={"keys_examined": result.keys_examined, "multi_name_keys": result.keys_with_multiple_raw,
                 "flagged_keys": result.flagged_keys, "collapsed": result.collapsed_by_spelling,
                 "max_entities": result.max_entities, "values": mode},
        parameters={**params, "threshold": result.threshold}, signals=signals,
        evidence=_evidence(result.identity, links, truncated), finding_ids=(finding_id,), run_id=run_id)


def cross_result(method: MethodRef, dataset: DatasetVersion, result, *, run_id: int, finding_id: int,
                 links: int, truncated: bool, params: dict) -> MethodResult:
    signals = []
    if result.only_left:
        signals.append(Signal("orphan_keys", _ratio(result.only_left, result.left_distinct),
                              Subject.group("left_keys"), {"count": result.only_left}))
    if result.only_right:
        signals.append(Signal("orphan_keys", _ratio(result.only_right, result.right_distinct),
                              Subject.group("right_keys"), {"count": result.only_right},
                              classification=Classification.OBSERVATION))
    return MethodResult.build(
        method=method, dataset=dataset, verdict=Verdict.SUPPORTED,
        classification=Classification.ANOMALY if result.only_left else Classification.OBSERVATION,
        records_scanned=result.left_rows, findings_count=1,
        summary={"left_rows": result.left_rows, "right_rows": result.right_rows, "left_distinct": result.left_distinct,
                 "right_distinct": result.right_distinct, "in_both": result.in_both, "only_left": result.only_left,
                 "only_right": result.only_right, "left_coverage_pct": result.left_coverage_pct,
                 "right_coverage_pct": result.right_coverage_pct},
        parameters=params, signals=signals, evidence=_evidence(result.identity, links, truncated),
        finding_ids=(finding_id,), run_id=run_id)


def duplicate_payment_result(method: MethodRef, dataset: DatasetVersion, result, *, run_id: int, finding_id: int,
                             links: int, truncated: bool, params: dict, salt: bytes | None) -> MethodResult:
    subjects, mode = _key_subjects([f"{g['period']}|{g['names'][0]}" for g in result.top_groups], salt)
    signals = [Signal("duplicate_payment", _ratio(g["paid_instances"], g["known_entities"] or 1), s,
                      {"period": g["period"], "paid_instances": g["paid_instances"],
                       "known_entities": g["known_entities"]})
               for g, s in zip(result.top_groups, subjects)]
    return MethodResult.build(
        method=method, dataset=dataset, verdict=Verdict.SUPPORTED,
        classification=Classification.ANOMALY if result.flagged_groups else Classification.OBSERVATION,
        records_scanned=result.records_examined, findings_count=1,
        summary={"groups_examined": result.groups_examined, "flagged_groups": result.flagged_groups,
                 "flagged_rows": result.flagged_rows, "max_paid_instances": result.max_paid_instances,
                 "identity_table_used": result.identity_table_used, "merge_map_size": result.merge_map_size,
                 "values": mode},
        parameters=params, signals=signals, evidence=_evidence(result.identity, links, truncated),
        finding_ids=(finding_id,), run_id=run_id)


def benford_result(method: MethodRef, dataset: DatasetVersion, result, assessment: Assessment, *, run_id: int,
                   finding_id: int, overridden: bool, anomalous: bool) -> MethodResult:
    """Benford. A run that went ahead despite an unsuitable verdict is reported as completed with a
    warning that says so, never as a plain SUPPORTED one, and is never an anomaly."""
    if overridden:
        verdict = Verdict.SUPPORTED_WITH_WARNING
        why = ((SUITABILITY_OVERRIDDEN,) + tuple(assessment.reason_codes))[:6]
    else:
        verdict, why = assessment.verdict, tuple(assessment.reason_codes)
    signals = []
    if anomalous and not overridden:
        worst = max(range(1, 10), key=lambda d: abs(result.digit_observed_pct[d] - result.digit_expected_pct[d]))
        signals.append(Signal("digit_distribution", min(1.0, result.mad), Subject.group("first_digit"),
                              {"mad": round(result.mad, 5), "worst_digit": worst,
                               "observed_pct": round(100 * result.digit_observed_pct[worst], 2),
                               "expected_pct": round(100 * result.digit_expected_pct[worst], 2)}))
    return MethodResult.build(
        method=method, dataset=dataset, verdict=verdict, reason_codes=why,
        classification=Classification.ANOMALY if signals else Classification.OBSERVATION,
        records_scanned=result.records_examined, findings_count=1,
        summary={"mad": round(result.mad, 5), "chi_square": round(result.chi_square, 2),
                 "conformity": result.conformity.replace(" ", "_"), "reliable": result.reliable_sample_size,
                 "thresholds": "unvalidated" if assessment.unvalidated else "validated"},
        signals=signals, finding_ids=(finding_id,), run_id=run_id)


# ------------------------------------------------------------------ refusals and errors
def method_ref(subtest: str, version: str | None) -> MethodRef:
    """A MethodRef for whatever was asked for, falling back when the name is not a valid method id."""
    try:
        return MethodRef(subtest, version or "0.0.0")
    except ValueError:
        return MethodRef("engine", "0.0.0")


def refused(subtest: str, version: str | None, code: str, reason: str) -> dict:
    return MethodResult.refused(method_ref(subtest, version), code, reason[:300]).to_dict()


def failed(subtest: str, version: str | None, code: str, reason: str) -> dict:
    return MethodResult.failed(method_ref(subtest, version), code, reason[:300]).to_dict()
