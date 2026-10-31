/* ES5 syntax deliberately: an unsupported Node must still start the program. */
(function () {
  try {
    if (global.__traceaiLauncherLoaded) return;
    global.__traceaiLauncherLoaded = true;
    var Module = require('module');
    var path = require('path');
    function start() {
      try {
        var resolved;
        try {
          var makeRequire = Module.createRequire || Module.createRequireFromPath;
          if (makeRequire) {
            resolved = makeRequire(path.join(process.cwd(), '__traceai_resolve__.cjs')).resolve('@traceai/auto/register');
          } else {
            resolved = Module._resolveFilename('@traceai/auto/register', { paths: Module._nodeModulePaths(process.cwd()) });
          }
        } catch (_) {}
        if (resolved) {
          try { require(resolved); return; } catch (_) {}
        }
        var fallback = __TRACEAI_NODE_BUNDLE__;
        if (fallback) { try { require(fallback); } catch (_) {} }
      } catch (_) {}
    }
    // Ordinary preloads can themselves make requests. Start before them, only
    // deferring past a known resolution prerequisite (Yarn's .pnp.js/.pnp.cjs).
    // NODE_OPTIONS is not in execArgv; its parser uses double quotes and only
    // treats backslashes as escapes inside them, just like Node itself.
    var options = [], word = '', quoted = false;
    var raw = process.env.NODE_OPTIONS || '';
    for (var n = 0; n < raw.length; n++) {
      var c = raw.charAt(n);
      if (c === '\\' && quoted) word += raw.charAt(++n);
      else if (c === '"') quoted = !quoted;
      else if (c === ' ' && !quoted) { if (word) options.push(word); word = ''; }
      else word += c;
    }
    if (word) options.push(word);
    options = options.concat(process.execArgv);
    var loader;
    for (var i = 0; i < options.length; i++) {
      var arg = options[i], request = null;
      if (arg === '-r' || arg === '--require') request = options[++i];
      else if (arg.indexOf('--require=') === 0) request = arg.slice(10);
      else if (arg.indexOf('-r') === 0 && arg.length > 2) request = arg.slice(2);
      if (request && /(^|[\\/])\.pnp\.(c?js)$/.test(request)) loader = request;
    }
    if (!loader) { start(); return; }
    // PnP replaces _load and may bypass the previous implementation entirely.
    // Observe the preload parent's require() instead, outside that loader.
    var originalRequire = Module.prototype.require;
    var done = false;
    function afterPreload(request) {
      // Never catch an application loader's exception or alter its return value.
      var result = originalRequire.apply(this, arguments);
      if (!done && this && this.id === 'internal/preload' && request === loader) {
        done = true;
        // Preserve a project wrapper if it changed require in the meantime.
        if (Module.prototype.require === afterPreload) Module.prototype.require = originalRequire;
        start();
      }
      return result;
    }
    Module.prototype.require = afterPreload;
  } catch (_) {}
}());
