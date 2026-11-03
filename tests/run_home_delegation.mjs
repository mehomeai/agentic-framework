#!/usr/bin/env node
// R8 owned integration: real home (sibling infra-server, no duplication) +
// real Python runtime bridge (me_runtime.bridge) proving durable delegation.
// Strict gated skip only if sibling missing; ME_RUNTIME_REQUIRE_HOME=1 fails.
//
// Cases: delegate->waiting_input with child finishing before answer + restart,
// wait->CAS wake, multiple children, file/tool mtime unchanged, idempotency,
// child failure, parent cancel cascade, cross-user denials, disabled/denials.
import assert from 'node:assert/strict';
import { mkdtemp, rm, stat } from 'node:fs/promises';
import { existsSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join, dirname, resolve as resolvePath } from 'node:path';
import { fileURLToPath } from 'node:url';
import test from 'node:test';

const HERE = dirname(fileURLToPath(import.meta.url));
const REPO = resolvePath(HERE, '..');
const SIBLING = resolvePath(REPO, '..', 'infra-server');
const SIBLING_SERVER = join(SIBLING, 'src', 'server.mjs');

const REQUIRE_HOME = process.env.ME_RUNTIME_REQUIRE_HOME === '1';
const PY = join(REPO, '.venv', 'bin', 'python');

async function hasSibling() {
  return existsSync(SIBLING_SERVER) && existsSync(PY);
}

const skipIfNoHome = async (t) => {
  if (!(await hasSibling())) {
    if (REQUIRE_HOME) throw new Error(`sibling home missing at ${SIBLING}`);
    t.skip('sibling infra-server or venv missing (gated skip)');
    return true;
  }
  return false;
};

async function loadHome() {
  const srv = await import(SIBLING_SERVER);
  return srv;
}

function brokerFor() {
  return {
    runtime: { command: [PY, '-m', 'me_runtime.bridge'], cwd: REPO },
    model: { provider: 'fixture', model: 'fixture-echo' },
    pollMs: 25,
    termination: { termGraceMs: 500, killGraceMs: 500, wallAllowanceMs: 500, shutdownMs: 10000 },
  };
}

async function launch(t, { brokerOverrides = {}, delegation } = {}) {
  const { createHomeServer, ensureUser, createInvitation } = await loadHome();
  const dir = await mkdtemp(join(tmpdir(), 'me-r8-home-'));
  const dbPath = join(dir, 'home.sqlite');
  const tasksRoot = join(dir, 'tasks');
  const broker = { ...brokerFor(), ...brokerOverrides };
  if (delegation) broker.delegation = delegation;
  const start = async () => {
    const server = createHomeServer({ dbPath, broker: { ...broker, tasksRoot } });
    const info = await server.start({ host: '127.0.0.1', port: 0 });
    return { server, base: `http://${info.host}:${info.port}` };
  };
  let current = await start();
  t.after(async () => {
    try { await current.server.stop(); } catch {}
    await rm(dir, { recursive: true, force: true });
  });
  for (const u of ['alice', 'bob']) ensureUser(current.server.db, u, `${u} (dev)`);
  const api = async (method, path, { token, body } = {}) => {
    const headers = {};
    if (body !== undefined) headers['content-type'] = 'application/json';
    if (token) headers.authorization = `Bearer ${token}`;
    const res = await fetch(`${current.base}${path}`, { method, headers, body: body === undefined ? undefined : JSON.stringify(body) });
    const text = await res.text();
    let json = null;
    try { json = JSON.parse(text); } catch {}
    return { status: res.status, json, text };
  };
  const pairAs = async (user) => {
    const inv = createInvitation(current.server.db, user, 60_000);
    const paired = await api('POST', '/v1/pair', { body: { invitation_id: inv.invitation_id, invitation_secret: inv.invitation_secret } });
    assert.equal(paired.status, 201);
    const sess = await api('POST', '/v1/sessions', { body: { device_id: paired.json.device_id, device_secret: paired.json.device_secret } });
    assert.equal(sess.status, 201);
    return { ...paired.json, token: sess.json.token };
  };
  const restart = async () => {
    await current.server.stop();
    current = await start();
  };
  const waitTask = async (token, taskId, wants, timeoutMs = 30000) => {
    const want = Array.isArray(wants) ? wants : [wants];
    const deadline = Date.now() + timeoutMs;
    for (;;) {
      const res = await api('GET', `/v1/tasks/${taskId}`, { token });
      assert.equal(res.status, 200);
      if (want.includes(res.json.status)) return res.json;
      if (['succeeded', 'failed', 'cancelled'].includes(res.json.status)) {
        throw new Error(`settled as ${res.json.status} (${res.json.error ?? ''}) waiting for ${want}`);
      }
      if (Date.now() > deadline) throw new Error(`timeout waiting for ${want}; last=${res.json.status}`);
      await new Promise((r) => setTimeout(r, 50));
    }
  };
  return { api, pairAs, restart, waitTask, get server() { return current.server; }, tasksRoot, dir };
}

