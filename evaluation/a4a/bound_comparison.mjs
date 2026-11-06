// Offline draft: compare grade records only when separately recorded pair IDs
// agree. Matching caller-supplied hashes do not establish what graders saw.
import { compareGrades } from './reproducibility.mjs';
import { requireSamePair } from './pair_identity.mjs';

export function compareBoundGrades(firstGrade, firstPairId, secondGrade, secondPairId) {
  requireSamePair(firstPairId, secondPairId);
  return compareGrades(firstGrade, secondGrade);
}
