import { test } from 'node:test';
import assert from 'node:assert/strict';
import { pairIdentity, requireSamePair } from './pair_identity.mjs';
const pair = (a='first', b='second', id='case-1', version='v1') => pairIdentity(id, version, Buffer.from(a), Buffer.from(b));
test('same versioned pair produces the same identity', () => { assert.equal(pair(), pair()); requireSamePair(pair(), pair()); });
test('any label, byte, case, or version change is detected', () => {
  for (const other of [pair('second','first'), pair('First','second'), pair('first','second','case-2'), pair('first','second','case-1','v2')]) assert.throws(() => requireSamePair(pair(), other));
});
test('length framing avoids concatenation ambiguity', () => assert.notEqual(pair('ab','c'), pair('a','bc')));
test('rejects malformed identifiers and non-buffer fixtures', () => {
  assert.throws(() => pairIdentity('', 'v1', Buffer.from('a'), Buffer.from('b')));
  assert.throws(() => pairIdentity('case', 'v1', 'a', Buffer.from('b')));
  for (const bad of ['', 'a'.repeat(63), 'G'.repeat(64), null]) assert.throws(() => requireSamePair(bad, bad));
});
