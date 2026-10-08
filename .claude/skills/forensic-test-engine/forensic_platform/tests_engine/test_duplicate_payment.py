"""
Assertion tests for duplicate_payment. Run directly:

    .venv\\Scripts\\python.exe forensic_platform\\tests_engine\\test_duplicate_payment.py

All names are SYNTHETIC fixtures, invented for this test only.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from forensic_platform.tests_engine.duplicate_payment import (  # noqa: E402
    analyze, build_identity_ceiling, canon_key,
)

failures = 0


def check(label, condition, detail=""):
    global failures
    status = "PASS" if condition else "FAIL"
    if not condition:
        failures += 1
    print(f"  [{status}] {label}{(' - ' + detail) if detail else ''}")


print("=== canon_key: automatic collapsing (honorific + spacing only) ===")
check("honorific prefix collapses", canon_key("KM JYOTI DEVI", None) == canon_key("JYOTI DEVI", None))
check("spacing-only difference collapses", canon_key("RAM KUMAR", None) == canon_key("RAMKUMAR", None))
check("different names stay different",
      canon_key("RAM KUMAR", None) != canon_key("SHYAM KUMAR", None))
check("spelling variant does NOT auto-collapse without a merge map",
      canon_key("TABREJ AHAMD", None) != canon_key("TABREJ AHMAD", None))
check("spelling variant DOES collapse with an explicit merge map entry",
      canon_key("TABREJ AHAMD", {"TABREJAHAMD": "TABREJAHMAD"}) == canon_key("TABREJ AHMAD", None))

print("\n=== build_identity_ceiling ===")
identity_rows = [
    ("Jan 2030", "RAM KUMAR", "ID-001"),
    ("Jan 2030", "RAM KUMAR", "ID-002"),  # two different real people, same name, same period
    ("Jan 2030", "SHYAM KUMAR", "ID-003"),
]
ceiling = build_identity_ceiling(identity_rows, None)
check("two distinct ids under one name/period -> ceiling 2",
      ceiling[("Jan 2030", canon_key("RAM KUMAR", None))] == 2)
check("single id under another name/period -> ceiling 1",
      ceiling[("Jan 2030", canon_key("SHYAM KUMAR", None))] == 1)

print("\n=== analyze: the core false-positive this test exists to prevent ===")
# Two different real people named RAM KUMAR this period (known from the identity table).
# One is paid once via invoice-group A, the other once via invoice-group B.
# Total paid instances (2) does not exceed known real identities (2) -> must NOT be flagged.
rows_not_duplicate = [
    ("Jan 2030", "RAM KUMAR", "INV-A-1", (1,), "RAM KUMAR"),
    ("Jan 2030", "RAM KUMAR", "INV-B-1", (2,), "RAM KUMAR"),
]
groups = analyze(rows_not_duplicate, ceiling, None)
g = next(g for g in groups if g["canon_key"] == canon_key("RAM KUMAR", None))
check("2 instances == 2 known real people -> NOT flagged", g["flagged"] is False, g["basis"])

# Same shape, but a THIRD paid instance appears - now exceeds the known ceiling of 2.
rows_duplicate = rows_not_duplicate + [("Jan 2030", "RAM KUMAR", "INV-A-2", (3,), "RAM KUMAR")]
groups2 = analyze(rows_duplicate, ceiling, None)
g2 = next(g for g in groups2 if g["canon_key"] == canon_key("RAM KUMAR", None))
check("3 instances > 2 known real people -> flagged", g2["flagged"] is True, g2["basis"])

print("\n=== analyze: without an identity table (naive fallback) ===")
rows_no_identity = [
    ("Feb 2030", "ANIL VERMA", "INV-X-1", (10,), "ANIL VERMA"),
    ("Feb 2030", "ANIL VERMA", "INV-Y-1", (11,), "ANIL VERMA"),
]
groups3 = analyze(rows_no_identity, None, None, min_paid_instances=2)
g3 = groups3[0]
check("no identity table: 2 instances >= default threshold 2 -> flagged (naive)", g3["flagged"] is True)
check("known_entities is None when no identity table supplied", g3["known_entities"] is None)

print("\n=== analyze: period isolation (same name, different periods, never merged) ===")
rows_cross_period = [
    ("Jan 2030", "SUNIL RAO", "INV-1", (20,), "SUNIL RAO"),
    ("Feb 2030", "SUNIL RAO", "INV-2", (21,), "SUNIL RAO"),
]
groups4 = analyze(rows_cross_period, None, None, min_paid_instances=2)
check("same name, two different periods -> two separate groups, not one", len(groups4) == 2)

print(f"\n{'ALL PASSED' if failures == 0 else f'{failures} FAILURE(S)'}")
sys.exit(1 if failures else 0)