test('R8: delegate->waiting_input, child finishes first, restart, aggregate single message', async (t) => {
  if (await skipIfNoHome(t)) return;
  const h = await launch(t);
  const alice = await h.pairAs('alice');
  const conv = await h.api('POST', '/v1/conversations', { token: alice.token, body: {} });
  // Child does a real tool write (WRITE_CHILD), parent asks after delegating.
  const sub = await h.api('POST', `/v1/conversations/${conv.json.id}/tasks`, {
    token: alice.token, body: { request: 'parent\nDELEGATE:r1:WRITE_CHILD:hello from child\nASK:Which format?', title: 'Parent' },
  });
  assert.equal(sub.status, 201);
  const waiting = await h.waitTask(alice.token, sub.json.id, 'waiting_input');
  assert.equal(waiting.questions[0].id, 'q1');
  assert.ok(Array.isArray(waiting.children) && waiting.children.length === 1);
  const childId = waiting.children[0];
  // Child settles while parent waits for human: parent must stay put.
  const childDone = await h.waitTask(alice.token, childId, 'succeeded');
  assert.match(childDone.output, /child done|hello from child/);
  await new Promise((r) => setTimeout(r, 500));
  assert.equal((await h.api('GET', `/v1/tasks/${sub.json.id}`, { token: alice.token })).json.status, 'waiting_input');
  // Restart both home+bridge checkpoint (same db/tasksRoot, new server).
  const mtimeBefore = (await stat(join(h.tasksRoot, childId, 'output', 'child.md'))).mtimeNs;
  await h.restart();
  const answered = await h.api('POST', `/v1/tasks/${sub.json.id}/inputs`, { token: alice.token, body: { inputs: [{ id: 'q1', answer: 'MP4' }] } });
  assert.equal(answered.status, 200);
  const done = await h.waitTask(alice.token, sub.json.id, 'succeeded');
  assert.match(done.output, /MP4/);
  assert.match(done.output, /task_|child/i);
  // Single parent assistant message (no premature publish, no duplicate).
  const msgs = (await h.api('GET', `/v1/conversations/${conv.json.id}/messages`, { token: alice.token })).json.messages;
  assert.equal(msgs.filter((m) => m.role === 'assistant').length, 1);
  assert.equal(msgs.filter((m) => m.role === 'assistant')[0].task_id, sub.json.id);
  // Existing completed child file unchanged across resume (no replay).
  const mtimeAfter = (await stat(join(h.tasksRoot, childId, 'output', 'child.md'))).mtimeNs;
  assert.equal(mtimeAfter, mtimeBefore);
});

test('R8: wait->CAS wake, multiple children aggregate', async (t) => {
  if (await skipIfNoHome(t)) return;
  const h = await launch(t);
  const alice = await h.pairAs('alice');
  const conv = await h.api('POST', '/v1/conversations', { token: alice.token, body: {} });
  const sub = await h.api('POST', `/v1/conversations/${conv.json.id}/tasks`, {
    token: alice.token, body: { request: 'fan\nDELEGATE:a:WRITE_CHILD:alpha\nDELEGATE:b:WRITE_CHILD:beta\nDELEGATE_WAIT' },
  });
  const parked = await h.waitTask(alice.token, sub.json.id, 'waiting_children');
  assert.equal(parked.children.length, 2);
  const done = await h.waitTask(alice.token, sub.json.id, 'succeeded');
  assert.match(done.output, /succeeded/);
  const listed = (await h.api('GET', `/v1/conversations/${conv.json.id}/tasks`, { token: alice.token })).json.tasks;
  assert.equal(listed.filter((r) => r.parent_task_id === sub.json.id).length, 2);
});

test('R8: premature completed held, not published', async (t) => {
  if (await skipIfNoHome(t)) return;
  const h = await launch(t);
  const alice = await h.pairAs('alice');
  const conv = await h.api('POST', '/v1/conversations', { token: alice.token, body: {} });
  const sub = await h.api('POST', `/v1/conversations/${conv.json.id}/tasks`, {
    token: alice.token, body: { request: 'eager\nDELEGATE:k1:WRITE_CHILD:work\nCOMPLETE_EARLY' },
  });
  const parked = await h.waitTask(alice.token, sub.json.id, 'waiting_children');
  assert.equal(parked.children.length, 1);
  const before = (await h.api('GET', `/v1/conversations/${conv.json.id}/messages`, { token: alice.token })).json.messages;
  assert.equal(before.filter((m) => m.role === 'assistant').length, 0);
  const done = await h.waitTask(alice.token, sub.json.id, 'succeeded');
  assert.match(done.output, /parent done/);
});

