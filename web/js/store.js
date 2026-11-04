// The state store. It holds the server state document, the command schema and a small
// local UI state, and tells each subscriber which top-level keys changed.
//
// Server keys are the document's top-level keys ("run", "plan", ...).
// Local keys start with "$" ("$schema", "$connection", "$selectedTask", ...).

import { applyPatch, touchedKeys } from './patch.js';

const EVERYTHING = new Set(['*']);

export class Store {
  #subscribers = new Set();

  constructor() {
    this.state = null;
    this.seq = 0;
    this.schema = null;
    this.info = {};
    this.local = { connection: 'connecting', selectedTask: null, selectedNode: null, board: 'dag' };
  }

  get ready() {
    return this.state !== null && this.schema !== null;
  }

  /** Replace the whole document with a fresh snapshot. */
  load(seq, state, info = {}) {
    this.state = state;
    this.seq = seq;
    this.info = info;
    this.#emit({ type: 'snapshot', keys: EVERYTHING });
  }

  /**
   * Apply one stream event. Returns false when the event does not follow the local
   * sequence or does not fit the document; the caller then reads the snapshot again.
   */
  apply(event) {
    if (event.seq <= this.seq) return true;           // already applied
    if (event.seq !== this.seq + 1 || !this.state) return false;
    try {
      this.state = applyPatch(this.state, event.ops);
    } catch {
      return false;
    }
    this.seq = event.seq;
    this.#emit({ type: event.type, keys: touchedKeys(event.ops), event });
    return true;
  }

  setSchema(schema) {
    this.schema = schema;
    this.#emit({ type: 'schema', keys: new Set(['$schema']) });
  }

  setLocal(patch) {
    const keys = new Set();
    for (const [name, value] of Object.entries(patch)) {
      if (this.local[name] !== value) {
        this.local[name] = value;
        keys.add('$' + name);
      }
    }
    if (keys.size) this.#emit({ type: 'local', keys });
  }

  /**
   * Call `render(state, change)` whenever one of `keys` changes, and once now if the
   * store is ready. Returns an unsubscribe function.
   */
  subscribe(keys, render) {
    const subscriber = { keys: new Set(keys), render };
    this.#subscribers.add(subscriber);
    if (this.ready) render(this.state, { type: 'snapshot', keys: EVERYTHING });
    return () => this.#subscribers.delete(subscriber);
  }

  #emit(change) {
    if (!this.ready && change.type !== 'local') return;
    const all = change.keys.has('*');
    for (const subscriber of [...this.#subscribers]) {   // a copy: renders may subscribe
      const local = [...subscriber.keys].every((key) => key.startsWith('$'));
      const hit = all ? !local || change.type === 'snapshot'
        : [...change.keys].some((key) => subscriber.keys.has(key));
      if (hit) subscriber.render(this.state, change);
    }
  }
}
