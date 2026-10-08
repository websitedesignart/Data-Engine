"""
The single entry point through which a forensic subtest is ever executed.
This is what the forensic-test-engine skill calls — it never lets an LLM
compute a result itself; it only ever reports what this script writes.

Usage:
    .venv\\Scripts\\python.exe forensic_platform\\scripts\\run_test.py \\
        --database demo --subtest benford --schema public --table transactions --column amount

Prints a single JSON object (the recorded test_runs row plus any findings) to
stdout. Every invocation writes an audit_log row, success or failure.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import psycopg2

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from forensic_platform.core.config import forensic_dsn
from forensic_platform.core.db import connect, describe_db_error, timeouts
from forensic_platform.core import masking
from forensic_platform.core import present
from forensic_platform.core.contract import ContractViolation, MethodRef, Verdict
from forensic_platform.core.guard import assess_column
from forensic_platform.core.identity import NoRowIdentity, resolve_row_identity
from forensic_platform.core.sqlsafe import UnsafeIdentifier, check_identifier, table_exists
from forensic_platform.tests_engine import base
from forensic_platform.tests_engine import benford
from forensic_platform.tests_engine import duplicate_analysis
from forensic_platform.tests_engine import fuzzy_entity_match
from forensic_platform.tests_engine import cross_dataset_match
from forensic_platform.tests_engine import duplicate_payment

ACTOR = "forensic-test-engine skill"

# Subtests that write record-level evidence, so they need a usable row identity.
EVIDENCE_SUBTESTS = {"duplicate-analysis", "fuzzy-entity-match", "cross-dataset-match", "duplicate-payment"}

DISPATCH = {
    "benford": benford,
    "duplicate-analysis": duplicate_analysis,
    "fuzzy-entity-match": fuzzy_entity_match,
    "cross-dataset-match": cross_dataset_match,
    "duplicate-payment": duplicate_payment,
}


def _salt():
    """The project's masking key, or None (then values are withheld, never printed)."""
    try:
        return masking.load_salt()
    except masking.MaskSaltUnavailable:
        return None


def _mode_info(protected) -> dict:
    info = {"values": protected.mode}
    if protected.note:
        info["values_note"] = protected.note
    return info


def _mask_groups(groups: list, column: str, args) -> tuple[list, dict]:
    """Group rows for Claude with their key values masked (see core/masking.py)."""
    p = masking.protect([g["key_value"] for g in groups], column=column, salt=_salt(), reveal=args.reveal_values)
    if p.mode == "withheld":
        return [{k: v for k, v in g.items() if k != "key_value"} for g in groups], _mode_info(p)
    return [{**g, "key_value": v} for g, v in zip(groups, p.values)], _mode_info(p)


def _version(args) -> str | None:
    try:
        return base.get_test_entry(args.subtest).get("version")
    except ValueError:
        return None


def _print_refused(args, code: str, reason: str) -> None:
    print(json.dumps(present.refused(args.subtest, _version(args), code, f"{reason} Nothing was executed."),
                     separators=(",", ":")))


def _print_error(args, code: str, reason: str) -> None:
    print(json.dumps(present.failed(args.subtest, _version(args), code, reason), separators=(",", ":")))


def _emit(args, legacy: dict, build) -> None:
    """Print the compact contract result, or the previous per-method shape under --legacy-output
    (kept for the transition; `--reveal-values` only affects that shape)."""
    if args.legacy_output:
        print(json.dumps(legacy, indent=2, default=str))
        return
    try:
        print(build().to_json())
    except ContractViolation as exc:
        # The run is recorded; only its presentation failed. Say so, never print a raw fallback.
        _print_error(args, "RESULT_NOT_PRESENTABLE",
                     f"the run was recorded but its result broke the output contract: {exc}")


def _refuse(cur, args, dataset_id, reason: str, code: str = "REFUSED") -> None:
    """Audit-log a refusal and print it in the standard compact shape."""
    base.log_audit(cur, ACTOR, f"run:{args.subtest}", dataset_id, __file__, vars(args), "refused", error_text=reason)
    _print_refused(args, code, reason)


