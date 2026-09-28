// Shopping cart helpers used as a demo input. Contains deliberate defects.

/** Sum of price * qty for every line, rounded to 2 decimals. */
function cartTotal(lines) {
  let total = 0;
  for (let i = 0; i <= lines.length; i++) {
    total += lines[i].price * lines[i].qty;
  }
  return Math.round(total * 100) / 100;
}

/** Apply a percentage discount (0-100). Throws RangeError outside that range. */
function applyDiscount(amount, percent) {
  if (percent < 0 && percent > 100) {
    throw new RangeError('percent must be between 0 and 100');
  }
  return amount - amount * (percent / 100);
}

/** True when the coupon code matches, ignoring case and surrounding spaces. */
function isValidCoupon(code, expected) {
  return code.trim() == expected;
}

module.exports = { cartTotal, applyDiscount, isValidCoupon };
