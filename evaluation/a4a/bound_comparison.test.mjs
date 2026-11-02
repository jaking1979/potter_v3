import {test} from 'node:test';
import assert from 'node:assert/strict';
import {pairIdentity} from './pair_identity.mjs';
import {compareBoundGrades} from './bound_comparison.mjs';
const grade=()=>({case_id:'synthetic',responsibility:false,outputs:{A:{done:'yes',reexplaining:'none',speed:'yes',trust:'yes'},B:{done:'no',reexplaining:'some',speed:'slow',trust:'somewhat'}}});
const pair=(a,b)=>pairIdentity('synthetic','v1',Buffer.from(a),Buffer.from(b));
test('same identity and valid grades can be compared',()=>assert.equal(compareBoundGrades(grade(),pair('a','b'),grade(),pair('a','b')).agreements,8));
test('changed or swapped output pair is refused',()=>{for(const id of [pair('b','a'),pair('a','changed')]) assert.throws(()=>compareBoundGrades(grade(),pair('a','b'),grade(),id),/pair identity mismatch/);});
test('missing malformed and mismatched grade records fail closed',()=>{assert.throws(()=>compareBoundGrades(grade(),null,grade(),null));let g=grade();g.case_id='other';assert.throws(()=>compareBoundGrades(grade(),pair('a','b'),g,pair('a','b')),/mismatched case/);});
