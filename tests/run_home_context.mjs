#!/usr/bin/env node
// Bounded conversation-context integration: real home + real runtime.
// No home copy: imports the sibling server read-only, drives only public
// /v1 routes. Proves same-conversation history is visible to the next task
// while other-conversation / other-user facts stay excluded, and the usual
// phone post-then-task duplicate appears only once.
import assert from 'node:assert/strict';
import { mkdtemp, rm } from 'node:fs/promises';
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

function brokerFor() {
  return {
    runtime: { command: [PY, '-m', 'me_runtime.bridge'], cwd: REPO },
    model: { provider: 'fixture', model: 'fixture-echo' },
    pollMs: 25,
    termination: { termGraceMs: 500, killGraceMs: 500, wallAllowanceMs: 500, shutdownMs: 10000 },
  };
}

async function launch(t) {
  const { createHomeServer, ensureUser, createInvitation } = await import(SIBLING_SERVER);
  const dir = await mkdtemp(join(tmpdir(), 'me-ctx-home-'));
  const dbPath = join(dir, 'home.sqlite');
  const tasksRoot = join(dir, 'tasks');
  const server = createHomeServer({ dbPath, broker: { ...brokerFor(), tasksRoot } });
  const info = await server.start({ host: '127.0.0.1', port: 0 });
  const base = `http://${info.host}:${info.port}`;
  t.after(async () => {
    try { await server.stop(); } catch {}
    await rm(dir, { recursive: true, force: true });
  });
  for (const u of ['alice', 'bob']) ensureUser(server.db, u, `${u} (dev)`);
  const api = async (method, path, { token, body } = {}) => {
    const headers = {};
    if (body !== undefined) headers['content-type'] = 'application/json';
    if (token) headers.authorization = `Bearer ${token}`;
    const res = await fetch(`${base}${path}`, { method, headers, body: body === undefined ? undefined : JSON.stringify(body) });
    const text = await res.text();
    let json = null;
    try { json = JSON.parse(text); } catch {}
    return { status: res.status, json, text };
  };
  const pairAs = async (user) => {
    const inv = createInvitation(server.db, user, 60_000);
    const paired = await api('POST', '/v1/pair', { body: { invitation_id: inv.invitation_id, invitation_secret: inv.invitation_secret } });
    assert.equal(paired.status, 201);
    const sess = await api('POST', '/v1/sessions', { body: { device_id: paired.json.device_id, device_secret: paired.json.device_secret } });
    assert.equal(sess.status, 201);
    return { ...paired.json, token: sess.json.token };
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
  return { api, pairAs, waitTask, server };
}

test('context: same-conversation visible, other conv/user excluded (real home)', async (t) => {
  if (await skipIfNoHome(t)) return;
  const h = await launch(t);
  const alice = await h.pairAs('alice');
  const bob = await h.pairAs('bob');
  const fact = `comet CTX-ISO-${Date.now()}`;

  const convA = await h.api('POST', '/v1/conversations', { token: alice.token, body: {} });
  assert.equal(convA.status, 201);
  const posted = await h.api('POST', `/v1/conversations/${convA.json.id}/messages`, { token: alice.token, body: { body: `my comet is ${fact}` } });
  assert.equal(posted.status, 201);
  const taskA = await h.api('POST', `/v1/conversations/${convA.json.id}/tasks`, { token: alice.token, body: { request: 'CONTEXT_ECHO: recall my comet?' } });
  assert.equal(taskA.status, 201);
  const doneA = await h.waitTask(alice.token, taskA.json.id, 'succeeded');
  assert.match(doneA.output, new RegExp(fact.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')));

  // Other conversation (same user): excluded via home scoping.
  const convB = await h.api('POST', '/v1/conversations', { token: alice.token, body: {} });
  const taskB = await h.api('POST', `/v1/conversations/${convB.json.id}/tasks`, { token: alice.token, body: { request: 'CONTEXT_ECHO: recall my comet?' } });
  const doneB = await h.waitTask(alice.token, taskB.json.id, 'succeeded');
  assert.doesNotMatch(doneB.output, new RegExp(fact.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')));

  // Other user: excluded via home owner scoping.
  const convBob = await h.api('POST', '/v1/conversations', { token: bob.token, body: {} });
  const taskBob = await h.api('POST', `/v1/conversations/${convBob.json.id}/tasks`, { token: bob.token, body: { request: 'CONTEXT_ECHO: recall my comet?' } });
  const doneBob = await h.waitTask(bob.token, taskBob.json.id, 'succeeded');
  assert.doesNotMatch(doneBob.output, new RegExp(fact.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')));
});

test('context: phone post-then-task duplicate appears once (real home)', async (t) => {
  if (await skipIfNoHome(t)) return;
  const h = await launch(t);
  const alice = await h.pairAs('alice');
  const conv = await h.api('POST', '/v1/conversations', { token: alice.token, body: {} });
  const dupReq = `CONTEXT_ECHO dup-phone-${Date.now()}`;
  const posted = await h.api('POST', `/v1/conversations/${conv.json.id}/messages`, { token: alice.token, body: { body: dupReq } });
  assert.equal(posted.status, 201);
  const task = await h.api('POST', `/v1/conversations/${conv.json.id}/tasks`, { token: alice.token, body: { request: dupReq } });
  const done = await h.waitTask(alice.token, task.json.id, 'succeeded');
  // Dedup of the trailing identical turn: current text exactly once.
  const needle = dupReq.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
  const count = (done.output.match(new RegExp(needle, 'g')) || []).length;
  assert.equal(count, 1);
});
