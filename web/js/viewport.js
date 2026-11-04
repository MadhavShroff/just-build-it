// One zoom viewport for the task graph and the architecture diagram: zoom out, percent,
// zoom in and fit controls, wheel and keyboard zoom, and a refit on resize until the user
// zooms. 'pan' mode moves a transformed layer and pans by drag. 'scroll' mode scales the
// content inside a native scroll box, so touch scrolling and scrollbars keep working.

import { h, setText } from './dom.js';
import { icon } from './icons.js';

const clamp = (value, low, high) => Math.min(high, Math.max(low, value));

/**
 * element: the box that receives wheel and pointer input (the scroll box in 'scroll' mode).
 * size(): {x, y, width, height} of the content in content units (x and y default to 0).
 * apply(view): draw the view; view = {x, y, k, sw, sh} where sw and sh size the scroll area.
 */
export function createViewport({
  element, size, apply, mode = 'pan', keys = element, observe = element, onTap = null,
  minZoom = 0.3, maxZoom = 2.5, maxFit = 1.2, mobileZoom = 0.62, pad = 40, reserve = 0,
}) {
  const scrolls = mode === 'scroll';
  const view = { x: 0, y: 0, k: 1, sw: 0, sh: 0, moved: false };
  const label = h('span', { class: 'zoom-label tabular', 'aria-live': 'polite' });
  const controls = h('div', { class: 'zoom-group' },
    h('button', { class: 'btn btn-sm btn-icon', type: 'button', 'aria-label': 'Zoom out', onclick: () => zoomBy(1 / 1.2) }, icon('minus', { size: 14 })),
    label,
    h('button', { class: 'btn btn-sm btn-icon', type: 'button', 'aria-label': 'Zoom in', onclick: () => zoomBy(1.2) }, icon('plus', { size: 14 })),
    h('button', { class: 'btn btn-sm', type: 'button', onclick: () => fit() }, icon('fit', { size: 13 }), 'Fit'));

  const box = () => (scrolls ? { width: element.clientWidth, height: element.clientHeight } : element.getBoundingClientRect());
  const content = () => ({ x: 0, y: 0, ...size() });

  function update() {
    if (scrolls) {   // centre narrow content and size the scroll area to the scaled content
      const b = box(), c = content();
      view.x = Math.max(pad, (b.width - c.width * view.k) / 2);
      view.y = pad;
      view.sw = Math.max(b.width, c.width * view.k + pad * 2);
      view.sh = c.height * view.k + pad * 2;
    }
    apply(view);
    setText(label, `${Math.round(view.k * 100)}%`);
  }

  function zoomAt(factor, cx, cy) {
    const k = clamp(view.k * factor, minZoom, maxZoom);
    if (scrolls) {
      const px = (element.scrollLeft + cx - view.x) / view.k, py = (element.scrollTop + cy - view.y) / view.k;
      view.k = k;
      view.moved = true;
      update();
      element.scrollLeft = px * k + view.x - cx;
      element.scrollTop = py * k + view.y - cy;
      return;
    }
    view.x = cx - ((cx - view.x) * k) / view.k;
    view.y = cy - ((cy - view.y) * k) / view.k;
    view.k = k;
    view.moved = true;
    update();
  }
  function zoomBy(factor) {
    const b = box();
    zoomAt(factor, b.width / 2, b.height / 2);
  }

  /** The fit zoom: the whole content, but a legible minimum on a narrow screen. */
  function fitZoom() {
    const b = box(), c = content();
    if (!b.width || !c.width) return null;
    const k = clamp(Math.min((b.width - pad * 2) / c.width, (b.height - pad * 2 - reserve) / Math.max(c.height, 1)), minZoom, maxFit);
    return b.width < 600 ? Math.max(k, mobileZoom) : k;
  }
  function settle(k) {
    view.k = k;
    if (!scrolls) {
      const b = box(), c = content();
      view.x = (b.width - c.width * k) / 2 - c.x * k;
      view.y = Math.max(pad, pad + (b.height - pad * 2 - reserve - c.height * k) / 2) - c.y * k;
    }
    view.moved = false;
    update();
  }
  /** Fit the content and reset the scroll position. */
  function fit() {
    const k = fitZoom();
    if (k === null) return;
    settle(k);
    if (scrolls) element.scrollTo(0, 0);
  }
  /** After a content or size change: refit unless the user zoomed, and keep the scroll position. */
  function refresh() {
    const k = view.moved ? null : fitZoom();
    if (k === null) update();
    else settle(k);
  }

  element.addEventListener('wheel', (event) => {
    if (scrolls && !event.ctrlKey && !event.metaKey) return;   // a plain wheel scrolls the box
    event.preventDefault();
    const r = element.getBoundingClientRect();
    zoomAt(Math.exp(-event.deltaY * 0.0015), event.clientX - r.left, event.clientY - r.top);
  }, { passive: false });

  if (!scrolls) {
    let drag = null;
    element.addEventListener('pointerdown', (event) => {
      if (event.button !== 0) return;
      drag = { id: event.pointerId, x: event.clientX, y: event.clientY, vx: view.x, vy: view.y, moved: false };
    });
    element.addEventListener('pointermove', (event) => {
      if (!drag || drag.id !== event.pointerId) return;
      const dx = event.clientX - drag.x, dy = event.clientY - drag.y;
      if (!drag.moved && Math.hypot(dx, dy) > 3) { drag.moved = true; element.setPointerCapture(event.pointerId); element.classList.add('is-panning'); }
      if (drag.moved) { view.x = drag.vx + dx; view.y = drag.vy + dy; view.moved = true; update(); }
    });
    const endDrag = (event) => {
      if (!drag) return;
      const moved = drag.moved;
      drag = null;
      element.classList.remove('is-panning');
      if (!moved && event.type === 'pointerup') onTap?.(event);
    };
    element.addEventListener('pointerup', endDrag);
    element.addEventListener('pointercancel', endDrag);
  }

  keys.addEventListener('keydown', (event) => {
    if (event.target.matches?.('input, textarea')) return;
    const step = 40;
    const moves = { ArrowLeft: [step, 0], ArrowRight: [-step, 0], ArrowUp: [0, step], ArrowDown: [0, -step] };
    if (!scrolls && moves[event.key]) { view.x += moves[event.key][0]; view.y += moves[event.key][1]; view.moved = true; update(); event.preventDefault(); }
    if (event.key === '+' || event.key === '=') zoomBy(1.2);
    if (event.key === '-') zoomBy(1 / 1.2);
    if (event.key === '0') fit();
  });
  if (observe) new ResizeObserver(() => refresh()).observe(observe);

  /** Pan mode: centre a content point in the box. */
  function centreOn(x, y) {
    const b = box();
    view.x = b.width / 2 - x * view.k;
    view.y = b.height / 2 - y * view.k;
    view.moved = true;
    update();
  }

  return { view, controls, fit, refresh, zoomBy, centreOn };
}
