import {test} from 'node:test';
import assert from 'node:assert/strict';
import {compareGrades} from './reproducibility.mjs';
const grade=()=>({case_id:'synthetic',responsibility:false,outputs:{A:{done:'yes',reexplaining:'none',speed:'yes',trust:'yes'},B:{done:'no',reexplaining:'some',speed:'slow',trust:'somewhat'}}});
test('identical grades agree on every field',()=>assert.deepEqual(compareGrades(grade(),grade()),{case_id:'synthetic',comparisons:8,agreements:8,disagreements:[]}));
test('disagreement reports field and answers',()=>{let a=grade(),b=grade();b.outputs.B.speed='too slow';assert.deepEqual(compareGrades(a,b).disagreements,[{label:'B',dimension:'speed',first:'slow',second:'too slow'}]);});
test('mismatched cases, incomplete grades and identifying fields rejected',()=>{for(let f of [x=>x.case_id='other',x=>delete x.outputs.A.done,x=>x.outputs.A.model='name']){let a=grade(),b=grade();f(b);assert.throws(()=>compareGrades(a,b));}});
test('responsibility grading requires follow through twice',()=>{let a=grade(),b=grade();a.responsibility=b.responsibility=true;assert.throws(()=>compareGrades(a,b));for(let x of [a,b]) for(let k of ['A','B']) x.outputs[k].follow_through='partly';assert.equal(compareGrades(a,b).comparisons,10);});
