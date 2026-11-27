export function findWidget(node, name) {
  return node.widgets?.find((widget) => widget?.name === name);
}

export function isLinked(node, name) {
  return node.inputs?.some((input) => input.name === name && input.link != null);
}

export function modelEntry(table, key) {
  return table && Object.hasOwn(table, key) ? table[key] : undefined;
}

export function readModelMetadata(nodeData) {
  const [values, options] = nodeData?.input?.required?.model_type ?? [];
  return {
    values: Array.isArray(values) ? values : [],
    inputs: { ...nodeData?.input?.required, ...nodeData?.input?.optional },
    labels: options?.xinbao_model_labels,
    aliases: options?.xinbao_model_aliases,
    image: options?.xinbao_image_model_capabilities,
    video: options?.xinbao_video_capabilities,
  };
}

export function canonicalModel(metadata, value) {
  const id = String(value ?? "").trim().toLowerCase();
  return modelEntry(metadata?.aliases, id) ?? id;
}

export function setWidgetValue(widget, value) {
  if (!widget || widget.value === value) return;
  widget.value = value;
  if (widget.inputEl) widget.inputEl.value = value;
  if (widget.element) widget.element.value = value;
  if (widget.domEl) widget.domEl.value = value;
}

export function updateCombo(node, name, values, defaultValue) {
  const widget = findWidget(node, name);
  if (!widget || !Array.isArray(values) || !values.length || isLinked(node, name)) return;
  widget.options ??= {};
  widget.options.values = [...values];
  if (defaultValue !== undefined) {
    const current = String(widget.value).toLowerCase();
    const selected = values.find((option) => option.toLowerCase() === current);
    setWidgetValue(widget, selected ?? (values.includes(defaultValue) ? defaultValue : values[0]));
  }
  return widget.value;
}
