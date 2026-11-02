// Real Pipecat clients, with only media/network I/O replaced by a transport.
function createClient(sdk, callbacks = {}) {
  // Pipecat reads window.navigator to describe the client, even with media off.
  globalThis.window ??= { navigator: { userAgent: 'offline-contract' } };
  class LoopbackTransport extends sdk.Transport {
    initialize(options, onMessage) {
      this.callbacks = options.callbacks;
      this.onMessage = onMessage;
      this._state = 'disconnected';
      this.sent = [];
    }
    get state() { return this._state; }
    set state(value) {
      this._state = value;
      this.callbacks.onTransportStateChanged?.(value);
    }
    async initDevices() { this.state = 'initialized'; }
    async connect() {
      if (this.connectError) throw this.connectError;
      this.state = 'connected';
      this.callbacks.onConnected?.();
    }
    sendReadyMessage() {
      this.state = 'ready';
      this.onMessage(new sdk.RTVIMessage('bot-ready', { version: '2.3.0', config: [] }));
    }
    async disconnect() {
      if (this.disconnectError) throw this.disconnectError;
      this.state = 'disconnected';
      this.callbacks.onDisconnected?.();
    }
    sendMessage(message) {
      if (this.sendError) throw this.sendError;
      this.sent.push(message);
      if (message.type === 'action') {
        queueMicrotask(() => this.onMessage(new sdk.RTVIMessage('action-response', { result: 'ok' }, message.id)));
      }
      if (message.type === 'client-message') {
        queueMicrotask(() => this.onMessage(new sdk.RTVIMessage('server-response', { t: message.data.t, d: { result: 'ok' } }, message.id)));
      }
    }
    tracks() { return { local: {} }; }
  }
  const transport = new LoopbackTransport();
  const Client = sdk.PipecatClient ?? sdk.RTVIClient;
  const client = new Client({
    transport,
    callbacks,
    enableMic: false,
    enableCam: false,
    params: { baseUrl: 'http://127.0.0.1/unused' },
    customConnectHandler: async () => ({}),
  });
  return { client, transport };
}
module.exports = { createClient };