def _finish_duplicate_analysis(cur, args, entry, module, dataset, result) -> None:
    """Record a duplicate-analysis run, its finding, and its record-level evidence."""
    dataset_id = dataset.dataset_id
    fields = [args.column] + ([args.distinct_of] if args.distinct_of else [])
    limitations = entry["limitations"]
    truncated = len(result.evidence) >= args.evidence_limit
    if truncated:
        limitations += (f" Evidence links truncated at {args.evidence_limit} rows for this run; "
                        f"the flagged group counts are complete but the linked rows are not.")

    run_id = base.record_test_run(
        cur,
        test_name=module.TEST_NAME,
        test_version=module.TEST_VERSION,
        dataset_id=dataset_id,
        fields_used=fields,
        params={
            "schema": args.schema, "table": args.table, "column": args.column,
            "distinct_of": args.distinct_of, "mode": result.mode,
            "min_occurrences": args.min_occurrences,
            "exclude_placeholders": not args.include_placeholders,
            "require_digit": args.require_digit,
            "label": args.label,
        },
        records_examined=result.records_examined,
        result_count=result.flagged_keys,
        query_text=result.query_text,
        limitations=limitations,
        status="success",
    )

    target = f"{args.schema}.{args.table}.{args.column}"
    if result.flagged_keys == 0:
        classification = "OBSERVATION"
        desc = (f"No values of {target} met the duplication criteria "
                f"(mode={result.mode}, min_occurrences={args.min_occurrences}).")
    elif result.mode == "shared_identifier":
        classification = "ANOMALY"
        desc = (f"{result.flagged_keys} distinct values of {target} are each associated with "
                f"{args.min_occurrences}+ distinct values of '{args.distinct_of}' "
                f"(max {result.max_group_size}), spanning {result.flagged_rows} rows of "
                f"{result.records_examined} examined. A shared identifier is a structural "
                f"fact about the data, not evidence of wrongdoing.")
    else:
        classification = "ANOMALY"
        desc = (f"{result.flagged_keys} distinct values of {target} appear on "
                f"{args.min_occurrences}+ rows (max {result.max_group_size}), "
                f"covering {result.flagged_rows} rows of {result.records_examined} examined. "
                f"Duplication is a structural fact about the data, not evidence of wrongdoing.")

    finding_id = base.record_finding(cur, run_id, classification, desc)
    linked = base.record_evidence_links(
        cur, finding_id, args.schema, args.table, result.identity, result.evidence)

    base.log_audit(cur, ACTOR, f"run:{args.subtest}", dataset_id, __file__, vars(args), "success")

    top_groups, values_info = _mask_groups(result.top_groups[:15], args.column, args)

    legacy = ({
        "status": "success",
        "run_id": run_id,
        "dataset_id": dataset_id,
        "finding_ids": [finding_id],
        "classification": classification,
        "test_name": module.TEST_NAME,
        "test_version": module.TEST_VERSION,
        "mode": result.mode,
        "records_examined": result.records_examined,
        "keys_examined": result.keys_examined,
        "flagged_keys": result.flagged_keys,
        "flagged_rows": result.flagged_rows,
        "max_group_size": result.max_group_size,
        "placeholders_excluded_rows": result.placeholders_excluded_rows,
        "evidence_links_written": linked,
        "evidence_identity": result.identity.describe(),
        "evidence_truncated": truncated,
        "top_groups": top_groups,
        **values_info,
        "limitations": limitations,
        "query_text": result.query_text,
    })
    _emit(args, legacy, lambda: present.duplicate_result(
        MethodRef(module.TEST_NAME, module.TEST_VERSION), present.dataset_version(dataset), result,
        run_id=run_id, finding_id=finding_id, links=linked, truncated=truncated, salt=_salt(),
        params={"min_occurrences": args.min_occurrences, "exclude_placeholders": not args.include_placeholders,
                "require_digit": args.require_digit}))


