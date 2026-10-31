// Preload before either agent. Defense in depth, not a kernel network sandbox.
import net from 'node:net';
import dns from 'node:dns';
function hostAllowed(host) {
  host = String(host ?? 'localhost').replace(/^\[|\]$/g, '');
  if (host === 'localhost' || host === '::1' || /^127\.\d+\.\d+\.\d+$/.test(host)) return;
  throw new Error('blocked non-loopback network destination');
}
const lookup = dns.lookup;
dns.lookup = function(host, ...args) { hostAllowed(host); return lookup.call(this,host,...args); };
const connect = net.Socket.prototype.connect;
net.Socket.prototype.connect = function(...args) {
  let value = args[0];
  if (Array.isArray(value)) value = value[0];
  if (typeof value === 'object' && value !== null) hostAllowed(value.host ?? value.hostname);
  else if (typeof value === 'number') hostAllowed(typeof args[1] === 'string' ? args[1] : 'localhost');
  else throw new Error('blocked non-TCP socket destination');
  return connect.apply(this,args);
};
const fetch = globalThis.fetch;
globalThis.fetch = function(input,...args) {
  const url = new URL(typeof input === 'string' || input instanceof URL ? input : input.url);
  hostAllowed(url.hostname);
  return fetch.call(this,input,...args);
};
