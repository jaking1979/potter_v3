import {test} from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {compareGrades} from './reproducibility.mjs';

const cases=JSON.parse(readFileSync(new URL('./grade_vectors.json',import.meta.url),'utf8'));
for (const c of cases) test(c.name,()=>{
  if(c.accept) assert.equal(compareGrades(c.record,c.record).disagreements.length,0);
  else assert.throws(()=>compareGrades(c.record,c.record));
});
