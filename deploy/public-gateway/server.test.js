'use strict';

const { test } = require('node:test');
const assert = require('node:assert/strict');
const http = require('node:http');
const { once } = require('node:events');
const { createGateway, configuration } = require('./server');

const origin = new URL('https://pbip-test.azurewebsites.net');
const headers = { Host: origin.host, 'X-Forwarded-Proto': 'https' };

async function fixture(t, handler, options = {}) {
  const backend = http.createServer(handler).listen(0, '127.0.0.1');
  await once(backend, 'listening');
  const gateway = createGateway({
    origin, backend: new URL(`http://127.0.0.1:${backend.address().port}`), ...options
  }).listen(0, '127.0.0.1');
  await once(gateway, 'listening');
  t.after(() => {
    gateway.closeAllConnections(); gateway.close();
    backend.closeAllConnections(); backend.close();
  });
  return gateway.address().port;
}

function request(port, options = {}, chunks = []) {
  return new Promise((resolve, reject) => {
    const req = http.request({
      hostname: '127.0.0.1', port, path: '/', ...options,
      headers: { ...headers, ...options.headers }
    }, res => {
      const data = [];
      res.on('data', chunk => data.push(chunk));
      res.on('end', () => resolve({ status: res.statusCode, headers: res.headers, data: Buffer.concat(data) }));
      res.on('error', reject);
    });
    req.on('error', reject);
    for (const chunk of chunks) req.write(chunk);
    req.end();
  });
}

test('configuration accepts host/port or URL, rejects non-private upstream and insecure origin', () => {
  const env = { PUBLIC_ORIGIN: origin.origin, BACKEND_HOST: '10.0.0.4', BACKEND_PORT: '8765' };
  assert.equal(configuration(env).backend.href, 'http://10.0.0.4:8765/');
  assert.equal(configuration({ ...env, BACKEND_URL: 'http://192.168.0.4:8765' }).backend.hostname, '192.168.0.4');
  for (const BACKEND_URL of ['http://127.0.0.1:8765', 'http://example.org:8765', 'http://10.0.0.4:8765/path']) {
    assert.throws(() => configuration({ ...env, BACKEND_URL }));
  }
  assert.throws(() => configuration({ ...env, PUBLIC_ORIGIN: 'http://pbip-test.azurewebsites.net' }));
});

test('streams methods and bytes, preserves response cookies/disposition/status/CSP and fixes proxy headers', async t => {
  const body = Buffer.alloc(16 * 1024 * 1024, 97);
  const port = await fixture(t, (req, res) => {
    assert.equal(req.method, 'POST');
    assert.equal(req.url, '/api/jobs?direction=auto');
    assert.equal(req.headers.host, origin.host);
    assert.equal(req.headers['x-forwarded-host'], origin.host);
    assert.equal(req.headers['x-forwarded-proto'], 'https');
    assert.equal(req.headers['x-forwarded-for'], undefined);
    assert.equal(req.headers.forwarded, undefined);
    assert.equal(req.headers.origin, origin.origin);
    assert.equal(req.headers.authorization, 'Bearer test-only');
    assert.equal(req.headers['x-strip-me'], undefined);
    let bytes = 0;
    req.on('data', chunk => { bytes += chunk.length; });
    req.on('end', () => {
      assert.equal(bytes, body.length);
      res.writeHead(201, {
        'Set-Cookie': ['a=1; Secure; HttpOnly', 'b=2; SameSite=Strict'],
        'Content-Disposition': 'attachment; filename="test.pbix"',
        'Content-Security-Policy': "script-src 'sha256-original'",
        'Connection': 'x-response-hop',
        'X-Response-Hop': 'remove'
      });
      res.end(body);
    });
  });
  const result = await request(port, {
    method: 'POST', path: '/api/jobs?direction=auto',
    headers: {
      Origin: origin.origin, Authorization: 'Bearer test-only',
      'Content-Type': 'multipart/form-data; boundary=test',
      Connection: 'x-strip-me', 'X-Strip-Me': 'remove',
      'X-Forwarded-Host': 'attacker.example', Forwarded: 'host=attacker.example',
      'X-Forwarded-For': '127.0.0.1'
    }
  }, [body.subarray(0, 65536), body.subarray(65536)]);
  assert.equal(result.status, 201);
  assert.deepEqual(result.data, body);
  assert.equal(result.headers['set-cookie'].length, 2);
  assert.equal(result.headers['content-disposition'], 'attachment; filename="test.pbix"');
  assert.equal(result.headers['content-security-policy'], "script-src 'sha256-original'");
  assert.equal(result.headers['x-response-hop'], undefined);
});

test('forwards upload chunks before request end rather than buffering the whole body', async t => {
  let observed;
  const received = new Promise(resolve => { observed = resolve; });
  const port = await fixture(t, (req, res) => {
    req.once('data', observed);
    req.on('end', () => res.end('ok'));
    req.resume();
  });
  const req = http.request({ hostname: '127.0.0.1', port, method: 'POST', headers });
  req.on('error', () => {});
  const response = once(req, 'response');
  req.write('first');
  await Promise.race([received, new Promise((_, reject) => {
    const timer = setTimeout(() => reject(new Error('Upstream did not receive streaming bytes')), 2000);
    timer.unref();
  })]);
  req.end('last');
  (await response)[0].resume();
});

test('rejects wrong host/origin/protocol and counts actual chunked bytes', async t => {
  const port = await fixture(t, (req, res) => { req.resume(); req.on('end', () => res.end('ok')); }, { maxBodyBytes: 20 });
  for (const extra of [{ Host: 'evil.example' }, { Origin: 'https://evil.example' }, { 'X-Forwarded-Proto': 'http' }]) {
    const result = await request(port, { method: 'POST', headers: extra }, ['x']);
    assert.equal(result.status, 403);
    assert.ok(result.headers['content-security-policy']);
  }
  const result = await request(port, { method: 'POST' }, ['123456789012345', '123456789012345']);
  assert.equal(result.status, 413);
});

test('health requires actual backend authentication response without exposing backend details', async t => {
  let healthy = true;
  const port = await fixture(t, (req, res) => {
    assert.equal(req.url, '/api/session');
    assert.equal(req.headers.host, origin.host);
    res.writeHead(healthy ? 401 : 500, { 'Content-Type': 'application/json' });
    res.end(JSON.stringify({ ok: false, error: { code: healthy ? 'AUTH_REQUIRED' : 'PRIVATE_ERROR' } }));
  });
  let result = await request(port, { path: '/healthz' });
  assert.equal(result.status, 200);
  assert.deepEqual(JSON.parse(result.data), { ok: true });
  healthy = false;
  result = await request(port, { path: '/healthz' });
  assert.equal(result.status, 503);
  assert.doesNotMatch(result.data.toString(), /PRIVATE_ERROR|10\.44/);
});

test('client download cancellation closes the upstream streaming response', async t => {
  let closed;
  const upstreamClosed = new Promise(resolve => { closed = resolve; });
  const port = await fixture(t, (req, res) => {
    res.writeHead(200);
    res.write(Buffer.alloc(1024));
    res.on('close', closed);
  });
  const req = http.get({ hostname: '127.0.0.1', port, headers }, res => {
    res.once('data', () => res.destroy());
  });
  req.on('error', () => {});
  await Promise.race([upstreamClosed, new Promise((_, reject) => {
    const timer = setTimeout(() => reject(new Error('Upstream stream remained open')), 2000);
    timer.unref();
  })]);
});
