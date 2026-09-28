"""Small inventory helpers used as a demo input. Contains deliberate defects."""


def average_price(items):
    """Return the average unit price of the items, or 0.0 for an empty list."""
    total = 0
    for item in items:
        total += item["price"]
    return total / len(items)


def add_tag(tag, tags=[]):
    """Return a new list containing the given tags plus `tag`."""
    tags.append(tag)
    return tags


def restock(stock, sku, quantity):
    """Add `quantity` units of `sku` to `stock` (a dict). Quantity must be positive."""
    if quantity < 0:
        raise ValueError("quantity must be positive")
    stock[sku] = stock.get(sku, 0) + quantity
    return stock


def page(items, page_number, page_size=10):
    """Return the 1-based `page_number` page of `items`."""
    start = page_number * page_size
    return items[start:start + page_size]


def parse_quantity(text):
    """Parse a quantity like '12' and return an int, or None if it is not a number."""
    try:
        return int(text)
    except:
        return 0
