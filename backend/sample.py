def safe_divide(a, b):
    try:
        result = a / b
    except ZeroDivisionError:
        return None
    return result