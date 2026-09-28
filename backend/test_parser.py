"""Tests for parser.py — deterministic AST fact extraction for Python functions."""

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


def test_extracts_conditions_raises_returns_and_flags():
    fn = parse_source(EXAMPLE_SOURCE)[0]
    assert fn.name == "calculate_discount"
    assert fn.signature == "calculate_discount(price, discount_percent)"
    assert fn.conditions == ["discount_percent > 100"]
    assert fn.raises == ["ValueError('Discount cannot exceed 100%')"]
    assert fn.returns == ["final_price"]
    assert any("price" in flag for flag in fn.flags)
    assert "Raises:" in fn.logic_summary()


def test_extracts_caught_exceptions():
    fn = parse_source(SAFE_DIVIDE_SOURCE)[0]
    assert fn.name == "safe_divide"
    assert fn.catches == ["ZeroDivisionError"]
    assert fn.conditions == []
    assert fn.raises == []


def test_bare_except_is_flagged():
    fn = parse_source("def f():\n    try:\n        pass\n    except:\n        pass\n")[0]
    assert "bare except" in fn.catches
    assert any("bare except" in flag for flag in fn.flags)


def test_signature_includes_varargs_and_kwonly():
    fn = parse_source("def g(a, *args, key=1, **kw):\n    return a\n")[0]
    assert fn.signature == "g(a, *args, key, **kw)"
