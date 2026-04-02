/**
 * Main application controller.
 *
 * Orchestrates upload, extraction, grouping, rendering, and exports.
 */

const App = (() => {
  // ── State ──────────────────────────────────────────────
  let rawData = null;           // raw JSON from /api/extract
  let flatElements = [];        // [{elem, groupName, globalIndex}]
  let visibility = null;        // Uint8Array bitset
  let groupedData = [];         // [{baseType, dimension, subgroups}]
  let groupColors = {};         // groupName -> {r,g,b}
  let source = '';
  let pageSize = null;
  let scaleFactor = 3;

  // ── Init ───────────────────────────────────────────────

  function init() {
    Renderer.init(document.getElementById('canvas'));

    UI.init({
      onVisibilityChange: () => doRender(),
      onRegroupRequest: (baseType, dimension, bins) => regroupType(baseType, dimension, bins),
    });

    // File input
    document.getElementById('file-input').addEventListener('change', (e) => {
      if (e.target.files.length > 0) uploadFile(e.target.files[0]);
    });

    // Drag and drop
    const canvasWrap = document.getElementById('canvas-wrap');
    canvasWrap.addEventListener('dragover', (e) => { e.preventDefault(); canvasWrap.classList.add('dragover'); });
    canvasWrap.addEventListener('dragleave', () => canvasWrap.classList.remove('dragover'));
    canvasWrap.addEventListener('drop', (e) => {
      e.preventDefault();
      canvasWrap.classList.remove('dragover');
      if (e.dataTransfer.files.length > 0) uploadFile(e.dataTransfer.files[0]);
    });

    // Blueprint toggle
    document.getElementById('blueprint-btn').addEventListener('click', () => {
      const visible = !Renderer.isBlueprintVisible();
      Renderer.setBlueprintVisible(visible);
      document.getElementById('blueprint-btn').textContent = `Blueprint: ${visible ? 'ON' : 'OFF'}`;
      doRender();
    });

    // Export dropdown
    const dropdown = document.getElementById('export-dropdown');
    document.getElementById('export-btn').addEventListener('click', () => {
      dropdown.classList.toggle('open');
    });
    document.addEventListener('click', (e) => {
      if (!dropdown.contains(e.target)) dropdown.classList.remove('open');
    });

    document.getElementById('export-json').addEventListener('click', () => {
      dropdown.classList.remove('open');
      Export.exportJSON(getExportState());
    });
    document.getElementById('export-png-elements').addEventListener('click', () => {
      dropdown.classList.remove('open');
      Export.exportPNGElements(getExportState());
    });
    document.getElementById('export-png-blueprint').addEventListener('click', () => {
      dropdown.classList.remove('open');
      Export.exportPNGBlueprint(getExportState());
    });
    document.getElementById('export-mask').addEventListener('click', () => {
      dropdown.classList.remove('open');
      Export.exportWallMask(getExportState());
    });

    // Reset
    document.getElementById('reset-btn').addEventListener('click', () => {
      if (!visibility) return;
      visibility.fill(1);
      rebuildUI();
      doRender();
    });

    // Select all / none
    document.getElementById('select-all-btn').addEventListener('click', () => {
      if (!visibility) return;
      visibility.fill(1);
      rebuildUI();
      doRender();
    });
    document.getElementById('select-none-btn').addEventListener('click', () => {
      if (!visibility) return;
      visibility.fill(0);
      rebuildUI();
      doRender();
    });

    // Zoom controls + pan
    initZoom();
    initPan();
  }

  // ── Zoom ──────────────────────────────────────────────

  const ZOOM_STEPS = [0.1, 0.25, 0.5, 0.75, 1, 1.5, 2, 3, 4, 5];
  let currentZoomIdx = -1;  // -1 = fit mode

  function initZoom() {
    const wrap = document.getElementById('canvas-wrap');

    document.getElementById('zoom-in-btn').addEventListener('click', () => stepZoom(1));
    document.getElementById('zoom-out-btn').addEventListener('click', () => stepZoom(-1));
    document.getElementById('zoom-fit-btn').addEventListener('click', () => {
      currentZoomIdx = -1;
      applyZoom();
    });

    // Mouse wheel zoom (Ctrl+scroll or plain scroll)
    wrap.addEventListener('wheel', (e) => {
      e.preventDefault();
      stepZoom(e.deltaY < 0 ? 1 : -1);
    }, { passive: false });

    applyZoom();
  }

  function stepZoom(dir) {
    if (currentZoomIdx === -1) {
      // Leaving fit mode — find closest step to current fit scale
      const canvas = document.getElementById('canvas');
      const fitScale = canvas.clientWidth / (canvas.width || 1);
      currentZoomIdx = ZOOM_STEPS.findIndex(s => s >= fitScale - 0.01);
      if (currentZoomIdx === -1) currentZoomIdx = ZOOM_STEPS.length - 1;
    }
    currentZoomIdx = Math.max(0, Math.min(ZOOM_STEPS.length - 1, currentZoomIdx + dir));
    applyZoom();
  }

  function applyZoom() {
    const canvas = document.getElementById('canvas');
    if (!canvas.width) return;

    if (currentZoomIdx === -1) {
      // Fit mode: let CSS constrain
      canvas.style.width = '';
      canvas.style.height = '';
      const wrap = document.getElementById('canvas-wrap');
      const maxW = wrap.clientWidth - 16;
      const maxH = wrap.clientHeight - 16;
      const scaleW = maxW / canvas.width;
      const scaleH = maxH / canvas.height;
      const fitScale = Math.min(scaleW, scaleH, 1);
      canvas.style.width = Math.round(canvas.width * fitScale) + 'px';
      canvas.style.height = Math.round(canvas.height * fitScale) + 'px';
      document.getElementById('zoom-level').textContent = 'Fit';
    } else {
      const scale = ZOOM_STEPS[currentZoomIdx];
      canvas.style.width = Math.round(canvas.width * scale) + 'px';
      canvas.style.height = Math.round(canvas.height * scale) + 'px';
      document.getElementById('zoom-level').textContent = `${Math.round(scale * 100)}%`;
    }
  }

  // ── Pan (click-hold-drag) ──────────────────────────────

  function initPan() {
    const wrap = document.getElementById('canvas-wrap');
    let panning = false;
    let startX, startY, scrollX0, scrollY0;

    wrap.addEventListener('mousedown', (e) => {
      if (e.button !== 0) return;
      panning = true;
      startX = e.clientX;
      startY = e.clientY;
      scrollX0 = wrap.scrollLeft;
      scrollY0 = wrap.scrollTop;
      wrap.style.cursor = 'grabbing';
      e.preventDefault();
    });

    window.addEventListener('mousemove', (e) => {
      if (!panning) return;
      wrap.scrollLeft = scrollX0 - (e.clientX - startX);
      wrap.scrollTop = scrollY0 - (e.clientY - startY);
    });

    window.addEventListener('mouseup', () => {
      if (!panning) return;
      panning = false;
      wrap.style.cursor = '';
    });
  }

  // ── Upload ─────────────────────────────────────────────

  async function uploadFile(file) {
    if (!file.name.toLowerCase().endsWith('.pdf')) {
      alert('Please upload a PDF file.');
      return;
    }

    setStatus('Extracting...');
    showLoading(true);

    const formData = new FormData();
    formData.append('file', file);

    try {
      const resp = await fetch('/api/extract', { method: 'POST', body: formData });
      if (!resp.ok) throw new Error(`Server error: ${resp.status}`);
      rawData = await resp.json();
    } catch (err) {
      alert(`Upload failed: ${err.message}`);
      showLoading(false);
      setStatus('Upload failed');
      return;
    }

    source = rawData.source || file.name;
    pageSize = rawData.page_size;
    scaleFactor = rawData.scale_factor || 3;

    // Setup renderer
    Renderer.setPage(pageSize, scaleFactor);
    if (rawData.background_png) {
      await Renderer.loadBackground(rawData.background_png);
    }

    // Build flat element list and preload embedded images
    buildFlatElements();
    await Renderer.preloadImages(flatElements);

    // Enable buttons
    document.getElementById('blueprint-btn').disabled = false;
    document.getElementById('export-btn').disabled = false;
    document.getElementById('reset-btn').disabled = false;
    document.getElementById('select-all-btn').disabled = false;
    document.getElementById('select-none-btn').disabled = false;

    // Initial grouping
    applyDefaultGrouping();
    buildElementColors();
    rebuildUI();
    doRender();
    applyZoom();

    showLoading(false);
    const total = flatElements.length;
    setStatus(`${source} \u2014 ${total} elements`);
  }

  // ── Flat element list ──────────────────────────────────

  function buildFlatElements() {
    flatElements = [];
    let idx = 0;
    for (const g of rawData.groups) {
      for (const elem of g.elements) {
        flatElements.push({ elem, groupName: g.group, globalIndex: idx });
        idx++;
      }
    }
    visibility = new Uint8Array(flatElements.length).fill(1);
  }

  // ── Grouping ───────────────────────────────────────────

  function applyDefaultGrouping() {
    groupedData = [];
    groupColors = {};

    // Group flat elements by base type
    const byType = {};
    for (const fe of flatElements) {
      const bt = fe.groupName;
      if (!byType[bt]) byType[bt] = [];
      byType[bt].push(fe);
    }

    for (const [baseType, elements] of Object.entries(byType)) {
      const rawElems = elements.map(fe => fe.elem);
      const dim = Grouping.getDefaultField(baseType);
      const dist = Grouping.analyzeDistribution(rawElems, baseType, dim);
      const bins = dist.suggestedBins;
      UI.setGroupingState(baseType, dim, bins);

      const subgroups = Grouping.bin(rawElems, baseType, dim, bins);

      // Map local indices back to global indices
      for (const sg of subgroups) {
        sg.globalIndices = sg.indices.map(localIdx => elements[localIdx].globalIndex);
        // Register color for this subgroup
        const sgName = `${baseType}_${sg.label}`;
        groupColors[sgName] = sg.color;
      }

      groupedData.push({ baseType, dimension: dim, subgroups });
    }

    // Also map base type names to first subgroup color for ungrouped rendering
    for (const gd of groupedData) {
      if (gd.subgroups.length > 0) {
        groupColors[gd.baseType] = gd.subgroups[0].color;
      }
    }
  }

  function regroupType(baseType, dimension, bins) {
    // Find flat elements for this base type
    const elements = flatElements.filter(fe => fe.groupName === baseType);
    const rawElems = elements.map(fe => fe.elem);
    const subgroups = Grouping.bin(rawElems, baseType, dimension, bins);

    for (const sg of subgroups) {
      sg.globalIndices = sg.indices.map(localIdx => elements[localIdx].globalIndex);
      const sgName = `${baseType}_${sg.label}`;
      groupColors[sgName] = sg.color;
    }

    // Replace in groupedData
    const idx = groupedData.findIndex(gd => gd.baseType === baseType);
    if (idx >= 0) {
      groupedData[idx] = { baseType, dimension, subgroups };
    }

    buildElementColors();
    rebuildUI();
    doRender();
  }

  // ── Rendering ──────────────────────────────────────────

  // Pre-built per-element color array (rebuilt on regroup)
  let elementColors = [];

  function buildElementColors() {
    elementColors = new Array(flatElements.length);
    for (const gd of groupedData) {
      for (const sg of gd.subgroups) {
        for (const idx of sg.globalIndices) {
          elementColors[idx] = sg.color;
        }
      }
    }
  }

  function doRender() {
    if (elementColors.length !== flatElements.length) buildElementColors();
    Renderer.renderDirect(flatElements, visibility, elementColors);
  }

  function rebuildUI() {
    UI.buildTree(groupedData, visibility);
    const baseTypes = groupedData.map(gd => gd.baseType);
    UI.buildGroupingControls(baseTypes, (bt) => {
      return flatElements.filter(fe => fe.groupName === bt).map(fe => fe.elem);
    });
  }

  // ── Helpers ────────────────────────────────────────────

  function getElement(globalIdx) {
    return flatElements[globalIdx]?.elem;
  }

  function getExportState() {
    return { flatElements, visibility, groupedData, elementColors, source, pageSize, scaleFactor };
  }

  function setStatus(text) {
    document.getElementById('status-text').textContent = text;
  }

  function showLoading(show) {
    let overlay = document.getElementById('loading-overlay');
    if (!overlay && show) {
      overlay = document.createElement('div');
      overlay.id = 'loading-overlay';
      const box = document.createElement('div');
      box.id = 'loading-box';
      const spinner = document.createElement('div');
      spinner.className = 'spinner';
      const msg = document.createElement('div');
      msg.textContent = 'Extracting elements...';
      box.appendChild(spinner);
      box.appendChild(msg);
      overlay.appendChild(box);
      document.body.appendChild(overlay);
    }
    if (overlay) {
      overlay.classList.toggle('active', show);
    }
  }

  // Public API
  return { init, getElement };
})();

document.addEventListener('DOMContentLoaded', App.init);
