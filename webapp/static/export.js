/**
 * Export module for JSON, PNG, and Wall Mask outputs.
 *
 * All exports respect the current visibility state.
 */

const Export = (() => {

  function timestamp() {
    const d = new Date();
    const pad = (n) => String(n).padStart(2, '0');
    return `${d.getFullYear()}${pad(d.getMonth() + 1)}${pad(d.getDate())}_${pad(d.getHours())}${pad(d.getMinutes())}${pad(d.getSeconds())}`;
  }

  function download(blob, filename) {
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = filename;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    URL.revokeObjectURL(url);
  }

  /**
   * Export visible elements as JSON with both pt and px coordinates.
   */
  function exportJSON(appState) {
    const { flatElements, visibility, groupedData, source, pageSize, scaleFactor } = appState;
    const canvasSize = Renderer.getCanvasSize();

    // Build visible groups
    const visibleGroups = [];
    for (const group of groupedData) {
      for (const sg of group.subgroups) {
        const visibleElements = [];
        for (const idx of sg.globalIndices) {
          if (!visibility[idx]) continue;
          const elem = { ...flatElements[idx].elem };
          // Add pixel coordinates
          elem.points_px = elem.points.map(p => ({
            x: Math.round(p.x * scaleFactor),
            y: Math.round(p.y * scaleFactor),
          }));
          visibleElements.push(elem);
        }
        if (visibleElements.length > 0) {
          visibleGroups.push({
            group: `${group.baseType}_${sg.label}`.replace(/[\s,]+/g, '_'),
            elements: visibleElements,
          });
        }
      }
    }

    const output = {
      source,
      page_size: pageSize,
      scale_factor: scaleFactor,
      canvas_size: canvasSize,
      visible_groups: visibleGroups,
    };

    const stem = source.replace(/\.pdf$/i, '');
    const blob = new Blob([JSON.stringify(output, null, 2)], { type: 'application/json' });
    download(blob, `${stem}_elements_${timestamp()}.json`);
  }

  /**
   * Export PNG - elements only (white background).
   */
  function exportPNGElements(appState) {
    const { flatElements, visibility, elementColors, source } = appState;
    const offCanvas = Renderer.renderOffscreen(false, flatElements, visibility, elementColors);
    offCanvas.toBlob((blob) => {
      const stem = source.replace(/\.pdf$/i, '');
      download(blob, `${stem}_elements_${timestamp()}.png`);
    }, 'image/png');
  }

  /**
   * Export PNG - with blueprint background.
   */
  function exportPNGBlueprint(appState) {
    const { flatElements, visibility, elementColors, source } = appState;
    const offCanvas = Renderer.renderOffscreen(true, flatElements, visibility, elementColors);
    offCanvas.toBlob((blob) => {
      const stem = source.replace(/\.pdf$/i, '');
      download(blob, `${stem}_blueprint_${timestamp()}.png`);
    }, 'image/png');
  }

  /**
   * Export wall mask: binary image (white elements on black) + seeds JSON.
   */
  function exportWallMask(appState) {
    const { flatElements, visibility, source, scaleFactor } = appState;

    // Binary mask
    const maskCanvas = Renderer.renderMask(flatElements, visibility);
    maskCanvas.toBlob((blob) => {
      const stem = source.replace(/\.pdf$/i, '');
      download(blob, `${stem}_mask_${timestamp()}.png`);
    }, 'image/png');

    // Seeds JSON: visible text elements with pixel positions
    const seeds = [];
    for (let i = 0; i < flatElements.length; i++) {
      if (!visibility[i]) continue;
      const elem = flatElements[i].elem;
      if (elem.type !== 'text' || !elem.label) continue;
      const pts = elem.points;
      if (pts.length < 2) continue;
      // Center of bounding box
      const cx = Math.round(((pts[0].x + pts[1].x) / 2) * scaleFactor);
      const cy = Math.round(((pts[0].y + pts[1].y) / 2) * scaleFactor);
      seeds.push({ label: elem.label, x_px: cx, y_px: cy });
    }

    const seedsBlob = new Blob([JSON.stringify({ seeds }, null, 2)], { type: 'application/json' });
    const stem = source.replace(/\.pdf$/i, '');
    download(seedsBlob, `${stem}_seeds_${timestamp()}.json`);
  }

  return { exportJSON, exportPNGElements, exportPNGBlueprint, exportWallMask };
})();
