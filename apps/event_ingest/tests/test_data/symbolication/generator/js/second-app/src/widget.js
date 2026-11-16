function renderWidget(element, options) {
  const settings = Object.assign({ size: "small" }, options);
  if (!element) {
    throw new Error("renderWidget needs an element for " + settings.size);
  }
  element.textContent = settings.size;
  return element;
}

export function mountAll(elements) {
  return elements.map((element) => renderWidget(element, { size: "large" }));
}

globalThis.mountAll = mountAll;