def _finish_fuzzy_entity_match(cur, args, entry, module, dataset, result) -> None:
    """Record a fuzzy-entity-match run: how many identifiers still resolve to
    multiple entities once spelling variants are collapsed."""
    dataset_id = dataset.dataset_id
    limitations = entry["limitations"]
    truncated = len(result.evidence) >= args.evidence_limit
    if truncated:
        limitations += (f" Evidence links truncated at {args.evidence_limit} rows; "
                        f"group counts are complete but linked rows are not.")

    run_id = base.record_test_run(
        cur,
        test_name=module.TEST_NAME,
        test_version=module.TEST_VERSION,
        dataset_id=dataset_id,
        fields_used=[args.column, args.distinct_of],
        params={
            "schema": args.schema, "table": args.table, "column": args.column,
            "distinct_of": args.distinct_of, "min_entities": args.min_occurrences,
            "threshold": args.threshold,
            "exclude_placeholders": not args.include_placeholders,
            "require_digit": args.require_digit,
            "label": args.label,
        },
        records_examined=result.records_examined,
        result_count=result.flagged_keys,
        query_text=result.query_text,
        limitations=limitations,
        status="success",
    )

    target = f"{args.schema}.{args.table}.{args.column}"
    if result.flagged_keys == 0:
        classification = "OBSERVATION"
        desc = (f"After collapsing spelling variants (threshold {args.threshold}), no value of "
                f"{target} resolves to {args.min_occurrences}+ distinct entities of "
                f"'{args.distinct_of}'. All apparent conflicts were spelling variance.")
    else:
        classification = "ANOMALY"
        desc = (f"{result.flagged_keys} values of {target} still resolve to "
                f"{args.min_occurrences}+ distinct entities of '{args.distinct_of}' after "
                f"collapsing spelling variants at threshold {args.threshold} "
                f"(max {result.max_entities}). {result.collapsed_by_spelling} of "
                f"{result.keys_with_multiple_raw} identifiers with multiple raw name strings "
                f"were explained by spelling variance alone. A shared identifier is a "
                f"structural fact about the data, not evidence of wrongdoing.")

    finding_id = base.record_finding(cur, run_id, classification, desc)
    linked = base.record_evidence_links(
        cur, finding_id, args.schema, args.table, result.identity, result.evidence)

    base.log_audit(cur, ACTOR, f"run:{args.subtest}", dataset_id, __file__, vars(args), "success")

    top_groups, values_info = _mask_groups(result.top_groups[:15], args.column, args)

    legacy = ({
        "status": "success",
        "run_id": run_id,
        "dataset_id": dataset_id,
        "finding_ids": [finding_id],
        "classification": classification,
        "test_name": module.TEST_NAME,
        "test_version": module.TEST_VERSION,
        "threshold": result.threshold,
        "records_examined": result.records_examined,
        "keys_examined": result.keys_examined,
        "keys_with_multiple_raw_names": result.keys_with_multiple_raw,
        "flagged_keys": result.flagged_keys,
        "collapsed_by_spelling": result.collapsed_by_spelling,
        "max_entities": result.max_entities,
        "evidence_links_written": linked,
        "evidence_identity": result.identity.describe(),
        "evidence_truncated": truncated,
        "top_groups": top_groups,
        **values_info,
        "limitations": limitations,
    })
    _emit(args, legacy, lambda: present.fuzzy_result(
        MethodRef(module.TEST_NAME, module.TEST_VERSION), present.dataset_version(dataset), result,
        run_id=run_id, finding_id=finding_id, links=linked, truncated=truncated, salt=_salt(),
        params={"min_entities": args.min_occurrences, "exclude_placeholders": not args.include_placeholders,
                "require_digit": args.require_digit}))


def _finish_cross_dataset_match(cur, args, entry, module, dataset, result) -> None:
    """Record a reconciliation run between two datasets."""
    dataset_id = dataset.dataset_id
    limitations = entry["limitations"]
    truncated = len(result.evidence) >= args.evidence_limit
    if truncated:
        limitations += (f" Evidence links truncated at {args.evidence_limit} orphan rows; "
                        f"counts are complete but linked rows are not.")

    run_id = base.record_test_run(
        cur,
        test_name=module.TEST_NAME,
        test_version=module.TEST_VERSION,
        dataset_id=dataset_id,
        fields_used=[args.column, f"{args.right_table}.{args.right_column}"],
        params={
            "left": result.left, "right": result.right,
            "exclude_placeholders": not args.include_placeholders,
            "label": args.label,
        },
        records_examined=result.left_rows,
        result_count=result.only_left,
        query_text=result.query_text,
        limitations=limitations,
        status="success",
    )

    if result.only_left == 0 and result.only_right == 0:
        classification = "OBSERVATION"
        desc = (f"{result.left} and {result.right} reconcile exactly: "
                f"{result.in_both} distinct keys present in both, none unmatched either way.")
    else:
        classification = "ANOMALY" if result.only_left else "OBSERVATION"
        desc = (f"Reconciliation of {result.left} against {result.right}: "
                f"{result.in_both} keys in both, {result.only_left} only on the left "
                f"({result.left_coverage_pct}% left coverage), {result.only_right} only on the "
                f"right ({result.right_coverage_pct}% right coverage). Unmatched keys are a "
                f"completeness/integrity fact about these two extracts, not evidence of "
                f"wrongdoing.")

    finding_id = base.record_finding(cur, run_id, classification, desc)
    linked = base.record_evidence_links(
        cur, finding_id, args.schema, args.table, result.identity, result.evidence)

    base.log_audit(cur, ACTOR, f"run:{args.subtest}", dataset_id, __file__, vars(args), "success")

    salt = _salt()
    sample_left = masking.protect(result.sample_only_left[:10], column=args.column, salt=salt, reveal=args.reveal_values)
    sample_right = masking.protect(result.sample_only_right[:10], column=args.right_column, salt=salt, reveal=args.reveal_values)
    if sample_left.mode != sample_right.mode:
        # never show one side raw because the other side was unsafe: mask both, and say why
        note = sample_left.note or sample_right.note
        sample_left = masking.protect(result.sample_only_left[:10], column=args.column, salt=salt)
        sample_right = masking.protect(result.sample_only_right[:10], column=args.right_column, salt=salt)
        sample_left = masking.Protected(sample_left.values, sample_left.mode, note)
    legacy = ({
        "status": "success",
        "run_id": run_id,
        "finding_ids": [finding_id],
        "classification": classification,
        "left": result.left, "right": result.right,
        "left_rows": result.left_rows, "right_rows": result.right_rows,
        "left_distinct": result.left_distinct, "right_distinct": result.right_distinct,
        "in_both": result.in_both,
        "only_left": result.only_left, "only_right": result.only_right,
        "left_coverage_pct": result.left_coverage_pct,
        "right_coverage_pct": result.right_coverage_pct,
        "sample_only_left": sample_left.values,
        "sample_only_right": sample_right.values,
        **_mode_info(sample_left),
        "evidence_links_written": linked,
        "evidence_identity": result.identity.describe(),
        "limitations": limitations,
    })
    _emit(args, legacy, lambda: present.cross_result(
        MethodRef(module.TEST_NAME, module.TEST_VERSION), present.dataset_version(dataset), result,
        run_id=run_id, finding_id=finding_id, links=linked, truncated=truncated,
        params={"exclude_placeholders": not args.include_placeholders}))


