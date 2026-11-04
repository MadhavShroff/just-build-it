// The RFC 6902 subset of the wire protocol: add, remove and replace.
// Paths use RFC 6901 escaping (`~` is `~0`, `/` is `~1`). This mirrors jbi/jsonpatch.py.

export class PatchError extends Error {
  constructor(message) {
    super(message);
    this.name = 'PatchError';
  }
}

export const escapeToken = (token) => String(token).replaceAll('~', '~0').replaceAll('/', '~1');
export const unescapeToken = (token) => token.replaceAll('~1', '/').replaceAll('~0', '~');

/** pointer('tasks', 'a/b') === '/tasks/a~1b' */
export function pointer(...tokens) {
  return tokens.map((token) => '/' + escapeToken(token)).join('');
}

export function split(path) {
  if (path === '') return [];
  if (typeof path !== 'string' || !path.startsWith('/')) {
    throw new PatchError(`pointer must start with '/': ${JSON.stringify(path)}`);
  }
  return path.slice(1).split('/').map(unescapeToken);
}

const isObject = (value) => value !== null && typeof value === 'object' && !Array.isArray(value);
const clone = (value) => (value === undefined ? undefined : structuredClone(value));

function arrayIndex(list, token, allowEnd) {
  if (token === '-' && allowEnd) return list.length;
  if (!/^\d+$/.test(token)) throw new PatchError(`bad array index ${JSON.stringify(token)}`);
  const index = Number(token);
  if (index >= list.length + (allowEnd ? 1 : 0)) throw new PatchError(`array index ${index} out of range`);
  return index;
}

function parentOf(doc, tokens) {
  let node = doc;
  for (const token of tokens.slice(0, -1)) {
    if (isObject(node)) {
      if (!Object.hasOwn(node, token)) throw new PatchError(`missing key ${JSON.stringify(token)}`);
      node = node[token];
    } else if (Array.isArray(node)) {
      node = node[arrayIndex(node, token, false)];
    } else {
      throw new PatchError(`cannot descend into ${node === null ? 'null' : typeof node}`);
    }
  }
  return [node, tokens[tokens.length - 1]];
}

/** Apply one operation in place. Returns the document (a new root for path ""). */
export function applyOp(doc, op) {
  const kind = op?.op;
  if (!['add', 'remove', 'replace'].includes(kind) || typeof op.path !== 'string') {
    throw new PatchError(`unsupported operation ${JSON.stringify(op)}`);
  }
  const tokens = split(op.path);
  if (tokens.length === 0) {
    if (kind === 'remove') throw new PatchError('cannot remove the root');
    return clone(op.value);
  }
  const [parent, last] = parentOf(doc, tokens);
  if (isObject(parent)) {
    if (kind !== 'add' && !Object.hasOwn(parent, last)) {
      throw new PatchError(`${kind} of missing key ${JSON.stringify(last)}`);
    }
    if (kind === 'remove') delete parent[last];
    else parent[last] = clone(op.value);
  } else if (Array.isArray(parent)) {
    if (kind === 'add') parent.splice(arrayIndex(parent, last, true), 0, clone(op.value));
    else if (kind === 'remove') parent.splice(arrayIndex(parent, last, false), 1);
    else parent[arrayIndex(parent, last, false)] = clone(op.value);
  } else {
    throw new PatchError(`cannot patch into ${parent === null ? 'null' : typeof parent}`);
  }
  return doc;
}

export function applyPatch(doc, ops) {
  for (const op of ops) doc = applyOp(doc, op);
  return doc;
}

/** The top-level state keys that a list of operations touches. A root operation touches '*'. */
export function touchedKeys(ops) {
  const keys = new Set();
  for (const op of ops) {
    const tokens = split(op.path);
    keys.add(tokens.length ? tokens[0] : '*');
  }
  return keys;
}
