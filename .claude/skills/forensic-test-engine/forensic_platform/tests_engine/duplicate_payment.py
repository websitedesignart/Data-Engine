"""
Duplicate-payment detection across invoice-groups, with a known-identity veto.

Fills the `duplicate-payment` slot left `not_implemented` in the registry.

The anomaly this looks for is not "the same name appears in two named systems" -
that framing only fits an organisation that happens to split one population across
two labelled systems (e.g. two sections of one office, each billing its own
outsourced staff). An office with a single billing system hits the identical
anomaly a different way: the same person paid twice on two DIFFERENT INVOICE
NUMBERS for the same period, inside that one system. Both are the same underlying
fact - "this entity was paid in more invoice-groups this period than it has known
real identities" - so this test groups by (entity, period, invoice number)
regardless of how many labelled sources the caller's table actually has. The
caller is expected to hand this a single table or view that already unions
however many source systems exist (one, two, or more); `source_column` is for
evidence labelling only and plays no part in the grouping logic.

Two names that are not byte-identical are the central difficulty, and real data
forces three different answers, not two (verified on real multi-source payroll
reconciliation, synthetic names used throughout):

  1. Honorific/prefix noise ("KM"/"SMT"/"SHRI"-equivalents) and pure spacing
     differences are always safe to collapse automatically - they can never turn
     two different real people into one.
  2. Genuine spelling/transliteration variants (a dropped or swapped letter) ARE
     the same entity, but cannot be told apart algorithmically from case 3 by
     edit distance alone: two pairs with identical edit distance can be opposite
     ground truth (one pair the same person misspelled, the other pair two real
     people who happen to look similar). This was tested directly: automatic
     fuzzy clustering at a fixed threshold produced both false merges and missed
     merges on the same input set.
  3. Two different real entities can share one name outright.

Because a false merge here directly produces a false fraud accusation (unlike
fuzzy-entity-match's existing use, where a false merge only understates a lower
bound), this test refuses to guess cases 2 vs 3. It auto-collapses case 1 only
(honorifics + spacing, via `fuzzy_entity_match.canonical_flat`), and accepts an
optional, explicitly human-reviewed `name_merge_map` for case 2. Anything not
covered by either stays ungrouped - i.e. treated as potentially different
entities, the safe direction to be wrong in.

Identity veto: when an `identity_table` is supplied (a dataset carrying a real
per-entity identifier - an account number, a national ID equivalent - independent
of the payment records being checked), the number of distinct identifiers it
shows for a name/period becomes the ceiling. A name/period group is flagged only
when its paid-instance count EXCEEDS that ceiling: if the identity table shows two
different real people share a name this period, and there are only two paid
instances total (one in each invoice-group), that is consistent with two different
people, each paid once - not a duplicate. This was the precise shape of a false
positive produced by the naive version of this check (name-match-across-sources
only, no identity veto) during development, on real data.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from psycopg2 import sql

from ..core.identity import RowIdentity, resolve_row_identity
from ..core.sqlsafe import ident, norm_expr
from .fuzzy_entity_match import canonical_flat

TEST_NAME = "duplicate-payment"
TEST_VERSION = "1.0.0"


@dataclass
class DuplicatePaymentResult:
    records_examined: int
    groups_examined: int
    flagged_groups: int
    flagged_rows: int
    max_paid_instances: int
    identity_table_used: bool
    merge_map_size: int
    top_groups: list = field(default_factory=list)
    evidence: list = field(default_factory=list)  # tuples of `identity` column values
    query_text: str = ""
    identity: RowIdentity | None = None


def canon_key(name: str, merge_map: dict[str, str] | None) -> str:
    """Case 1 (honorific + spacing) collapsed automatically; case 2 (genuine
    spelling variants) collapsed only via an explicit, human-reviewed entry in
    merge_map. Anything else stays as its own key - see module docstring."""
    key = canonical_flat(name)
    if merge_map:
        key = merge_map.get(key, key)
    return key


def build_identity_ceiling(identity_rows, merge_map: dict[str, str] | None) -> dict[tuple[str, str], int]:
    """(period, canon_key) -> count of distinct real identifiers the identity
    table shows for that name/period. This is the ceiling a paid-instance count
    is compared against."""
    seen: dict[tuple[str, str], set[str]] = {}
    for period, name, id_value in identity_rows:
        if name is None or period is None or id_value is None:
            continue
        key = (str(period), canon_key(name, merge_map))
        seen.setdefault(key, set()).add(str(id_value))
    return {k: len(v) for k, v in seen.items()}


def analyze(
    payment_rows,
    identity_ceiling: dict[tuple[str, str], int] | None,
    merge_map: dict[str, str] | None,
    min_paid_instances: int = 2,
):
    """Pure grouping/decision logic, independent of SQL, so it is directly
    testable. `payment_rows` is an iterable of
    (period, name, invoice_no, pk_values, display_name) tuples already filtered
    by the caller to amount > 0 and non-null period/name.

    Grouping key is (period, canon_key(name)) - i.e. entity x period - never
    entity alone (the same name in a different period is not a duplicate signal)
    and never including the invoice number (that is exactly the dimension being
    counted, not grouped away).
    """
    groups: dict[tuple[str, str], dict] = {}
    for period, name, invoice_no, pk_values, display_name in payment_rows:
        if name is None or period is None:
            continue
        key = (str(period), canon_key(name, merge_map))
        g = groups.setdefault(key, {
            "period": str(period), "names": set(), "invoices": set(), "rows": [],
        })
        g["names"].add(display_name or name)
        if invoice_no is not None:
            g["invoices"].add(str(invoice_no))
        g["rows"].append(pk_values)

    out = []
    for (period, ck), g in groups.items():
        paid_instances = len(g["invoices"]) or len(g["rows"])
        known = identity_ceiling.get((period, ck)) if identity_ceiling is not None else None
        if known is not None:
            flagged = paid_instances > known
            basis = (f"Identity table shows {known} distinct real identifier(s) for this name/period; "
                     f"{paid_instances} paid invoice(s) found.")
            if not flagged and known > 1:
                basis += " Not flagged: consistent with multiple real people, not a duplicate."
        else:
            flagged = paid_instances >= min_paid_instances
            basis = (f"No identity table supplied - {paid_instances} paid invoice(s) found for one name/period "
                     f"(naive threshold {min_paid_instances}); cannot rule out two different people sharing a name.")
        out.append({
            "period": period, "canon_key": ck, "names": sorted(g["names"]),
            "paid_instances": paid_instances, "known_entities": known,
            "flagged": flagged, "basis": basis, "rows": g["rows"],
        })

    out.sort(key=lambda r: (-int(r["flagged"]), -r["paid_instances"], r["period"], r["canon_key"]))
    return out


def run(
    cur,
    schema: str,
    table: str,
    entity_column: str,
    period_column: str,
    invoice_column: str,
    amount_column: str,
    identity_table: str | None = None,
    identity_schema: str | None = None,
    identity_entity_column: str | None = None,
    identity_period_column: str | None = None,
    identity_id_column: str | None = None,
    name_merge_map: dict[str, str] | None = None,
    min_paid_instances: int = 2,
    evidence_limit: int = 50000,
    identity: RowIdentity | None = None,
) -> DuplicatePaymentResult:
    """`table` must already be a single table or view unioning however many
    source systems exist - this test does not know or care how many there are
    (see module docstring). `identity_table`, when supplied, must carry a real
    per-entity identifier independent of the payment rows being checked."""
    identity = identity or resolve_row_identity(cur, schema, table)
    tbl = ident(schema, table)
    entity_c = ident(entity_column)
    period_c = ident(period_column)
    invoice_c = ident(invoice_column)
    amount_c = ident(amount_column)

    cur.execute(sql.SQL("SELECT count(*) FROM {}").format(tbl))
    records_examined = cur.fetchone()[0]

    pk = identity.select_list()
    composed = sql.SQL(
        "SELECT {period} AS period, {entity} AS entity, {invoice} AS invoice, {pk} AS pk, {entity} AS display_name\n"
        "FROM {t}\n"
        "WHERE {entity} IS NOT NULL AND {period} IS NOT NULL\n"
        "  AND {amount} IS NOT NULL AND {amount} > 0"
    ).format(period=period_c, entity=entity_c, invoice=invoice_c, amount=amount_c, pk=pk, t=tbl)
    query = composed.as_string(cur)  # reproducible query text, stored and reported with the result
    cur.execute(composed)
    n_pk = len(identity.columns)
    payment_rows = [
        (r[0], r[1], r[2], tuple(r[3:3 + n_pk]), r[3 + n_pk])
        for r in cur.fetchall()
    ]

    identity_ceiling = None
    identity_table_used = False
    if identity_table and identity_entity_column and identity_period_column and identity_id_column:
        identity_table_used = True
        ischema = identity_schema or schema
        itbl = ident(ischema, identity_table)
        ic_entity = ident(identity_entity_column)
        ic_period = ident(identity_period_column)
        ic_id = ident(identity_id_column)
        cur.execute(
            sql.SQL(
                "SELECT {period}, {entity}, {idc} FROM {t} "
                "WHERE {entity} IS NOT NULL AND {period} IS NOT NULL AND {idc} IS NOT NULL"
            ).format(period=ic_period, entity=ic_entity, idc=ic_id, t=itbl)
        )
        identity_ceiling = build_identity_ceiling(cur.fetchall(), name_merge_map)

    groups = analyze(payment_rows, identity_ceiling, name_merge_map, min_paid_instances)
    flagged = [g for g in groups if g["flagged"]]
    flagged_rows = sum(len(g["rows"]) for g in flagged)
    max_paid = max((g["paid_instances"] for g in groups), default=0)

    evidence: list = []
    for g in flagged:
        evidence.extend(g["rows"])
        if len(evidence) >= evidence_limit:
            evidence = evidence[:evidence_limit]
            break

    top_groups = [
        {"period": g["period"], "names": g["names"], "paid_instances": g["paid_instances"],
         "known_entities": g["known_entities"], "basis": g["basis"]}
        for g in flagged[:100]
    ]

    return DuplicatePaymentResult(
        records_examined=records_examined,
        groups_examined=len(groups),
        flagged_groups=len(flagged),
        flagged_rows=flagged_rows,
        max_paid_instances=max_paid,
        identity_table_used=identity_table_used,
        merge_map_size=len(name_merge_map) if name_merge_map else 0,
        top_groups=top_groups,
        evidence=evidence,
        query_text=query,
        identity=identity,
    )
