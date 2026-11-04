// Inline SVG icons (24px grid, 1.6 stroke). icon('name') returns a fresh <svg>.

import { s } from './dom.js';

const PATHS = {
  logo: ['M4 12l5 5L20 6', 'M4 6h6', 'M14 18h6'],
  sun: ['M12 4V2', 'M12 22v-2', 'M4.9 4.9 3.5 3.5', 'M20.5 20.5l-1.4-1.4', 'M4 12H2', 'M22 12h-2', 'M4.9 19.1l-1.4 1.4', 'M20.5 3.5l-1.4 1.4', 'M12 16a4 4 0 1 0 0-8 4 4 0 0 0 0 8z'],
  moon: ['M20 14.5A8 8 0 0 1 9.5 4a8 8 0 1 0 10.5 10.5z'],
  key: ['M15 7a4 4 0 1 1-3.9 4.9L4 19v-3h2v-2h2l1.1-1.1A4 4 0 0 1 15 7z', 'M16 10h.01'],
  stop: ['M7 7h10v10H7z'],
  power: ['M12 3v9', 'M6.3 6.3a8 8 0 1 0 11.4 0'],
  pause: ['M9 6v12', 'M15 6v12'],
  play: ['M8 5.5v13l10-6.5z'],
  retry: ['M4 12a8 8 0 0 1 14-5.3L20 9', 'M20 4v5h-5', 'M20 12a8 8 0 0 1-14 5.3L4 15', 'M4 20v-5h5'],
  skull: ['M12 3a7 7 0 0 0-7 7v3l2 2v3h10v-3l2-2v-3a7 7 0 0 0-7-7z', 'M9 11h.01', 'M15 11h.01', 'M10 18v3', 'M14 18v3'],
  flag: ['M5 21V4', 'M5 4h11l-2 4 2 4H5'],
  exit: ['M15 4h4v16h-4', 'M10 8l-4 4 4 4', 'M6 12h10'],
  settings: ['M4 7h10', 'M18 7h2', 'M4 17h4', 'M12 17h8', 'M16 5v4', 'M10 15v4'],
  close: ['M6 6l12 12', 'M18 6L6 18'],
  check: ['M5 12.5l4.5 4.5L19 7.5'],
  x: ['M7 7l10 10', 'M17 7L7 17'],
  copy: ['M9 9h11v11H9z', 'M5 15H4V4h11v1'],
  send: ['M5 12h13', 'M13 6l6 6-6 6'],
  terminal: ['M4 5h16v14H4z', 'M8 10l2.5 2L8 14', 'M13 14h3'],
  search: ['M11 18a7 7 0 1 0 0-14 7 7 0 0 0 0 14z', 'M20 20l-4-4'],
  plus: ['M12 5v14', 'M5 12h14'],
  minus: ['M5 12h14'],
  fit: ['M4 9V4h5', 'M20 9V4h-5', 'M4 15v5h5', 'M20 15v5h-5'],
  arrowLeft: ['M19 12H5', 'M11 6l-6 6 6 6'],
  arrowRight: ['M5 12h14', 'M13 6l6 6-6 6'],
  sparkle: ['M12 3l1.8 5.2L19 10l-5.2 1.8L12 17l-1.8-5.2L5 10l5.2-1.8z', 'M19 16l.8 2.2L22 19l-2.2.8L19 22l-.8-2.2L16 19l2.2-.8z'],
  list: ['M9 6h11', 'M9 12h11', 'M9 18h11', 'M4.5 6h.01', 'M4.5 12h.01', 'M4.5 18h.01'],
  chat: ['M4 5h16v11H9l-5 4z'],
  graph: ['M6 6h.01', 'M18 6h.01', 'M12 18h.01', 'M6 6l6 12', 'M18 6l-6 12', 'M6 6h12'],
  dag: ['M4 6h5v4H4z', 'M15 4h5v4h-5z', 'M15 16h5v4h-5z', 'M9 8h3l3-2', 'M12 8v10h3'],
  bolt: ['M13 3L5 14h6l-1 7 8-11h-6z'],
  coins: ['M12 8c4.4 0 8-1.3 8-3s-3.6-3-8-3-8 1.3-8 3 3.6 3 8 3z', 'M4 5v6c0 1.7 3.6 3 8 3s8-1.3 8-3V5', 'M4 11v6c0 1.7 3.6 3 8 3s8-1.3 8-3v-6'],
  in: ['M12 4v12', 'M6 10l6 6 6-6', 'M5 20h14'],
  out: ['M12 20V8', 'M6 14l6-6 6 6', 'M5 4h14'],
  lane: ['M6 3v18', 'M18 3v18', 'M12 5v2', 'M12 11v2', 'M12 17v2'],
  inbox: ['M4 13l2.5-8h11L20 13', 'M4 13v6h16v-6', 'M4 13h5l1 2h4l1-2h5'],
  clock: ['M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18z', 'M12 7v5l3 2'],
  folder: ['M3 6h6l2 2h10v11H3z'],
  layers: ['M12 3l9 5-9 5-9-5z', 'M3 13l9 5 9-5'],
  upload: ['M12 16V4', 'M6 10l6-6 6 6', 'M5 20h14'],
  edit: ['M4 20h4L19 9l-4-4L4 16z', 'M13 7l4 4'],
  // architecture node kinds
  service: ['M4 5h16v6H4z', 'M4 13h16v6H4z', 'M8 8h.01', 'M8 16h.01'],
  client: ['M3 5h18v11H3z', 'M8 20h8', 'M12 16v4'],
  database: ['M12 8c4.4 0 8-1.3 8-3s-3.6-3-8-3-8 1.3-8 3 3.6 3 8 3z', 'M4 5v14c0 1.7 3.6 3 8 3s8-1.3 8-3V5', 'M4 12c0 1.7 3.6 3 8 3s8-1.3 8-3'],
  queue: ['M4 7h4v10H4z', 'M10 7h4v10h-4z', 'M16 7h4v10h-4z'],
  cache: ['M13 3L5 14h6l-1 7 8-11h-6z'],
  external: ['M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18z', 'M3 12h18', 'M12 3c2.5 2.5 3.8 5.5 3.8 9s-1.3 6.5-3.8 9c-2.5-2.5-3.8-5.5-3.8-9S9.5 5.5 12 3z'],
  storage: ['M3 7h18v4H3z', 'M3 13h18v4H3z', 'M7 9h.01', 'M7 15h.01'],
  worker: ['M12 15a3 3 0 1 0 0-6 3 3 0 0 0 0 6z', 'M12 2v3', 'M12 19v3', 'M2 12h3', 'M19 12h3', 'M4.9 4.9l2.1 2.1', 'M17 17l2.1 2.1', 'M4.9 19.1L7 17', 'M17 7l2.1-2.1'],
};

export function icon(name, { size = 16, label } = {}) {
  const paths = PATHS[name] || PATHS.sparkle;
  return s('svg', {
    class: `icon icon-${name}`, width: size, height: size, viewBox: '0 0 24 24', fill: 'none',
    stroke: 'currentColor', 'stroke-width': 1.6, 'stroke-linecap': 'round', 'stroke-linejoin': 'round',
    'aria-hidden': label ? null : 'true', role: label ? 'img' : null, 'aria-label': label || null,
  }, paths.map((d) => s('path', { d })));
}

/** Path data for drawing an icon inside another SVG (the architecture diagram). */
export const iconPaths = (name) => PATHS[name] || PATHS.service;
