// The only module that talks HTTP. It owns the key exchange, the snapshot read, the
// event stream with sequence-gap detection, and the command write path.

const PROTOCOL = '1';
const MAX_RETRY_MS = 10000;

export class ApiError extends Error {
  constructor(status, code, message, details = {}) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
    this.code = code;
    this.details = details;
  }
}

function commandId() {
  if (globalThis.crypto?.randomUUID) return crypto.randomUUID();
  // Plain-HTTP Tailscale links are not a secure context, so randomUUID can be absent.
  const bytes = crypto.getRandomValues(new Uint8Array(16));
  bytes[6] = (bytes[6] & 0x0f) | 0x40;
  bytes[8] = (bytes[8] & 0x3f) | 0x80;
  const hex = [...bytes].map((b) => b.toString(16).padStart(2, '0')).join('');
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
}

export class Client {
  #source = null;
  #resync = null;
  #retryMs = 1000;
  #retryTimer = 0;

  constructor(store) {
    this.store = store;
  }

  /** Sign in with the link key, read the schema and the snapshot, then follow the stream. */
  async start() {
    try {
      await this.#exchangeKey();
      this.store.setSchema(await this.#request('GET', '/api/v1/schema'));
      await this.#refetch();
    } catch (error) {
      this.#fail(error);
    }
  }

  /** Submit one command. Resolves with the reply or throws an ApiError. */
  send(type, payload = {}) {
    return this.#request('POST', '/api/v1/commands', { id: commandId(), type, payload });
  }

  async #exchangeKey() {
    const match = location.hash.match(/(?:^#|&)k=([^&]+)/);
    if (!match) return;
    history.replaceState(null, '', location.pathname + location.search);
    try {
      await this.#request('POST', '/api/v1/session', { key: decodeURIComponent(match[1]) });
    } catch (error) {
      // A reload reuses a spent key; an existing session cookie may still be valid.
      if (!(error instanceof ApiError && error.code === 'unauthenticated')) throw error;
    }
  }

  async #refetch() {
    this.#close();
    const body = await this.#request('GET', '/api/v1/state');
    this.store.load(body.seq, body.state, body.info || {});
    this.#open();
  }

  #open() {
    const source = new EventSource(`/api/v1/events?after=${this.store.seq}`);
    this.#source = source;
    source.addEventListener('open', () => {
      this.#retryMs = 1000;
      this.store.setLocal({ connection: 'live' });
    });
    source.addEventListener('patch', (message) => {
      if (!this.store.apply(JSON.parse(message.data))) this.#resynchronize();
    });
    source.addEventListener('reset', () => this.#resynchronize());
    source.addEventListener('error', () => {
      if (source.readyState === EventSource.CLOSED) this.#scheduleRetry();
      else this.store.setLocal({ connection: 'connecting' });
    });
  }

  #close() {
    this.#source?.close();
    this.#source = null;
  }

  /** A gap, a reset or a bad patch: read the snapshot again, once at a time. */
  #resynchronize() {
    if (this.#resync) return;
    this.store.setLocal({ connection: 'connecting' });
    this.#resync = this.#refetch().catch((error) => this.#fail(error)).finally(() => { this.#resync = null; });
  }

  #scheduleRetry() {
    this.#close();
    this.store.setLocal({ connection: 'offline' });
    clearTimeout(this.#retryTimer);
    this.#retryTimer = setTimeout(() => this.#resynchronize(), this.#retryMs);
    this.#retryMs = Math.min(this.#retryMs * 2, MAX_RETRY_MS);
  }

  #fail(error) {
    if (error instanceof ApiError && error.code === 'unauthenticated') {
      this.store.setLocal({ connection: 'unauthenticated' });
      return;
    }
    this.#scheduleRetry();
  }

  async #request(method, path, body) {
    const headers = { 'Accept-Protocol': PROTOCOL };
    if (body !== undefined) headers['Content-Type'] = 'application/json';
    let response;
    try {
      response = await fetch(path, {
        method, headers, credentials: 'same-origin', cache: 'no-store',
        body: body === undefined ? undefined : JSON.stringify(body),
      });
    } catch {
      throw new ApiError(0, 'offline', 'The dashboard cannot reach the server.');
    }
    const data = await response.json().catch(() => null);
    if (!response.ok) {
      const error = data?.error || {};
      throw new ApiError(response.status, error.code || 'internal',
        error.message || `The server answered ${response.status}.`, error.details || {});
    }
    return data;
  }
}
