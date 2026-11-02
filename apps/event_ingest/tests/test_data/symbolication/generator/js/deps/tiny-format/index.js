// A stand-in third-party dependency, so the bundle has a node_modules source.
export function formatLabel(label, value) {
  if (value === undefined) {
    throw new TypeError("formatLabel: value is required for " + label);
  }
  return label + ": " + String(value);
}
