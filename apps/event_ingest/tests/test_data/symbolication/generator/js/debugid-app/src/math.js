export function divide(numerator, denominator) {
  if (denominator === 0) {
    throw new RangeError("Cannot divide " + numerator + " by zero");
  }
  return numerator / denominator;
}

export function average(values) {
  const total = values.reduce((sum, value) => sum + value, 0);
  return divide(total, values.length);
}