def _finish_duplicate_payment(cur, args, entry, module, dataset, result) -> None:
    """Record a duplicate-payment run: entities paid in more invoice-groups this
    period than they have known real identities."""
    dataset_id = dataset.dataset_id
    fields = [args.column, args.period_column, args.invoice_column, args.amount_column]
    limitations = entry["limitations"]
    truncated = len(result.evidence) >= args.evidence_limit
    if truncated:
        limitations += (f" Evidence links truncated at {args.evidence_limit} rows for this run; "
                        f"the flagged group counts are complete but the linked rows are not.")

    run_id = base.record_test_run(
        cur,
        test_name=module.TEST_NAME,
        test_version=module.TEST_VERSION,
        dataset_id=dataset_id,
        fields_used=fields,
        params={
            "schema": args.schema, "table": args.table, "column": args.column,
            "period_column": args.period_column, "invoice_column": args.invoice_column,
            "amount_column": args.amount_column, "identity_table": args.identity_table,
            "identity_table_used": result.identity_table_used, "merge_map_size": result.merge_map_size,
            "min_paid_instances": args.min_occurrences, "label": args.label,
        },
        records_examined=result.records_examined,
        result_count=result.flagged_groups,
        query_text=result.query_text,
        limitations=limitations,
        status="success",
    )

    target = f"{args.schema}.{args.table}.{args.column}"
    if result.flagged_groups == 0:
        classification = "OBSERVATION"
        desc = (f"No name/period group in {target} was paid in more invoice-groups than it has "
                f"known real identities (groups examined: {result.groups_examined}).")
    else:
        classification = "ANOMALY"
        desc = (f"{result.flagged_groups} name/period group(s) in {target} were paid on more "
                f"distinct invoice numbers than their known real-identity count allows "
                f"(max {result.max_paid_instances} paid instances), covering "
                f"{result.flagged_rows} rows of {result.records_examined} examined. "
                f"Identity table used: {result.identity_table_used}. A flagged group is a "
                f"structural fact about paid-instance counts, not evidence of wrongdoing.")

    finding_id = base.record_finding(cur, run_id, classification, desc)
    linked = base.record_evidence_links(
        cur, finding_id, args.schema, args.table, result.identity, result.evidence)

    base.log_audit(cur, ACTOR, f"run:{args.subtest}", dataset_id, __file__, vars(args), "success")

    top_groups, values_info = _mask_groups(
        [{"key_value": f"{g['period']}|{g['names'][0]}", **g} for g in result.top_groups[:15]],
        args.column, args)

    legacy = ({
        "status": "success",
        "run_id": run_id,
        "dataset_id": dataset_id,
        "finding_ids": [finding_id],
        "classification": classification,
        "test_name": module.TEST_NAME,
        "test_version": module.TEST_VERSION,
        "records_examined": result.records_examined,
        "groups_examined": result.groups_examined,
        "flagged_groups": result.flagged_groups,
        "flagged_rows": result.flagged_rows,
        "max_paid_instances": result.max_paid_instances,
        "identity_table_used": result.identity_table_used,
        "merge_map_size": result.merge_map_size,
        "evidence_links_written": linked,
        "evidence_identity": result.identity.describe(),
        "evidence_truncated": truncated,
        "top_groups": top_groups,
        **values_info,
        "limitations": limitations,
        "query_text": result.query_text,
    })
    _emit(args, legacy, lambda: present.duplicate_payment_result(
        MethodRef(module.TEST_NAME, module.TEST_VERSION), present.dataset_version(dataset), result,
        run_id=run_id, finding_id=finding_id, links=linked, truncated=truncated, salt=_salt(),
        params={"min_paid_instances": args.min_occurrences, "identity_table_used": result.identity_table_used}))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", required=True)
    parser.add_argument("--subtest", required=True)
    parser.add_argument("--schema", required=True)
    parser.add_argument("--table", required=True)
    parser.add_argument("--column", required=True, help="Column to test (test-specific meaning)")
    parser.add_argument("--distinct-of", default=None,
                        help="duplicate-analysis: switch to shared-identifier mode against this column")
    parser.add_argument("--min-occurrences", type=int, default=2)
    parser.add_argument("--include-placeholders", action="store_true",
                        help="Do NOT exclude placeholder tokens such as 0/NIL/NA")
    parser.add_argument("--right-table", default=None,
                        help="cross-dataset-match: table to reconcile against")
    parser.add_argument("--right-column", default=None,
                        help="cross-dataset-match: key column in the right table")
    parser.add_argument("--right-schema", default=None,
                        help="cross-dataset-match: schema of the right table (defaults to --schema)")
    parser.add_argument("--require-digit", action="store_true",
                        help="Treat identifier values containing no digit (e.g. 'MCI', "
                             "'HOSPITAL', a surname) as free text and exclude them")
    parser.add_argument("--period-column", default=None,
                        help="duplicate-payment: billing/payment period column")
    parser.add_argument("--invoice-column", default=None,
                        help="duplicate-payment: invoice/voucher number column - the paid-instance dimension")
    parser.add_argument("--amount-column", default=None,
                        help="duplicate-payment: paid-amount column (only amount > 0 rows count)")
    parser.add_argument("--identity-table", default=None,
                        help="duplicate-payment: optional dataset with a real per-entity identifier, used as the "
                             "flagging ceiling")
    parser.add_argument("--identity-schema", default=None,
                        help="duplicate-payment: schema of --identity-table (defaults to --schema)")
    parser.add_argument("--identity-entity-column", default=None,
                        help="duplicate-payment: entity/name column on --identity-table")
    parser.add_argument("--identity-period-column", default=None,
                        help="duplicate-payment: period column on --identity-table")
    parser.add_argument("--identity-id-column", default=None,
                        help="duplicate-payment: the real per-entity identifier column on --identity-table")
    parser.add_argument("--name-merge-map", default=None,
                        help="duplicate-payment: JSON object of {collapsed_name: collapsed_name} explicitly "
                             "human-reviewed spelling-variant merges. Never auto-generate this - ask the user "
                             "to confirm each pair first")
    parser.add_argument("--evidence-limit", type=int, default=50000)
    parser.add_argument("--threshold", type=float, default=0.55,
                        help="fuzzy-entity-match: similarity threshold for treating two "
                             "names as the same entity")
    parser.add_argument("--confirmed-by", default=None,
                        help="benford: name of the person who confirmed --column is an amount. Without "
                             "it the test is declined (REQUIRES_CONFIRMATION). Pass only a name the user gave")
    parser.add_argument("--allow-unsuitable", action="store_true",
                        help="benford: run even though the column is INSUFFICIENT_DATA or NOT_APPLICABLE. "
                             "The result is capped at OBSERVATION and says so. Never overrides a confirmation")
    parser.add_argument("--legacy-output", action="store_true", default=os.environ.get("FORENSIC_LEGACY_OUTPUT") == "1",
                        help="Print the previous per-method output shape instead of the compact contract "
                             "result (transition aid; also $FORENSIC_LEGACY_OUTPUT=1). Will be removed")
    parser.add_argument("--reveal-values", action="store_true",
                        help="Legacy output only: show raw key values instead of masked references. Honoured only when "
                             "neither the column nor its values look sensitive")
    parser.add_argument("--label", default=None, help="Optional human label for this run")
    args = parser.parse_args()
    try:
        _execute(args)
    except psycopg2.Error as exc:
        # Anything the database raised that a step did not handle itself (a timeout while
        # registering a dataset or writing evidence, a dropped connection, ...): report a
        # short code and reason, never a traceback.
        _report_db_failure(args, exc)


