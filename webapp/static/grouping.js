/**
 * Dynamic element grouping/binning module.
 *
 * Groups elements within each base type by any available metadata field.
 * Bin count auto-adjusts based on the selected field's data distribution.
 *
 * Field types:
 *   'distinct' — categorical (color, label): bins = unique value count
 *   'numeric'  — continuous (width, area): bins via equal-width or quantile
 */

const Grouping = (() => {

  // ── Field definitions per element type ────────────────────
  // Each field: { extract, type, label }
  //   extract: element -> value (number for numeric, string for distinct)
  //   type: 'numeric' | 'distinct'
  //   label: display name in dropdown

  const FIELD_DEFS = {
    lines: {
      width:       { extract: e => e.width || 0,        type: 'numeric',  label: 'Width (pt)' },
      color:       { extract: e => rgbKey(e.color),     type: 'distinct', label: 'Stroke Color' },
      length:      { extract: e => segmentLength(e),    type: 'numeric',  label: 'Length (pt)' },
      angle:       { extract: e => segmentAngle(e),     type: 'numeric',  label: 'Angle (deg)' },
      orientation: { extract: e => segmentOrientation(e), type: 'distinct', label: 'Orientation' },
    },
    curves: {
      width:     { extract: e => e.width || 0,      type: 'numeric',  label: 'Width (pt)' },
      color:     { extract: e => rgbKey(e.color),   type: 'distinct', label: 'Stroke Color' },
      bbox_area: { extract: e => bboxArea(e),       type: 'numeric',  label: 'BBox Area (pt\u00B2)' },
      span:      { extract: e => curveSpan(e),      type: 'numeric',  label: 'Span (pt)' },
    },
    rectangles: {
      width:       { extract: e => e.width || 0,      type: 'numeric',  label: 'Stroke Width (pt)' },
      color:       { extract: e => rgbKey(e.color),   type: 'distinct', label: 'Stroke Color' },
      area:        { extract: e => rectArea(e),       type: 'numeric',  label: 'Area (pt\u00B2)' },
      aspect:      { extract: e => rectAspect(e),     type: 'numeric',  label: 'Aspect Ratio' },
      rect_width:  { extract: e => rectW(e),          type: 'numeric',  label: 'Rect Width (pt)' },
      rect_height: { extract: e => rectH(e),          type: 'numeric',  label: 'Rect Height (pt)' },
    },
    quads: {
      width: { extract: e => e.width || 0,    type: 'numeric',  label: 'Width (pt)' },
      color: { extract: e => rgbKey(e.color), type: 'distinct', label: 'Stroke Color' },
      area:  { extract: e => bboxArea(e),     type: 'numeric',  label: 'Area (pt\u00B2)' },
    },
    fills: {
      fill_color:   { extract: e => rgbKey(e.fill || e.color), type: 'distinct', label: 'Fill Color' },
      stroke_color: { extract: e => rgbKey(e.color),           type: 'distinct', label: 'Stroke Color' },
      width:        { extract: e => e.width || 0,              type: 'numeric',  label: 'Stroke Width (pt)' },
      bbox_area:    { extract: e => bboxArea(e),               type: 'numeric',  label: 'BBox Area (pt\u00B2)' },
      point_count:  { extract: e => e.points.length,           type: 'numeric',  label: 'Vertex Count' },
    },
    text: {
      font_size: { extract: e => e.font_size || 0,        type: 'numeric',  label: 'Font Size (pt)' },
      color:     { extract: e => rgbKey(e.color),          type: 'distinct', label: 'Text Color' },
      label:     { extract: e => e.label || '',            type: 'distinct', label: 'Label' },
      text_len:  { extract: e => (e.label || '').length,   type: 'numeric',  label: 'Text Length' },
      bbox_area: { extract: e => bboxArea(e),              type: 'numeric',  label: 'BBox Area (pt\u00B2)' },
    },
    images: {
      bbox_area:  { extract: e => bboxArea(e),             type: 'numeric',  label: 'Area (pt\u00B2)' },
      img_width:  { extract: e => e.img_width || 0,        type: 'numeric',  label: 'Image Width (px)' },
      img_height: { extract: e => e.img_height || 0,       type: 'numeric',  label: 'Image Height (px)' },
    },
    tables: {
      bbox_area: { extract: e => bboxArea(e),              type: 'numeric',  label: 'Area (pt\u00B2)' },
      rows:      { extract: e => e.rows || 0,              type: 'numeric',  label: 'Row Count' },
      cols:      { extract: e => e.cols || 0,              type: 'numeric',  label: 'Column Count' },
    },
  };

  const DEFAULT_FIELD = {
    lines: 'width', curves: 'width', rectangles: 'area',
    quads: 'area', fills: 'fill_color', text: 'font_size',
    images: 'bbox_area', tables: 'bbox_area',
  };

  // ── Geometry helpers ──────────────────────────────────────

  function rgbKey(c) {
    if (!c) return '0,0,0';
    return `${c[0]},${c[1]},${c[2]}`;
  }

  function bboxArea(elem) {
    const xs = elem.points.map(p => p.x);
    const ys = elem.points.map(p => p.y);
    return (Math.max(...xs) - Math.min(...xs)) * (Math.max(...ys) - Math.min(...ys));
  }

  function segmentLength(elem) {
    if (elem.points.length < 2) return 0;
    const dx = elem.points[1].x - elem.points[0].x;
    const dy = elem.points[1].y - elem.points[0].y;
    return Math.sqrt(dx * dx + dy * dy);
  }

  function segmentAngle(elem) {
    if (elem.points.length < 2) return 0;
    const dx = elem.points[1].x - elem.points[0].x;
    const dy = elem.points[1].y - elem.points[0].y;
    return Math.round(Math.atan2(dy, dx) * 180 / Math.PI);
  }

  function segmentOrientation(elem) {
    if (elem.points.length < 2) return 'other';
    const dx = Math.abs(elem.points[1].x - elem.points[0].x);
    const dy = Math.abs(elem.points[1].y - elem.points[0].y);
    if (dy < 0.5) return 'horizontal';
    if (dx < 0.5) return 'vertical';
    if (Math.abs(dx - dy) < 1.0) return 'diagonal';
    return 'other';
  }

  function curveSpan(elem) {
    if (elem.points.length < 2) return 0;
    const first = elem.points[0];
    const last = elem.points[elem.points.length - 1];
    const dx = last.x - first.x;
    const dy = last.y - first.y;
    return Math.sqrt(dx * dx + dy * dy);
  }

  function rectArea(elem) {
    const w = rectW(elem);
    const h = rectH(elem);
    return w * h;
  }

  function rectAspect(elem) {
    const w = rectW(elem);
    const h = rectH(elem);
    return h > 0 ? w / h : 0;
  }

  function rectW(elem) {
    if (elem.points.length < 3) return 0;
    return Math.abs(elem.points[1].x - elem.points[0].x);
  }

  function rectH(elem) {
    if (elem.points.length < 3) return 0;
    return Math.abs(elem.points[2].y - elem.points[0].y);
  }

  // ── Distribution analysis ─────────────────────────────────

  /**
   * Analyze the value distribution for a field across elements.
   * Returns smart defaults for bin count and slider max.
   *
   * @param {Array} elements - array of element objects
   * @param {string} baseType - group name
   * @param {string} fieldName - field key
   * @returns {{distinctCount: number, suggestedBins: number, maxBins: number, fieldType: string}}
   */
  function analyzeDistribution(elements, baseType, fieldName) {
    const def = (FIELD_DEFS[baseType] || {})[fieldName];
    if (!def || elements.length === 0) {
      return { distinctCount: 1, suggestedBins: 1, maxBins: 1, fieldType: 'numeric' };
    }

    const values = elements.map(e => def.extract(e));
    const distinct = new Set(values.map(v => String(v)));
    const distinctCount = distinct.size;

    if (def.type === 'distinct') {
      return {
        distinctCount,
        suggestedBins: distinctCount,
        maxBins: distinctCount,
        fieldType: 'distinct',
      };
    }

    // Numeric: suggest bins based on distribution spread
    const numericVals = values.map(Number).filter(v => !isNaN(v));
    if (numericVals.length === 0) {
      return { distinctCount: 1, suggestedBins: 1, maxBins: 1, fieldType: 'numeric' };
    }

    // Cap at distinct count (no point having more bins than unique values)
    const maxBins = Math.min(distinctCount, 20);

    // Sturges' rule: k = ceil(1 + log2(n))
    const sturges = Math.ceil(1 + Math.log2(numericVals.length));
    const suggestedBins = Math.max(1, Math.min(sturges, maxBins));

    return { distinctCount, suggestedBins, maxBins, fieldType: 'numeric' };
  }

  // ── Binning ───────────────────────────────────────────────

  /**
   * Bin elements into sub-groups.
   *
   * @param {Array} elements - array of element objects
   * @param {string} baseType - group name
   * @param {string} fieldName - field to bin by
   * @param {number} numBins - requested number of bins
   * @returns {Array<{label: string, range: string, indices: number[], color: {r,g,b}}>}
   */
  function bin(elements, baseType, fieldName, numBins) {
    const def = (FIELD_DEFS[baseType] || {})[fieldName];
    if (!def) {
      return [{
        label: baseType,
        range: 'all',
        indices: elements.map((_, i) => i),
        color: generateColor(0, 1),
      }];
    }

    if (def.type === 'distinct') {
      return binDistinct(elements, def.extract, numBins);
    } else {
      return binEqualWidth(elements, def.extract, numBins);
    }
  }

  function binDistinct(elements, extractor, maxBins) {
    const buckets = new Map();
    elements.forEach((elem, i) => {
      const key = String(extractor(elem));
      if (!buckets.has(key)) buckets.set(key, []);
      buckets.get(key).push(i);
    });

    const sorted = [...buckets.entries()].sort((a, b) => b[1].length - a[1].length);
    const total = Math.min(sorted.length, Math.max(1, maxBins));
    const bins = [];

    for (let i = 0; i < total; i++) {
      bins.push({
        label: sorted[i][0],
        range: sorted[i][0],
        indices: sorted[i][1],
        color: generateColor(i, total),
      });
    }

    // Merge overflow into last bin
    if (sorted.length > total && bins.length > 0) {
      const last = bins[bins.length - 1];
      for (let i = total; i < sorted.length; i++) {
        last.indices.push(...sorted[i][1]);
      }
      last.label += ` (+${sorted.length - total})`;
    }

    return bins;
  }

  function binEqualWidth(elements, extractor, numBins) {
    const values = elements.map((e, i) => ({ val: extractor(e), idx: i }));
    if (values.length === 0) return [];

    const min = Math.min(...values.map(v => v.val));
    const max = Math.max(...values.map(v => v.val));
    const distinct = new Set(values.map(v => v.val));
    const effectiveBins = Math.min(numBins, distinct.size);

    if (effectiveBins <= 1 || min === max) {
      return [{
        label: fmtNum(min),
        range: `${fmtNum(min)} - ${fmtNum(max)}`,
        indices: values.map(v => v.idx),
        color: generateColor(0, 1),
      }];
    }

    const binWidth = (max - min) / effectiveBins;
    const bins = [];

    for (let b = 0; b < effectiveBins; b++) {
      const lo = min + b * binWidth;
      const hi = b === effectiveBins - 1 ? max + 0.001 : min + (b + 1) * binWidth;
      const indices = [];
      for (const v of values) {
        if (v.val >= lo && v.val < hi) indices.push(v.idx);
      }
      if (indices.length > 0) {
        const hiLabel = b === effectiveBins - 1 ? fmtNum(max) : fmtNum(min + (b + 1) * binWidth);
        bins.push({
          label: `${fmtNum(lo)} \u2013 ${hiLabel}`,
          range: `[${fmtNum(lo)}, ${fmtNum(hi)})`,
          indices,
          color: generateColor(b, effectiveBins),
        });
      }
    }

    return bins;
  }

  function fmtNum(n) {
    if (Number.isInteger(n)) return String(n);
    if (Math.abs(n) < 1) return n.toFixed(3);
    if (Math.abs(n) < 100) return n.toFixed(2);
    return n.toFixed(1);
  }

  // ── Color generation ──────────────────────────────────────

  function generateColor(index, total) {
    const hue = (index * 360 / Math.max(total, 1) + 200) % 360;
    return hslToRgb(hue, 70, 45);
  }

  function hslToRgb(h, s, l) {
    s /= 100; l /= 100;
    const c = (1 - Math.abs(2 * l - 1)) * s;
    const x = c * (1 - Math.abs((h / 60) % 2 - 1));
    const m = l - c / 2;
    let r, g, b;
    if (h < 60) { r = c; g = x; b = 0; }
    else if (h < 120) { r = x; g = c; b = 0; }
    else if (h < 180) { r = 0; g = c; b = x; }
    else if (h < 240) { r = 0; g = x; b = c; }
    else if (h < 300) { r = x; g = 0; b = c; }
    else { r = c; g = 0; b = x; }
    return {
      r: Math.round((r + m) * 255),
      g: Math.round((g + m) * 255),
      b: Math.round((b + m) * 255),
    };
  }

  // ── Public API ────────────────────────────────────────────

  function getFields(baseType) {
    const defs = FIELD_DEFS[baseType] || {};
    return Object.entries(defs).map(([key, def]) => ({ key, label: def.label, type: def.type }));
  }

  function getDefaultField(baseType) {
    return DEFAULT_FIELD[baseType] || Object.keys(FIELD_DEFS[baseType] || {})[0];
  }

  return { bin, getFields, getDefaultField, analyzeDistribution, generateColor };
})();