test('R8: idempotency across resume, child failure honest', async (t) => {
  if (await skipIfNoHome(t)) return;
  const h = await launch(t);
  const alice = await h.pairAs('alice');
  const conv = await h.api('POST', `/v1/conversations`, { token: alice.token, body: {} });
  // Dup: same key retry + conflict probe, then wait.
  const dup = await h.api('POST', `/v1/conversations/${conv.json.id}/tasks`, {
    token: alice.token, body: { request: 'dup\nDELEGATE_DUP:dupkey:WRITE_CHILD:hi\nDELEGATE_WAIT' },
  });
  const done = await h.waitTask(alice.token, dup.json.id, 'succeeded');
  assert.match(done.output, /dup ok/);
  const listed = (await h.api('GET', `/v1/conversations/${conv.json.id}/tasks`, { token: alice.token })).json.tasks;
  assert.equal(listed.filter((r) => r.parent_task_id === dup.json.id).length, 1);
  // Child failure aggregated honestly (parent succeeds with error data).
  const conv2 = await h.api('POST', '/v1/conversations', { token: alice.token, body: {} });
  const fail = await h.api('POST', `/v1/conversations/${conv2.json.id}/tasks`, {
    token: alice.token, body: { request: 'pfail\nDELEGATE:f1:FAIL:boom child\nDELEGATE_WAIT' },
  });
  const failedParent = await h.waitTask(alice.token, fail.json.id, 'succeeded');
  assert.match(failedParent.output, /failed|boom/i);
});

test('R8: parent cancel cascades, no orphans; cross-user safe', async (t) => {
  if (await skipIfNoHome(t)) return;
  const h = await launch(t);
  const alice = await h.pairAs('alice');
  const bob = await h.pairAs('bob');
  const conv = await h.api('POST', '/v1/conversations', { token: alice.token, body: {} });
  const bconv = await h.api('POST', '/v1/conversations', { token: bob.token, body: {} });
  const sub2 = await h.api('POST', `/v1/conversations/${conv.json.id}/tasks`, {
    token: alice.token, body: { request: 'long\nDELEGATE:slow:SLEEP:30\nDELEGATE_WAIT' },
  });
  const parked = await h.waitTask(alice.token, sub2.json.id, 'waiting_children');
  const childId = parked.children[0];
  await h.waitTask(alice.token, childId, 'running');
  const bsub = await h.api('POST', `/v1/conversations/${bconv.json.id}/tasks`, {
    token: bob.token, body: { request: 'bob work\nSLEEP:5' },
  });
  await h.waitTask(bob.token, bsub.json.id, 'running');
  const cancelled = await h.api('POST', `/v1/tasks/${sub2.json.id}/cancel`, { token: alice.token });
  assert.equal(cancelled.status, 200);
  assert.equal((await h.api('GET', `/v1/tasks/${childId}`, { token: alice.token })).json.status, 'cancelled');
  assert.equal((await h.api('GET', `/v1/tasks/${bsub.json.id}`, { token: bob.token })).json.status, 'running');
  await h.api('POST', `/v1/tasks/${bsub.json.id}/cancel`, { token: bob.token });
  // Cross-user: bob cannot read alice tasks.
  assert.equal((await h.api('GET', `/v1/conversations/${conv.json.id}/tasks`, { token: bob.token })).status, 404);
});

test('R8: disabled/denials honest, no bomb', async (t) => {
  if (await skipIfNoHome(t)) return;
  const h = await launch(t, { delegation: { maxFanout: 0, maxDepth: 0, maxTasksPerRoot: 0 } });
  const alice = await h.pairAs('alice');
  const conv = await h.api('POST', '/v1/conversations', { token: alice.token, body: {} });
  const sub = await h.api('POST', `/v1/conversations/${conv.json.id}/tasks`, {
    token: alice.token, body: { request: 'x\nDELEGATE:k:hi\nDELEGATE_WAIT' },
  });
  const settled = await h.waitTask(alice.token, sub.json.id, 'failed');
  assert.match(settled.error, /delegation_denied|disabled|denied/i);
});

test('R9: structured content-block final publishes single assistant message', async (t) => {
  if (await skipIfNoHome(t)) return;
  const h = await launch(t);
  const alice = await h.pairAs('alice');
  const conv = await h.api('POST', '/v1/conversations', { token: alice.token, body: {} });
  for (const directive of ['STRUCTURED:structured ORBIT English', 'STRUCTURED_RESP:responses ORBIT English']) {
    const sub = await h.api('POST', `/v1/conversations/${conv.json.id}/tasks`, {
      token: alice.token, body: { request: directive },
    });
    assert.equal(sub.status, 201);
    const done = await h.waitTask(alice.token, sub.json.id, 'succeeded');
    assert.match(done.output, /ORBIT/);
    assert.match(done.output, /English/);
  }
  const msgs = (await h.api('GET', `/v1/conversations/${conv.json.id}/messages`, { token: alice.token })).json.messages;
  assert.equal(msgs.filter((m) => m.role === 'assistant').length, 2);
});
