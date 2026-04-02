/**
 * Canvas rendering engine.
 *
 * Draws rasterized PDF background + element overlay on a single canvas.
 * Ports the draw logic from iterate_elements.py reconstruct_image().
 */

const Renderer = (() => {
  let canvas = null;
  let ctx = null;
  let backgroundImage = null;
  let blueprintVisible = true;
  let pageW = 0, pageH = 0;
  let scaleFactor = 3;
  let canvasW = 0, canvasH = 0;
  let renderPending = false;

  // Draw order: fills first, then strokes thin-to-thick
  const STROKE_ORDER = [
    'lines', 'curves', 'rectangles', 'quads', 'text',
  ];

  function init(canvasEl) {
    canvas = canvasEl;
    ctx = canvas.getContext('2d');
  }

  function setPage(pageSize, scale) {
    pageW = pageSize.width;
    pageH = pageSize.height;
    scaleFactor = scale;
    canvasW = Math.round(pageW * scaleFactor);
    canvasH = Math.round(pageH * scaleFactor);
    canvas.width = canvasW;
    canvas.height = canvasH;
  }

  function loadBackground(dataUri) {
    return new Promise((resolve) => {
      const img = new Image();
      img.onload = () => {
        backgroundImage = img;
        resolve();
      };
      img.onerror = () => {
        backgroundImage = null;
        resolve();
      };
      img.src = dataUri;
    });
  }

  function setBlueprintVisible(visible) {
    blueprintVisible = visible;
  }

  function isBlueprintVisible() {
    return blueprintVisible;
  }

  // Coordinate conversion: PDF points -> canvas pixels
  function ptToPx(x, y) {
    return [x * scaleFactor, y * scaleFactor];
  }

  function widthToPx(w) {
    const px = w * scaleFactor;
    return Math.max(1, Math.min(12, Math.round(px)));
  }

  /**
   * Render all visible elements using a direct per-element color array.
   * @param {Array} flatElements - flat array of {elem, groupName, globalIndex}
   * @param {Uint8Array} visibility - visibility bitset
   * @param {Array} elementColors - per-element {r,g,b} color array
   */
  function renderDirect(flatElements, visibility, elementColors) {
    if (!ctx) return;

    // Background
    if (blueprintVisible && backgroundImage) {
      ctx.drawImage(backgroundImage, 0, 0, canvasW, canvasH);
    } else {
      ctx.fillStyle = '#ffffff';
      ctx.fillRect(0, 0, canvasW, canvasH);
    }

    // Separate fills from strokes
    const fills = [];
    const strokes = [];
    for (let i = 0; i < flatElements.length; i++) {
      if (!visibility[i]) continue;
      const fe = flatElements[i];
      if (fe.elem.type === 'fill') {
        fills.push({ fe, color: elementColors[i] });
      } else {
        strokes.push({ fe, color: elementColors[i] });
      }
    }

    // Sort strokes: thin lines first, thick last
    strokes.sort((a, b) => (a.fe.elem.width || 0) - (b.fe.elem.width || 0));

    // Pass 1: fills
    for (const { fe, color } of fills) {
      drawElement(fe.elem, color);
    }

    // Pass 2: strokes (thin to thick)
    for (const { fe, color } of strokes) {
      drawElement(fe.elem, color);
    }
  }

  function drawElement(elem, groupColor) {
    const pts = elem.points;
    if (!pts || pts.length < 2) return;

    const pxPts = pts.map(p => ptToPx(p.x, p.y));
    const thicknessBoost = blueprintVisible ? 1 : 0;

    let color;
    if (groupColor) {
      color = `rgb(${groupColor.r}, ${groupColor.g}, ${groupColor.b})`;
    } else {
      const c = elem.color || [0, 0, 0];
      color = `rgb(${c[0]}, ${c[1]}, ${c[2]})`;
    }

    switch (elem.type) {
      case 'line':
        ctx.strokeStyle = color;
        ctx.lineWidth = widthToPx(elem.width || 0) + thicknessBoost;
        ctx.lineCap = 'round';
        ctx.beginPath();
        ctx.moveTo(pxPts[0][0], pxPts[0][1]);
        ctx.lineTo(pxPts[1][0], pxPts[1][1]);
        ctx.stroke();
        break;

      case 'fill': {
        const fc = elem.fill || elem.color || [0, 0, 0];
        let fillColor;
        if (groupColor) {
          fillColor = `rgba(${groupColor.r}, ${groupColor.g}, ${groupColor.b}, 0.7)`;
        } else {
          fillColor = `rgb(${fc[0]}, ${fc[1]}, ${fc[2]})`;
        }
        ctx.fillStyle = fillColor;
        ctx.beginPath();
        ctx.moveTo(pxPts[0][0], pxPts[0][1]);
        for (let i = 1; i < pxPts.length; i++) {
          ctx.lineTo(pxPts[i][0], pxPts[i][1]);
        }
        ctx.closePath();
        ctx.fill();
        // Outline
        ctx.strokeStyle = color;
        ctx.lineWidth = 1;
        ctx.stroke();
        break;
      }

      case 'rectangle':
        ctx.strokeStyle = color;
        ctx.lineWidth = widthToPx(elem.width || 0) + thicknessBoost;
        if (pxPts.length >= 3) {
          ctx.strokeRect(
            pxPts[0][0], pxPts[0][1],
            pxPts[2][0] - pxPts[0][0],
            pxPts[2][1] - pxPts[0][1]
          );
        }
        break;

      case 'curve':
        ctx.strokeStyle = color;
        ctx.lineWidth = widthToPx(elem.width || 0) + thicknessBoost;
        ctx.lineCap = 'round';
        ctx.beginPath();
        ctx.moveTo(pxPts[0][0], pxPts[0][1]);
        if (pxPts.length === 4) {
          // True cubic Bezier
          ctx.bezierCurveTo(
            pxPts[1][0], pxPts[1][1],
            pxPts[2][0], pxPts[2][1],
            pxPts[3][0], pxPts[3][1]
          );
        } else {
          for (let i = 1; i < pxPts.length; i++) {
            ctx.lineTo(pxPts[i][0], pxPts[i][1]);
          }
        }
        ctx.stroke();
        break;

      case 'quad': {
        ctx.strokeStyle = color;
        ctx.lineWidth = widthToPx(elem.width || 0) + thicknessBoost;
        const np = pxPts;
        ctx.beginPath();
        ctx.moveTo(np[0][0], np[0][1]);
        for (let i = 1; i < np.length; i++) ctx.lineTo(np[i][0], np[i][1]);
        ctx.closePath();
        ctx.stroke();
        break;
      }

      case 'text': {
        ctx.strokeStyle = color;
        ctx.lineWidth = 1;
        const x1 = pxPts[0][0], y1 = pxPts[0][1];
        const x2 = pxPts[1][0], y2 = pxPts[1][1];
        ctx.strokeRect(x1, y1, x2 - x1, y2 - y1);
        if (elem.label) {
          const fontSize = Math.max(8, (elem.font_size || 6) * scaleFactor);
          ctx.fillStyle = color;
          ctx.font = `${fontSize}px sans-serif`;
          ctx.fillText(elem.label, x1, y2 - 2);
        }
        break;
      }
    }
  }

  /**
   * Render for export at full resolution.
   * @param {boolean} withBackground - include blueprint background
   * @param {Array} flatElements
   * @param {Uint8Array} visibility
   * @param {Array} elementColors - per-element {r,g,b} array
   * @returns {HTMLCanvasElement}
   */
  function renderOffscreen(withBackground, flatElements, visibility, elementColors) {
    const offCanvas = document.createElement('canvas');
    offCanvas.width = canvasW;
    offCanvas.height = canvasH;
    const offCtx = offCanvas.getContext('2d');

    // Background
    if (withBackground && backgroundImage) {
      offCtx.drawImage(backgroundImage, 0, 0, canvasW, canvasH);
    } else {
      offCtx.fillStyle = '#ffffff';
      offCtx.fillRect(0, 0, canvasW, canvasH);
    }

    // Temporarily swap context
    const savedCtx = ctx;
    const savedBlueprint = blueprintVisible;
    ctx = offCtx;
    blueprintVisible = withBackground;

    const fills = [];
    const strokes = [];
    for (let i = 0; i < flatElements.length; i++) {
      if (!visibility[i]) continue;
      const fe = flatElements[i];
      const color = elementColors[i];
      if (fe.elem.type === 'fill') fills.push({ fe, color });
      else strokes.push({ fe, color });
    }
    strokes.sort((a, b) => (a.fe.elem.width || 0) - (b.fe.elem.width || 0));

    for (const { fe, color } of fills) drawElement(fe.elem, color);
    for (const { fe, color } of strokes) drawElement(fe.elem, color);

    ctx = savedCtx;
    blueprintVisible = savedBlueprint;
    return offCanvas;
  }

  /**
   * Render binary mask: white elements on black background.
   * @param {Array} flatElements
   * @param {Uint8Array} visibility
   * @returns {HTMLCanvasElement}
   */
  function renderMask(flatElements, visibility) {
    const offCanvas = document.createElement('canvas');
    offCanvas.width = canvasW;
    offCanvas.height = canvasH;
    const offCtx = offCanvas.getContext('2d');

    offCtx.fillStyle = '#000000';
    offCtx.fillRect(0, 0, canvasW, canvasH);

    const white = '#ffffff';

    for (let i = 0; i < flatElements.length; i++) {
      if (!visibility[i]) continue;
      const elem = flatElements[i].elem;
      const pts = elem.points;
      if (!pts || pts.length < 2) continue;
      const pxPts = pts.map(p => ptToPx(p.x, p.y));

      switch (elem.type) {
        case 'line':
          offCtx.strokeStyle = white;
          offCtx.lineWidth = 2;
          offCtx.lineCap = 'round';
          offCtx.beginPath();
          offCtx.moveTo(pxPts[0][0], pxPts[0][1]);
          offCtx.lineTo(pxPts[1][0], pxPts[1][1]);
          offCtx.stroke();
          break;

        case 'fill':
          offCtx.fillStyle = white;
          offCtx.beginPath();
          offCtx.moveTo(pxPts[0][0], pxPts[0][1]);
          for (let j = 1; j < pxPts.length; j++) offCtx.lineTo(pxPts[j][0], pxPts[j][1]);
          offCtx.closePath();
          offCtx.fill();
          break;

        case 'rectangle':
          offCtx.strokeStyle = white;
          offCtx.lineWidth = 2;
          if (pxPts.length >= 3) {
            offCtx.strokeRect(pxPts[0][0], pxPts[0][1],
              pxPts[2][0] - pxPts[0][0], pxPts[2][1] - pxPts[0][1]);
          }
          break;

        case 'curve':
          offCtx.strokeStyle = white;
          offCtx.lineWidth = 2;
          offCtx.lineCap = 'round';
          offCtx.beginPath();
          offCtx.moveTo(pxPts[0][0], pxPts[0][1]);
          if (pxPts.length === 4) {
            offCtx.bezierCurveTo(pxPts[1][0], pxPts[1][1], pxPts[2][0], pxPts[2][1], pxPts[3][0], pxPts[3][1]);
          } else {
            for (let j = 1; j < pxPts.length; j++) offCtx.lineTo(pxPts[j][0], pxPts[j][1]);
          }
          offCtx.stroke();
          break;

        case 'quad':
          offCtx.strokeStyle = white;
          offCtx.lineWidth = 2;
          offCtx.beginPath();
          offCtx.moveTo(pxPts[0][0], pxPts[0][1]);
          for (let j = 1; j < pxPts.length; j++) offCtx.lineTo(pxPts[j][0], pxPts[j][1]);
          offCtx.closePath();
          offCtx.stroke();
          break;

        // text: skip in mask (no text rendering in binary mask)
      }
    }

    return offCanvas;
  }

  function requestRender(flatElements, visibility, elementColors) {
    if (renderPending) return;
    renderPending = true;
    requestAnimationFrame(() => {
      renderPending = false;
      renderDirect(flatElements, visibility, elementColors);
    });
  }

  function getCanvasSize() {
    return { width: canvasW, height: canvasH };
  }

  function getScaleFactor() {
    return scaleFactor;
  }

  return {
    init, setPage, loadBackground,
    setBlueprintVisible, isBlueprintVisible,
    renderDirect, requestRender,
    renderOffscreen, renderMask,
    ptToPx, getCanvasSize, getScaleFactor,
  };
})();
