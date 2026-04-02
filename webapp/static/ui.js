/**
 * Sidebar UI module.
 *
 * Builds the checkbox tree from grouped element data,
 * handles group/subgroup/element toggling, and manages
 * the grouping controls panel.
 */

const UI = (() => {
  let onVisibilityChange = null;
  let onRegroupRequest = null;

  // Current grouping state per base type
  const groupingState = {};

  function init(callbacks) {
    onVisibilityChange = callbacks.onVisibilityChange;
    onRegroupRequest = callbacks.onRegroupRequest;
  }

  // ── DOM helpers (no innerHTML) ───────────────────────────

  function el(tag, attrs, children) {
    const node = document.createElement(tag);
    if (attrs) {
      for (const [k, v] of Object.entries(attrs)) {
        if (k === 'class') node.className = v;
        else if (k === 'style') Object.assign(node.style, v);
        else if (k.startsWith('data-')) node.setAttribute(k, v);
        else node[k] = v;
      }
    }
    if (children) {
      for (const c of children) {
        if (typeof c === 'string') node.appendChild(document.createTextNode(c));
        else if (c) node.appendChild(c);
      }
    }
    return node;
  }

  // ── Build tree ──────────────────────────────────────────

  function buildTree(groupedData, visibility) {
    const tree = document.getElementById('group-tree');
    tree.textContent = '';

    for (const group of groupedData) {
      tree.appendChild(createGroupNode(group, visibility));
    }

    updateAllCheckStates(groupedData, visibility);
  }

  function createGroupNode(group, visibility) {
    const totalCount = group.subgroups.reduce((s, sg) => s + sg.globalIndices.length, 0);

    const toggle = el('span', { class: 'group-toggle', textContent: '\u25B6' });
    const label = el('span', { class: 'group-label', textContent: group.baseType });
    const count = el('span', { class: 'group-count', textContent: String(totalCount) });
    const check = el('input', { type: 'checkbox', class: 'group-check', checked: true });

    const header = el('div', { class: 'group-header' }, [toggle, label, count, check]);
    const subgroupList = el('div', { class: 'subgroup-list' });

    header.addEventListener('click', (e) => {
      if (e.target === check) return;
      const isOpen = subgroupList.classList.toggle('open');
      toggle.textContent = isOpen ? '\u25BC' : '\u25B6';
    });

    check.addEventListener('change', () => {
      const on = check.checked;
      check.indeterminate = false;
      for (const sg of group.subgroups) {
        for (const idx of sg.globalIndices) visibility[idx] = on ? 1 : 0;
      }
      subgroupList.querySelectorAll('.subgroup-check').forEach(cb => { cb.checked = on; cb.indeterminate = false; });
      subgroupList.querySelectorAll('.element-check').forEach(cb => { cb.checked = on; });
      if (onVisibilityChange) onVisibilityChange();
    });

    for (const sg of group.subgroups) {
      subgroupList.appendChild(createSubgroupNode(sg, group, visibility, check));
    }

    const container = el('div', { class: 'group-node' }, [header, subgroupList]);
    container.dataset.baseType = group.baseType;
    return container;
  }

  function createSubgroupNode(sg, group, visibility, parentCheck) {
    const swatchColor = `rgb(${sg.color.r}, ${sg.color.g}, ${sg.color.b})`;

    const toggle = el('span', { class: 'subgroup-toggle', textContent: '\u25B6' });
    const swatch = el('span', { class: 'subgroup-swatch', style: { background: swatchColor } });
    const label = el('span', { class: 'subgroup-label', textContent: sg.label });
    const count = el('span', { class: 'subgroup-count', textContent: String(sg.globalIndices.length) });
    const check = el('input', { type: 'checkbox', class: 'subgroup-check', checked: true });

    const header = el('div', { class: 'subgroup-header' }, [toggle, swatch, label, count, check]);
    const elemList = el('div', { class: 'element-list' });

    header.addEventListener('click', (e) => {
      if (e.target === check) return;
      const isOpen = elemList.classList.toggle('open');
      toggle.textContent = isOpen ? '\u25BC' : '\u25B6';
      if (isOpen && elemList.children.length === 0) {
        populateElementList(elemList, sg, visibility, check, parentCheck, group);
      }
    });

    check.addEventListener('change', () => {
      const on = check.checked;
      check.indeterminate = false;
      for (const idx of sg.globalIndices) visibility[idx] = on ? 1 : 0;
      elemList.querySelectorAll('.element-check').forEach(cb => { cb.checked = on; });
      updateParentCheckState(parentCheck, group, visibility);
      if (onVisibilityChange) onVisibilityChange();
    });

    return el('div', { class: 'subgroup-node' }, [header, elemList]);
  }

  function populateElementList(elemList, sg, visibility, sgCheck, parentCheck, group) {
    const MAX_DIRECT = 500;

    if (sg.globalIndices.length <= MAX_DIRECT) {
      for (const idx of sg.globalIndices) {
        elemList.appendChild(createElementRow(idx, sg, visibility, sgCheck, parentCheck, group));
      }
    } else {
      const ROW_H = 22;
      const viewportH = 200;
      const totalH = sg.globalIndices.length * ROW_H;

      elemList.style.height = viewportH + 'px';
      elemList.style.overflow = 'auto';
      elemList.style.position = 'relative';

      const spacer = el('div', { style: { height: totalH + 'px', position: 'relative' } });
      elemList.appendChild(spacer);

      const renderVisible = () => {
        const scrollTop = elemList.scrollTop;
        const startIdx = Math.floor(scrollTop / ROW_H);
        const endIdx = Math.min(startIdx + Math.ceil(viewportH / ROW_H) + 2, sg.globalIndices.length);

        spacer.querySelectorAll('.element-row').forEach(r => r.remove());

        for (let i = startIdx; i < endIdx; i++) {
          const row = createElementRow(sg.globalIndices[i], sg, visibility, sgCheck, parentCheck, group);
          row.style.position = 'absolute';
          row.style.top = (i * ROW_H) + 'px';
          row.style.width = '100%';
          spacer.appendChild(row);
        }
      };

      elemList.addEventListener('scroll', renderVisible);
      renderVisible();
    }
  }

  function createElementRow(globalIdx, sg, visibility, sgCheck, parentCheck, group) {
    const swatchColor = `rgb(${sg.color.r}, ${sg.color.g}, ${sg.color.b})`;
    const elem = App.getElement(globalIdx);
    let info = `#${globalIdx}`;
    if (elem) {
      if (elem.width !== undefined && elem.width !== null) info += ` w:${elem.width}`;
      if (elem.label) info += ` "${elem.label}"`;
      if (elem.fill) info += ` fill:[${elem.fill}]`;
    }

    const swatch = el('span', { class: 'element-swatch', style: { background: swatchColor } });
    const infoSpan = el('span', { class: 'element-info', title: info, textContent: info });
    const check = el('input', { type: 'checkbox', class: 'element-check', checked: !!visibility[globalIdx] });

    check.addEventListener('change', () => {
      visibility[globalIdx] = check.checked ? 1 : 0;
      updateSubgroupCheckState(sgCheck, sg, visibility);
      updateParentCheckState(parentCheck, group, visibility);
      if (onVisibilityChange) onVisibilityChange();
    });

    return el('div', { class: 'element-row' }, [swatch, infoSpan, check]);
  }

  // ── Check state helpers ─────────────────────────────────

  function updateSubgroupCheckState(sgCheck, sg, visibility) {
    let checked = 0;
    for (const idx of sg.globalIndices) { if (visibility[idx]) checked++; }
    const total = sg.globalIndices.length;
    sgCheck.checked = checked === total;
    sgCheck.indeterminate = checked > 0 && checked < total;
  }

  function updateParentCheckState(parentCheck, group, visibility) {
    let total = 0, checked = 0;
    for (const sg of group.subgroups) {
      for (const idx of sg.globalIndices) { total++; if (visibility[idx]) checked++; }
    }
    parentCheck.checked = checked === total;
    parentCheck.indeterminate = checked > 0 && checked < total;
  }

  function updateAllCheckStates(groupedData, visibility) {
    const nodes = document.querySelectorAll('.group-node');
    nodes.forEach((node, gi) => {
      const group = groupedData[gi];
      if (!group) return;
      updateParentCheckState(node.querySelector('.group-check'), group, visibility);
      const sgChecks = node.querySelectorAll('.subgroup-check');
      group.subgroups.forEach((sg, si) => {
        if (sgChecks[si]) updateSubgroupCheckState(sgChecks[si], sg, visibility);
      });
    });
  }

  // ── Grouping controls ──────────────────────────────────

  /**
   * Build grouping controls with field-aware dropdowns and auto-adjusting bins.
   * @param {Array<string>} baseTypes
   * @param {Function} getElements - (baseType) => element array, for distribution analysis
   */
  function buildGroupingControls(baseTypes, getElements) {
    const panel = document.getElementById('grouping-panels');
    panel.textContent = '';

    for (const bt of baseTypes) {
      const fields = Grouping.getFields(bt);
      if (fields.length === 0) continue;

      const currentField = groupingState[bt]?.dimension || Grouping.getDefaultField(bt);
      const elements = getElements ? getElements(bt) : [];
      const dist = Grouping.analyzeDistribution(elements, bt, currentField);
      const currentBins = groupingState[bt]?.bins || dist.suggestedBins;

      const title = el('div', { class: 'grouping-panel-title', textContent: bt });

      // Field select (shows human-readable labels)
      const select = el('select', { class: 'dim-select', 'data-type': bt });
      for (const f of fields) {
        const opt = el('option', { value: f.key, textContent: f.label });
        if (f.key === currentField) opt.selected = true;
        select.appendChild(opt);
      }

      // Distribution info line
      const distInfo = el('span', { class: 'dist-info', textContent: distLabel(dist) });

      const dimRow = el('div', { class: 'grouping-row' }, [
        el('label', { textContent: 'By' }),
        select,
      ]);

      const infoRow = el('div', { class: 'grouping-row grouping-info' }, [distInfo]);

      // Bin slider — max and value adapt to the selected field
      const slider = el('input', {
        type: 'range', class: 'bin-slider', 'data-type': bt,
        min: '1', max: String(Math.max(1, dist.maxBins)),
        value: String(Math.min(currentBins, dist.maxBins)),
      });
      const binDisplay = el('span', {
        class: 'bin-value',
        textContent: String(Math.min(currentBins, dist.maxBins)),
      });

      const binRow = el('div', { class: 'grouping-row' }, [
        el('label', { textContent: 'Bins' }),
        slider,
        binDisplay,
      ]);

      // On field change: analyze distribution, auto-adjust slider, trigger regroup
      select.addEventListener('change', () => {
        const newField = select.value;
        const newDist = Grouping.analyzeDistribution(elements, bt, newField);

        // Update slider range and value
        slider.max = String(Math.max(1, newDist.maxBins));
        slider.value = String(newDist.suggestedBins);
        binDisplay.textContent = String(newDist.suggestedBins);
        distInfo.textContent = distLabel(newDist);

        groupingState[bt] = { dimension: newField, bins: newDist.suggestedBins };
        if (onRegroupRequest) onRegroupRequest(bt, newField, newDist.suggestedBins);
      });

      slider.addEventListener('input', () => { binDisplay.textContent = slider.value; });
      slider.addEventListener('change', () => {
        groupingState[bt] = { dimension: select.value, bins: parseInt(slider.value) };
        if (onRegroupRequest) onRegroupRequest(bt, select.value, parseInt(slider.value));
      });

      panel.appendChild(el('div', { class: 'grouping-panel' }, [title, dimRow, infoRow, binRow]));
    }
  }

  function distLabel(dist) {
    if (dist.fieldType === 'distinct') {
      return `${dist.distinctCount} unique values`;
    }
    return `${dist.distinctCount} distinct \u2192 suggested ${dist.suggestedBins} bins`;
  }

  function getGroupingState() { return { ...groupingState }; }
  function setGroupingState(bt, dimension, bins) { groupingState[bt] = { dimension, bins }; }

  return { init, buildTree, buildGroupingControls, getGroupingState, setGroupingState };
})();
