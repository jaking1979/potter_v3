// Offline integrity aid for fixed A/B grading fixtures. Does not prove blinding,
// presentation, independence, provenance, or authorization to run a baseline.
import { createHash } from 'node:crypto';

function frame(hash, bytes) {
  if (!Buffer.isBuffer(bytes)) throw new TypeError('fixture bytes must be Buffers');
  const length = Buffer.alloc(8);
  length.writeBigUInt64BE(BigInt(bytes.length));
  hash.update(length).update(bytes);
}

export function pairIdentity(caseId, version, outputA, outputB) {
  if (typeof caseId !== 'string' || !caseId.trim() || typeof version !== 'string' || !version.trim()) {
    throw new TypeError('case ID and version are required');
  }
  const h = createHash('sha256').update('potter-a4a-pair-v1\0');
  for (const item of [Buffer.from(caseId, 'utf8'), Buffer.from(version, 'utf8'), outputA, outputB]) frame(h, item);
  return h.digest('hex');
}

export function requireSamePair(first, second) {
  const valid = value => typeof value === 'string' && /^[0-9a-f]{64}$/.test(value);
  if (!valid(first) || !valid(second) || first !== second) throw new Error('pair identity mismatch');
}
