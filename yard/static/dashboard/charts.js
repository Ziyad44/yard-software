function numberOrZero(value) {
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : 0;
}

export function renderLineChart(
  svgId,
  values,
  { color = "#31a7b5", maxHint = null, xValues = [], yLabel = "" } = {},
) {
  const svg = document.getElementById(svgId);
  if (!svg) {
    return;
  }

  const width = 320;
  const height = 120;
  const margin = { top: 8, right: 8, bottom: 18, left: 32 };
  const usableWidth = width - margin.left - margin.right;
  const usableHeight = height - margin.top - margin.bottom;
  const safeValues = (Array.isArray(values) && values.length ? values : [0]).map(numberOrZero);
  const maxValue = Math.max(numberOrZero(maxHint), ...safeValues, 1);
  const stepX = safeValues.length > 1 ? usableWidth / (safeValues.length - 1) : usableWidth;

  const points = safeValues
    .map((value, index) => {
      const x = margin.left + index * stepX;
      const y = margin.top + usableHeight - (value / maxValue) * usableHeight;
      return `${x.toFixed(2)},${y.toFixed(2)}`;
    })
    .join(" ");

  const tickCount = 4;
  const yTicks = Array.from({ length: tickCount + 1 }, (_, index) => {
    const ratio = index / tickCount;
    const y = margin.top + ratio * usableHeight;
    const value = maxValue * (1 - ratio);
    return [
      `<line x1="${margin.left}" y1="${y}" x2="${width - margin.right}" y2="${y}" stroke="var(--line)" stroke-width="1" />`,
      `<text x="${margin.left - 4}" y="${y + 3}" text-anchor="end" font-size="8" fill="var(--muted)">${value.toFixed(0)}</text>`,
    ].join("");
  }).join("");

  const xStart = Array.isArray(xValues) && xValues.length ? xValues[0] : 0;
  const xEnd = Array.isArray(xValues) && xValues.length ? xValues[xValues.length - 1] : safeValues.length - 1;

  svg.innerHTML = `
    ${yTicks}
    <polyline points="${points}" fill="none" stroke="${color}" stroke-width="2.4" />
    <line x1="${margin.left}" y1="${height - margin.bottom}" x2="${width - margin.right}" y2="${height - margin.bottom}" stroke="var(--line)" stroke-width="1" />
    <line x1="${margin.left}" y1="${margin.top}" x2="${margin.left}" y2="${height - margin.bottom}" stroke="var(--line)" stroke-width="1" />
    <text x="${margin.left}" y="${height - 4}" font-size="8" fill="var(--muted)">t=${xStart}</text>
    <text x="${width - margin.right}" y="${height - 4}" text-anchor="end" font-size="8" fill="var(--muted)">t=${xEnd}</text>
    <text x="${margin.left + 2}" y="${margin.top + 8}" font-size="8" fill="var(--muted)">${yLabel}</text>
  `;
}
