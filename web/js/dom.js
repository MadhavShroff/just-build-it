// Small DOM helpers: an element builder, keyed list reconciliation and formatters.
// Every text value goes through textContent, so user and model text is never parsed as HTML.

const SVG_NS = 'http://www.w3.org/2000/svg';

function build(element, props, children) {
  for (const [name, value] of Object.entries(props || {})) {
    if (value === undefined || value === null || value === false) continue;
    if (name === 'class') element.setAttribute('class', value);
    else if (name === 'text') element.textContent = value;
    else if (name === 'dataset') Object.assign(element.dataset, value);
    else if (name === 'style' && typeof value === 'object') Object.assign(element.style, value);
    else if (name.startsWith('on') && typeof value === 'function') element.addEventListener(name.slice(2), value);
    else if (name in element && !(element instanceof SVGElement) && typeof value !== 'string') element[name] = value;
    else element.setAttribute(name, value === true ? '' : value);
  }
  for (const child of children.flat(Infinity)) {
    if (child === null || child === undefined || child === false) continue;
    element.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return element;
}

/** h('div', {class: 'x'}, 'text', childNode) */
export const h = (tag, props, ...children) => build(document.createElement(tag), props, children);

/** s('path', {d: '...'}) creates an SVG element. */
export const s = (tag, props, ...children) => build(document.createElementNS(SVG_NS, tag), props, children);

export const $ = (selector, root = document) => root.querySelector(selector);

/** Set text only when it changed, so selection and screen readers are not disturbed. */
export function setText(element, text) {
  const value = text ?? '';
  if (element.textContent !== value) element.textContent = value;
}

export function toggle(element, className, on) {
  element.classList.toggle(className, Boolean(on));
}

/**
 * Keyed reconciliation. `items` is an array; `key(item)` gives a stable id.
 * `create(item)` returns a new element; `update(element, item, index)` refreshes it.
 * Elements keep their identity across renders, so transitions run and focus survives.
 */
export function reconcile(container, items, { key, create, update, enter = true }) {
  const existing = new Map();
  for (const child of [...container.children]) {
    if (child.dataset.key !== undefined && !child.classList.contains('is-leaving')) existing.set(child.dataset.key, child);
  }
  let cursor = container.firstElementChild;
  const seen = new Set();
  items.forEach((item, index) => {
    const id = String(key(item));
    seen.add(id);
    let element = existing.get(id);
    if (!element) {
      element = create(item);
      element.dataset.key = id;
      if (enter && container.dataset.mounted) element.classList.add('is-entering');
    }
    update?.(element, item, index);
    while (cursor && cursor.classList.contains('is-leaving')) cursor = cursor.nextElementSibling;
    if (element !== cursor) container.insertBefore(element, cursor);
    else cursor = cursor.nextElementSibling;
    if (element.classList.contains('is-entering')) {
      requestAnimationFrame(() => requestAnimationFrame(() => element.classList.remove('is-entering')));
    }
  });
  for (const [id, element] of existing) {
    if (!seen.has(id)) leave(element);
  }
  container.dataset.mounted = '1';
}

/** Fade an element out softly, then remove it. */
export function leave(element) {
  if (prefersReducedMotion()) {
    element.remove();
    return;
  }
  element.classList.add('is-leaving');
  setTimeout(() => element.remove(), 180);
}

/** Restart a short CSS "pulse" beat on an element. */
export function beat(element, className = 'beat') {
  element.classList.remove(className);
  void element.offsetWidth;
  element.classList.add(className);
}

export const prefersReducedMotion = () => matchMedia('(prefers-reduced-motion: reduce)').matches;

/**
 * Mark `host` with is-more-left / is-more-right (and is-more-top / is-more-bottom) while
 * `scroller` has hidden content in that direction, so CSS can draw an edge fade as a cue.
 * Returns the update function; call it after the content changes size.
 */
export function trackOverflow(scroller, host = scroller) {
  const update = () => {
    const { scrollLeft, scrollTop, scrollWidth, scrollHeight, clientWidth, clientHeight } = scroller;
    host.classList.toggle('is-more-left', scrollLeft > 1);
    host.classList.toggle('is-more-right', scrollLeft + clientWidth < scrollWidth - 1);
    host.classList.toggle('is-more-top', scrollTop > 1);
    host.classList.toggle('is-more-bottom', scrollTop + clientHeight < scrollHeight - 1);
  };
  scroller.addEventListener('scroll', update, { passive: true });
  new ResizeObserver(update).observe(scroller);
  return update;
}

// ---- formatting --------------------------------------------------------------------

const UNITS = [[60, 's', 1], [3600, 'm', 60], [86400, 'h', 3600], [Infinity, 'd', 86400]];

export function relativeTime(iso, now = Date.now()) {
  if (!iso) return '';
  const seconds = Math.max(0, Math.round((now - Date.parse(iso)) / 1000));
  if (seconds < 5) return 'now';
  const [, unit, size] = UNITS.find(([limit]) => seconds < limit);
  return `${Math.floor(seconds / size)}${unit} ago`;
}

export function elapsed(fromIso, toIso, now = Date.now()) {
  if (!fromIso) return '';
  const end = toIso ? Date.parse(toIso) : now;
  const total = Math.max(0, Math.round((end - Date.parse(fromIso)) / 1000));
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  const seconds = String(total % 60).padStart(2, '0');
  return hours ? `${hours}:${String(minutes).padStart(2, '0')}:${seconds}` : `${minutes}:${seconds}`;
}

export function clockTime(iso) {
  if (!iso) return '';
  return new Date(iso).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' });
}

export function compactNumber(value) {
  if (value === null || value === undefined) return 'unknown';
  return new Intl.NumberFormat(undefined, { notation: 'compact', maximumFractionDigits: 1 }).format(value);
}

export function money(value) {
  if (value === null || value === undefined) return 'unknown';
  return new Intl.NumberFormat(undefined, { style: 'currency', currency: 'USD', maximumFractionDigits: 2 }).format(value);
}

export const shortSha = (sha) => (sha ? String(sha).slice(0, 7) : '—');

// The same rules as jbi/planner.py slug(): lower case, runs of other characters become one
// hyphen, no leading non-letter, at most 40 characters, no trailing hyphen. The CLI passes
// the fallback "app" for a project slug, so that is the default here.
export const slug = (text, fallback = 'app') => String(text ?? '').toLowerCase()
  .replace(/[^a-z0-9]+/g, '-').replace(/^-+|-+$/g, '').replace(/^[^a-z]+/, '')
  .slice(0, 40).replace(/-+$/, '') || fallback;

export const titleCase = (text) => String(text || '').replace(/(^|[-_ ])(\w)/g, (_, gap, c) => (gap ? ' ' : '') + c.toUpperCase());
