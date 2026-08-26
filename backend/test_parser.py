"""
Quick smoke-test for Stage 2 (parser.py).
Run with:  python test_parser.py
No external dependencies required.
"""

import sys
import os

# Allow running from the repo root as well as from backend/
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "backend"))

from parser import parse_source

EXAMPLE_SOURCE = """\
def calculate_discount(price, discount_percent):
    if discount_percent > 100:
        raise ValueError("Discount cannot exceed 100%")
    final_price = price - (price * discount_percent / 100)
    return final_price
"""

SAFE_DIVIDE_SOURCE = """\
def safe_divide(a, b):
    try:
        result = a / b
    except ZeroDivisionError:
        return None
    return result
"""


def print_function_info(fn):
    print("=" * 60)
    print(f"name        : {fn.name}")
    print(f"signature   : {fn.signature}")
    print(f"docstring   : {fn.docstring!r}")
    print(f"conditions  : {fn.conditions}")
    print(f"raises      : {fn.raises}")
    print(f"returns     : {fn.returns}")
    print(f"catches     : {fn.catches}")
    print(f"logic_summary: {fn.logic_summary()}")
    print(f"flags       : {fn.flags}")
    print(f"source (first 80 chars): {fn.source[:80]!r}")
    print()


def main():
    # --- Test 1: calculate_discount ---
    functions = parse_source(EXAMPLE_SOURCE)

    if not functions:
        print("ERROR: no functions were extracted.")
        sys.exit(1)

    for fn in functions:
        print_function_info(fn)

    fn = functions[0]
    assert fn.name == "calculate_discount", f"Wrong name: {fn.name}"
    assert fn.conditions == ["discount_percent > 100"], \
        f"Wrong conditions: {fn.conditions}"
    assert fn.raises == ["ValueError('Discount cannot exceed 100%')"], \
        f"Wrong raises: {fn.raises}"
    assert fn.returns == ["final_price"], \
        f"Wrong returns: {fn.returns}"
    assert any("price" in flag for flag in fn.flags), \
        f"Expected flag for 'price' not found in: {fn.flags}"

    # --- Test 2: safe_divide ---
    functions2 = parse_source(SAFE_DIVIDE_SOURCE)

    if not functions2:
        print("ERROR: no functions were extracted from SAFE_DIVIDE_SOURCE.")
        sys.exit(1)

    for fn2 in functions2:
        print_function_info(fn2)

    fn2 = functions2[0]
    assert fn2.name == "safe_divide", f"Wrong name: {fn2.name}"
    assert fn2.catches == ["ZeroDivisionError"], \
        f"Wrong catches: {fn2.catches}"
    assert fn2.conditions == [], \
        f"Wrong conditions: {fn2.conditions}"
    assert fn2.raises == [], \
        f"Wrong raises: {fn2.raises}"

    print("All assertions passed.")


if __name__ == "__main__":
    main()
