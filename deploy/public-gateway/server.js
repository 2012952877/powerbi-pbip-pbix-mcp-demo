'use strict';

const http = require('node:http');
const { Transform } = require('node:stream');
const HOP_HEADERS = new Set([
  'connection', 'keep-alive', 'proxy-authenticate', 'proxy-authorization',
  'te', 'trailer', 'transfer-encoding', 'upgrade', 'proxy-connection'
]);
const ERROR_CSP = "default-src 'none'; frame-ancestors 'none'; base-uri 'none'";

function cleanHeaders(headers) {
  const denied = new Set(HOP_HEADERS);
  for (const name of String(headers.connection || '').split(',')) denied.add(name.trim().toLowerCase());
  return Object.fromEntries(Object.entries(headers).filter(([name]) => !denied.has(name.toLowerCase())));
}

function configuration(env = process.env) {
  const origin = new URL(env.PUBLIC_ORIGIN);
  const backend = new URL(env.BACKEND_URL || `http://${env.BACKEND_HOST}:${env.BACKEND_PORT || 8765}`);
  if (origin.protocol !== 'https:' || origin.origin !== env.PUBLIC_ORIGIN ||
      !origin.hostname.endsWith('.azurewebsites.net') || origin.port ||
      origin.username || origin.password) throw new Error('PUBLIC_ORIGIN must be the canonical App Service HTTPS origin.');
  const octets = backend.hostname.split('.').map(Number);
  const privateIPv4 = octets.length === 4 && octets.every(value => Number.isInteger(value) && value >= 0 && value <= 255) &&
    (octets[0] === 10 || (octets[0] === 172 && octets[1] >= 16 && octets[1] <= 31) ||
      (octets[0] === 192 && octets[1] === 168));
  if (backend.protocol !== 'http:' || !privateIPv4 || !backend.port ||
      backend.pathname !== '/' || backend.search || backend.hash ||
      backend.username || backend.password) throw new Error('BACKEND_URL must target a private RFC1918 IPv4 portal and explicit port.');
  return { origin, backend, maxBodyBytes: 24 * 1024 * 1024, requireHttps: true };
}

function createGateway({ origin, backend, maxBodyBytes = 24 * 1024 * 1024, requireHttps = true }) {
  function failure(res, status, code, message) {
    if (res.headersSent) return res.destroy();
    res.writeHead(status, {
      'Content-Type': 'application/json',
      'Cache-Control': 'no-store',
      'Content-Security-Policy': ERROR_CSP,
      'X-Content-Type-Options': 'nosniff',
      'Strict-Transport-Security': 'max-age=31536000',
      'Connection': 'close'
    });
    res.end(JSON.stringify({ ok: false, error: { code, message } }));
  }

  function health(res) {
    const probe = http.get(new URL('/api/session', backend), {
      headers: { Host: origin.host, Accept: 'application/json' },
      timeout: 5000
    }, response => {
      let bytes = 0;
      let text = '';
      response.on('data', chunk => {
        bytes += chunk.length;
        if (bytes > 4096) return probe.destroy();
        text += chunk.toString('utf8');
      });
      response.on('error', () => failure(res, 503, 'BACKEND_UNAVAILABLE', 'The conversion service is unavailable.'));
      response.on('end', () => {
        let valid = false;
        try {
          const result = JSON.parse(text);
          valid = response.statusCode === 401 && result.ok === false && result.error.code === 'AUTH_REQUIRED';
        } catch { /* A non-JSON or unexpected upstream is not a healthy portal. */ }
        if (!valid) return failure(res, 503, 'BACKEND_UNAVAILABLE', 'The conversion service is unavailable.');
        res.writeHead(200, {
          'Content-Type': 'application/json', 'Cache-Control': 'no-store',
          'Content-Security-Policy': ERROR_CSP, 'X-Content-Type-Options': 'nosniff'
        });
        res.end('{"ok":true}');
      });
    });
    probe.on('timeout', () => probe.destroy());
    probe.on('error', () => failure(res, 503, 'BACKEND_UNAVAILABLE', 'The conversion service is unavailable.'));
    res.on('close', () => { if (!res.writableFinished) probe.destroy(); });
  }

  const server = http.createServer((req, res) => {
    if (req.method === 'GET' && req.url === '/healthz') return health(res);
    if (req.headers.host !== origin.host) return failure(res, 403, 'ORIGIN', 'Use the configured HTTPS endpoint.');
    if (requireHttps && req.headers['x-forwarded-proto'] !== 'https') {
      return failure(res, 403, 'HTTPS_REQUIRED', 'Use the configured HTTPS endpoint.');
    }
    if (req.headers.origin && req.headers.origin !== origin.origin) {
      return failure(res, 403, 'ORIGIN', 'Cross-origin requests are not permitted.');
    }
    if (!req.url.startsWith('/') || req.url.startsWith('//')) return failure(res, 400, 'REQUEST_TARGET', 'An origin-form request target is required.');
    const headers = cleanHeaders(req.headers);
    for (const name of Object.keys(headers)) {
      if (name === 'forwarded' || name.startsWith('x-forwarded-') ||
          name.startsWith('x-original-') || name.startsWith('x-ms-client-principal')) delete headers[name];
    }
    headers.host = origin.host;
    headers['x-forwarded-host'] = origin.host;
    headers['x-forwarded-proto'] = 'https';
    let bytes = 0;
    let rejected = false;
    const upstream = http.request({
      hostname: backend.hostname, port: backend.port, method: req.method,
      path: req.url, headers, timeout: 180000
    }, response => {
      if (rejected || res.destroyed) return response.destroy();
      const responseHeaders = cleanHeaders(response.headers);
      responseHeaders['content-security-policy'] ||= ERROR_CSP;
      responseHeaders['strict-transport-security'] = 'max-age=31536000';
      res.writeHead(response.statusCode, responseHeaders);
      response.on('error', () => res.destroy());
      response.pipe(res);
    });
    const counter = new Transform({
      transform(chunk, encoding, callback) {
        bytes += chunk.length;
        if (bytes > maxBodyBytes) {
          rejected = true;
          failure(res, 413, 'BODY_SIZE', 'Actual request body exceeds the gateway limit.');
          upstream.destroy();
          req.unpipe(counter);
          req.resume();
          callback();
        } else {
          callback(null, chunk);
        }
      }
    });
    upstream.on('timeout', () => upstream.destroy(new Error('UPSTREAM_TIMEOUT')));
    upstream.on('error', () => {
      if (!rejected) failure(res, 502, 'BACKEND_UNAVAILABLE', 'The conversion service is unavailable.');
    });
    req.on('aborted', () => upstream.destroy());
    req.on('error', () => upstream.destroy());
    res.on('close', () => { if (!res.writableFinished) upstream.destroy(); });
    req.pipe(counter).pipe(upstream);
  });
  server.requestTimeout = 150000;
  server.headersTimeout = 30000;
  server.keepAliveTimeout = 5000;
  server.on('upgrade', (req, socket) => socket.end('HTTP/1.1 400 Bad Request\r\nConnection: close\r\n\r\n'));
  return server;
}

if (require.main === module) {
  const port = Number(process.env.PORT || 8080);
  if (!Number.isInteger(port) || port < 1 || port > 65535) throw new Error('PORT must be a valid TCP port.');
  createGateway(configuration()).listen(port, '0.0.0.0', () => console.log('Authenticated pilot gateway listening.'));
}

module.exports = { createGateway, configuration, cleanHeaders };