def _report_db_failure(args, exc: psycopg2.Error) -> None:
    code, reason = describe_db_error(exc)
    try:
        # The failed transaction was rolled back, so record the failure in a fresh one.
        # Best effort: a logging failure must never hide the original error.
        with connect(args.database) as conn:
            base.log_audit(conn.cursor(), ACTOR, f"run:{args.subtest}", None, __file__,
                           vars(args), "error", error_text=f"{code}: {reason}")
    except Exception:
        pass
    _print_error(args, code, reason)


def _execute(args) -> None:
    entry = base.get_test_entry(args.subtest)
    if entry.get("status") != "implemented":
        _print_refused(args, "NOT_IMPLEMENTED",
                       f"Subtest '{args.subtest}' is registered as '{entry.get('status')}', not implemented.")
        return

    try:
        forensic_dsn(args.database)
        timeouts()  # a bad timeout setting is a configuration problem, refused up front
    except RuntimeError as exc:
        # Missing/unreadable project config: refuse in the same JSON shape as every
        # other refusal so the caller never has to parse a traceback.
        _print_refused(args, "BAD_CONFIGURATION", str(exc))
        return

    with connect(args.database) as conn:
        cur = conn.cursor()

        # Names from the command line (a user, or an LLM) must be safe to use as identifiers
        # before anything else happens. A refusal is audit-logged: an attempt is evidence too.
        try:
            for what, value in (
                ("--schema", args.schema), ("--table", args.table), ("--column", args.column),
                ("--distinct-of", args.distinct_of), ("--right-table", args.right_table),
                ("--right-column", args.right_column), ("--right-schema", args.right_schema),
                ("--period-column", args.period_column), ("--invoice-column", args.invoice_column),
                ("--amount-column", args.amount_column), ("--identity-table", args.identity_table),
                ("--identity-schema", args.identity_schema),
                ("--identity-entity-column", args.identity_entity_column),
                ("--identity-period-column", args.identity_period_column),
                ("--identity-id-column", args.identity_id_column),
            ):
                if value is not None:
                    check_identifier(value, what)
        except UnsafeIdentifier as exc:
            _refuse(cur, args, None, str(exc), "UNSAFE_IDENTIFIER")
            return

        identity = None
        if args.subtest in EVIDENCE_SUBTESTS:
            try:
                identity = resolve_row_identity(cur, args.schema, args.table)
            except NoRowIdentity as exc:
                # Refused BEFORE dataset registration: a refusal must not leave a permanent
                # row behind in the append-only `datasets` table.
                base.log_audit(
                    cur, ACTOR, f"run:{args.subtest}", None, __file__,
                    vars(args), "refused", error_text=str(exc),
                )
                _print_refused(args, "NO_ROW_IDENTITY", str(exc))
                return
        elif not table_exists(cur, args.schema, args.table):
            _refuse(cur, args, None, f"Table {args.schema}.{args.table} does not exist or is not visible to this role.",
                    "NO_SUCH_TABLE")
            return

        dataset = base.resolve_dataset_version(cur, args.schema, args.table, ACTOR)
        dataset_id = dataset.dataset_id

        data_type = base.column_info(cur, args.schema, args.table, args.column)
        if data_type is None:
            base.log_audit(
                cur, ACTOR, f"run:{args.subtest}", dataset_id, __file__,
                vars(args), "refused",
                error_text=f"Column '{args.column}' does not exist on {args.schema}.{args.table}",
            )
            _print_refused(args, "NO_SUCH_COLUMN", f"Column '{args.column}' does not exist on {args.schema}.{args.table}.")
            return

        if args.subtest == "benford" and data_type not in (
            "smallint", "integer", "bigint", "numeric", "real", "double precision",
        ):
            base.log_audit(
                cur, ACTOR, f"run:{args.subtest}", dataset_id, __file__,
                vars(args), "refused",
                error_text=f"Column '{args.column}' is type '{data_type}', not numeric",
            )
            _print_refused(args, "WRONG_COLUMN_TYPE",
                           f"benford requires a numeric column; '{args.column}' is '{data_type}'.")
            return

        if args.distinct_of:
            dtype = base.column_info(cur, args.schema, args.table, args.distinct_of)
            if dtype is None:
                base.log_audit(
                    cur, ACTOR, f"run:{args.subtest}", dataset_id, __file__,
                    vars(args), "refused",
                    error_text=f"Column '{args.distinct_of}' does not exist on {args.schema}.{args.table}",
                )
                _print_refused(args, "NO_SUCH_COLUMN", f"--distinct-of column '{args.distinct_of}' does not exist on "
                                                        f"{args.schema}.{args.table}.")
                return

        if args.subtest == "cross-dataset-match" and args.right_table and args.right_column:
            rschema = args.right_schema or args.schema
            if not table_exists(cur, rschema, args.right_table):
                _refuse(cur, args, dataset_id, f"Right table {rschema}.{args.right_table} does not exist or is not visible to this role.",
                        "NO_SUCH_TABLE")
                return
            if base.column_info(cur, rschema, args.right_table, args.right_column) is None:
                _refuse(cur, args, dataset_id, f"Column '{args.right_column}' does not exist on {rschema}.{args.right_table}.",
                        "NO_SUCH_COLUMN")
                return

        if args.subtest == "duplicate-payment" and args.identity_table:
            ischema = args.identity_schema or args.schema
            if not table_exists(cur, ischema, args.identity_table):
                _refuse(cur, args, dataset_id,
                        f"Identity table {ischema}.{args.identity_table} does not exist or is not visible to this role.",
                        "NO_SUCH_TABLE")
                return
            for col in (args.identity_entity_column, args.identity_period_column, args.identity_id_column):
                if col and base.column_info(cur, ischema, args.identity_table, col) is None:
                    _refuse(cur, args, dataset_id, f"Column '{col}' does not exist on {ischema}.{args.identity_table}.",
                            "NO_SUCH_COLUMN")
                    return

        module = DISPATCH[args.subtest]
        # A failed statement aborts the whole transaction. The savepoint lets us roll back just
        # the test, keep the dataset registration, and still write the failure to the audit log.
        cur.execute("SAVEPOINT test_run")
        try:
            if args.subtest == "cross-dataset-match":
                if not (args.right_table and args.right_column):
                    _print_refused(args, "MISSING_ARGUMENT",
                                   "cross-dataset-match requires --right-table and --right-column.")
                    return
                result = module.run(
                    cur, args.schema, args.table, args.column,
                    right_table=args.right_table,
                    right_column=args.right_column,
                    right_schema=args.right_schema,
                    exclude_placeholders=not args.include_placeholders,
                    evidence_limit=args.evidence_limit,
                    identity=identity,
                )
            elif args.subtest == "fuzzy-entity-match":
                if not args.distinct_of:
                    _print_refused(args, "MISSING_ARGUMENT",
                                   "fuzzy-entity-match requires --distinct-of (the name column).")
                    return
                result = module.run(
                    cur, args.schema, args.table, args.column,
                    distinct_of=args.distinct_of,
                    min_entities=args.min_occurrences,
                    threshold=args.threshold,
                    exclude_placeholders=not args.include_placeholders,
                    require_digit=args.require_digit,
                    evidence_limit=args.evidence_limit,
                    identity=identity,
                )
            elif args.subtest == "duplicate-analysis":
                result = module.run(
                    cur, args.schema, args.table, args.column,
                    distinct_of=args.distinct_of,
                    min_occurrences=args.min_occurrences,
                    exclude_placeholders=not args.include_placeholders,
                    require_digit=args.require_digit,
                    evidence_limit=args.evidence_limit,
                    identity=identity,
                )
            elif args.subtest == "duplicate-payment":
                if not (args.period_column and args.invoice_column and args.amount_column):
                    _print_refused(args, "MISSING_ARGUMENT",
                                   "duplicate-payment requires --period-column, --invoice-column and --amount-column.")
                    return
                merge_map = None
                if args.name_merge_map:
                    try:
                        merge_map = json.loads(args.name_merge_map)
                    except (TypeError, ValueError) as exc:
                        _print_refused(args, "BAD_ARGUMENT", f"--name-merge-map is not valid JSON: {exc}")
                        return
                result = module.run(
                    cur, args.schema, args.table, args.column,
                    period_column=args.period_column,
                    invoice_column=args.invoice_column,
                    amount_column=args.amount_column,
                    identity_table=args.identity_table,
                    identity_schema=args.identity_schema,
                    identity_entity_column=args.identity_entity_column,
                    identity_period_column=args.identity_period_column,
                    identity_id_column=args.identity_id_column,
                    name_merge_map=merge_map,
                    min_paid_instances=args.min_occurrences,
                    evidence_limit=args.evidence_limit,
                    identity=identity,
                )
            else:
                suitability = assess_column(cur, benford.RULESET, args.schema, args.table, args.column,
                                            role="amount", confirmed_by=args.confirmed_by)
                overridden = (not suitability.runnable and args.allow_unsuitable
                              and suitability.verdict in (Verdict.INSUFFICIENT_DATA, Verdict.NOT_APPLICABLE))
                if not suitability.runnable and not overridden:
                    declined = suitability.to_declined().to_dict()
                    if suitability.verdict is Verdict.REQUIRES_CONFIRMATION:
                        declined["next"] = "ask the user to confirm the column is an amount, then pass --confirmed-by <their name>"
                    elif suitability.verdict in (Verdict.INSUFFICIENT_DATA, Verdict.NOT_APPLICABLE):
                        declined["next"] = "do not run; --allow-unsuitable runs it as an OBSERVATION only, if the user insists"
                    base.log_audit(cur, ACTOR, f"run:{args.subtest}", dataset_id, __file__, vars(args), "declined",
                                   error_text=",".join(suitability.reason_codes))
                    print(json.dumps(declined, separators=(",", ":")))
                    return
                result = module.run(cur, args.schema, args.table, args.column)
        except Exception as exc:
            if isinstance(exc, psycopg2.Error):
                cur.execute("ROLLBACK TO SAVEPOINT test_run")
                code, reason = describe_db_error(exc)
            else:
                code, reason = "TEST_ERROR", str(exc)
            base.log_audit(
                cur, ACTOR, f"run:{args.subtest}", dataset_id, __file__,
                vars(args), "error", error_text=f"{code}: {reason}",
            )
            _print_error(args, code, reason)
            return

        if args.subtest == "duplicate-analysis":
            _finish_duplicate_analysis(cur, args, entry, module, dataset, result)
            return
        if args.subtest == "fuzzy-entity-match":
            _finish_fuzzy_entity_match(cur, args, entry, module, dataset, result)
            return
        if args.subtest == "cross-dataset-match":
            _finish_cross_dataset_match(cur, args, entry, module, dataset, result)
            return
        if args.subtest == "duplicate-payment":
            _finish_duplicate_payment(cur, args, entry, module, dataset, result)
            return

        run_id = base.record_test_run(
            cur,
            test_name=module.TEST_NAME,
            test_version=module.TEST_VERSION,
            dataset_id=dataset_id,
            fields_used=[args.column],
            params={"schema": args.schema, "table": args.table, "column": args.column},
            records_examined=result.records_examined,
            result_count=len(result.digit_counts),
            query_text=result.query_text,
            limitations=entry["limitations"],
            status="success",
        )

        findings = []
        # the full digit table is evidence: keep it with the finding, not only in Claude-facing output
        counts_txt = " First-digit counts 1-9: " + ",".join(str(result.digit_counts.get(d, 0)) for d in range(1, 10)) + "."
        if overridden:
            desc = (f"Run despite {','.join(suitability.reason_codes)} at the user's request (--allow-unsuitable). "
                    f"First-digit MAD={result.mad:.5f} (n={result.records_examined}) is reported for reference only "
                    f"and is not to be read as conformity or nonconformity." + counts_txt)
            findings.append(base.record_finding(cur, run_id, "OBSERVATION", desc))
        elif not result.reliable_sample_size:
            desc = (
                f"Only {result.records_examined} usable values in {args.schema}.{args.table}.{args.column} "
                f"(Nigrini's guidance recommends >= {benford.MIN_RELIABLE_SAMPLE} for a reliable first-digit test). "
                f"MAD/chi-square are reported but should not be relied on." + counts_txt
            )
            findings.append(base.record_finding(cur, run_id, "OBSERVATION", desc))
        elif result.conformity in ("marginally acceptable conformity", "nonconformity"):
            desc = (
                f"First-digit distribution of {args.schema}.{args.table}.{args.column} shows "
                f"{result.conformity} with Benford's Law (MAD={result.mad:.5f}, chi-square={result.chi_square:.2f}, "
                f"n={result.records_examined}). This is a statistical anomaly, not evidence of fraud on its own." + counts_txt
            )
            findings.append(base.record_finding(cur, run_id, "ANOMALY", desc))
        else:
            desc = (
                f"First-digit distribution of {args.schema}.{args.table}.{args.column} shows "
                f"{result.conformity} with Benford's Law (MAD={result.mad:.5f}, n={result.records_examined})." + counts_txt
            )
            findings.append(base.record_finding(cur, run_id, "OBSERVATION", desc))

        base.log_audit(cur, ACTOR, f"run:{args.subtest}", dataset_id, __file__, vars(args), "success")

        legacy = ({
            "status": "success",
            "run_id": run_id,
            "dataset_id": dataset_id,
            "test_name": module.TEST_NAME,
            "test_version": module.TEST_VERSION,
            "records_examined": result.records_examined,
            "digit_counts": result.digit_counts,
            "digit_observed_pct": {str(k): round(v, 5) for k, v in result.digit_observed_pct.items()},
            "digit_expected_pct": {str(k): round(v, 5) for k, v in result.digit_expected_pct.items()},
            "mad": round(result.mad, 5),
            "chi_square": round(result.chi_square, 3),
            "conformity": result.conformity,
            "reliable_sample_size": result.reliable_sample_size,
            "finding_ids": findings,
            "suitability": {"verdict": suitability.verdict.value, "why": list(suitability.reason_codes),
                            "thresholds": "unvalidated" if suitability.unvalidated else "validated",
                            **({"overridden": True} if overridden else {}), "confirmed_by": args.confirmed_by},
            "limitations": entry["limitations"],
            "query_text": result.query_text,
        })
        anomalous = (not overridden and result.reliable_sample_size
                     and result.conformity in ("marginally acceptable conformity", "nonconformity"))
        _emit(args, legacy, lambda: present.benford_result(
            MethodRef(module.TEST_NAME, module.TEST_VERSION), present.dataset_version(dataset), result, suitability,
            run_id=run_id, finding_id=findings[0], overridden=overridden, anomalous=anomalous))


if __name__ == "__main__":
    main()
