import test from "node:test";
import assert from "node:assert/strict";
import {EconomicsInputError, validateCall, priceCall, rollupTasks, estimatePreflight} from "../src/economics/run-economics.mjs";

const rate = {provider:"demo", model:"small", effective_at:"2026-01-01T00:00:00Z",
  expires_at:"2027-01-01T00:00:00Z", input_per_million:1, output_per_million:4,
  cache_read_per_million:0.1, cache_write_per_million:1.25, context_class:"standard", cache_write_class:"short"};
const make = (call_id="a", task_id="parent", override={}) => ({
  run_id:"run1", task_id, parent_task_id:task_id==="parent"?null:"parent",
  attempt:0, call_id, occurred_at:"2026-11-05T14:00:00Z",
  provider:"demo", model:"small", context_class:"standard", cache_write_class:"short",
  usage:{input_tokens:1000, output_tokens:100, cache_read_tokens:300, cache_write_tokens:200},
  ...override
});

test("prices cache read/write separately from uncached tokens",()=>{
 const q=priceCall(make(),rate);
 // 500 fresh * $1/M + 100 output * $4/M + 300 cache read * $0.1/M + 200 cache write * $1.25/M
 assert.equal(q.uncached_input_tokens,500);
 assert.equal(q.computed_cost_usd,0.00118);
 assert.equal(q.unpriced_reason,null);
 assert.equal(q.reported_cost_usd,null);
});
test("explicit zeros are billable zero, missing usage is unknown not zero",()=>{
 const zero=make("zero","parent",{usage:{input_tokens:0,output_tokens:0,cache_read_tokens:0,cache_write_tokens:0}});
 assert.equal(priceCall(zero,rate).computed_cost_usd,0);
 const missing=make("missing","parent",{usage:{input_tokens:100,output_tokens:10,cache_read_tokens:null,cache_write_tokens:null}});
 assert.equal(priceCall(missing,rate).computed_cost_usd,null);
 assert.equal(priceCall(missing,rate).unpriced_reason,"missing_usage");
 assert.equal(priceCall(make(),null).unpriced_reason,"missing_price");
});
test("prices never silently use a mismatched or stale rate",()=>{
 assert.throws(()=>priceCall(make(),{...rate, model:"different"}),EconomicsInputError);
 assert.equal(priceCall(make(),{...rate,effective_at:"2026-12-01T00:00:00Z"}).unpriced_reason,"rate_not_yet_effective");
 assert.equal(priceCall(make(),{...rate,expires_at:"2026-10-01T00:00:00Z"}).unpriced_reason,"rate_expired");
});
test("provider billed amounts remain distinct from computed costs",()=>{
 const q=priceCall(make("billed","parent",{reported_cost_usd:0.002}),rate);
 assert.equal(q.reported_cost_usd,0.002);
 assert.equal(q.computed_cost_usd,0.00118);
 assert.equal(q.billed_discrepancy_usd,0.00082);
});
test("rejects unsafe usage and all unallowlisted prompt fields",()=>{
 assert.throws(()=>validateCall({...make(),prompt:"SECRET"}),/unexpected call field/);
 assert.throws(()=>validateCall(make("negative","parent",{usage:{input_tokens:-1,output_tokens:0,cache_read_tokens:0,cache_write_tokens:0}})),EconomicsInputError);
 assert.throws(()=>validateCall(make("impossible","parent",{usage:{input_tokens:1,output_tokens:0,cache_read_tokens:2,cache_write_tokens:0}})),EconomicsInputError);
});
test("rollup includes child spend once and ignores repeated identical event delivery",()=>{
 const a=priceCall(make("a"),rate);
 const b=priceCall(make("b","child"),rate);
 const totals=rollupTasks([{task_id:"parent",parent_task_id:null},{task_id:"child",parent_task_id:"parent"}],[a,b,b]);
 assert.equal(totals.parent.calls,2);
 assert.equal(totals.child.calls,1);
 assert.equal(totals.parent.total_cost_usd,0.00236);
 assert.equal(totals.child.total_cost_usd,0.00118);
 assert.equal(totals.parent.cache_read_tokens,600);
});
test("one unknown child call makes parent total unknown with known partial amount",()=>{
 const a=priceCall(make("a"),rate);
 const b=priceCall(make("b","child"),null);
 const totals=rollupTasks([{task_id:"parent",parent_task_id:null},{task_id:"child",parent_task_id:"parent"}],[a,b]);
 assert.equal(totals.parent.total_cost_usd,null);
 assert.equal(totals.parent.known_cost_usd,0.00118);
 assert.equal(totals.parent.unpriced_calls,1);
});
test("cannot double count conflicting replays or cyclic task lineage",()=>{
 const a=priceCall(make("a"),rate);
 const changed=priceCall(make("a","parent",{usage:{input_tokens:2000,output_tokens:100,cache_read_tokens:300,cache_write_tokens:200}}),rate);
 assert.throws(()=>rollupTasks([{task_id:"parent",parent_task_id:null}],[a,changed]),/conflicting replay/);
 assert.throws(()=>rollupTasks([{task_id:"parent",parent_task_id:"child"},{task_id:"child",parent_task_id:"parent"}],[]),/cycle/);
});
test("preflight estimates only explicit scenarios and labels them heuristic",()=>{
 const mk=(label,token)=>make(label,"parent",{usage:{input_tokens:token,output_tokens:0,cache_read_tokens:0,cache_write_tokens:0}});
 const result=estimatePreflight({scenarios:{low:[mk("low",100)],likely:[mk("likely",200)],high:[mk("high",400)]},rateByModel:{"demo/small":rate}});
 assert.equal(result.status,"priced");
 assert.equal(result.scenarios.low.total_cost_usd,0.0001);
 assert.equal(result.scenarios.high.total_cost_usd,0.0004);
 assert.match(result.provenance,/heuristic/);
});
test("preflight missing prices become unknown rather than a confident quote",()=>{
 const scenarios={low:[make("low")],likely:[make("likely")],high:[make("high")]};
 const result=estimatePreflight({scenarios,rateByModel:{}});
 assert.equal(result.status,"partial_or_unknown");
 assert.equal(result.scenarios.likely.total_cost_usd,null);
 assert.deepEqual(result.scenarios.likely.missing_reasons,["missing_price"]);
});
test("impossible scenario ordering is rejected",()=>{
 const mk=(label,token)=>make(label,"parent",{usage:{input_tokens:token,output_tokens:0,cache_read_tokens:0,cache_write_tokens:0}});
 assert.throws(()=>estimatePreflight({scenarios:{low:[mk("low",300)],likely:[mk("likely",200)],high:[mk("high",100)]},rateByModel:{"demo/small":rate}}),/ordered/);
});

test("timestamps require explicit timezone",()=>{
 for (const date of ["2026-11-05T14:00:00","2026-11-05","bad"]) {
  assert.throws(()=>priceCall(make("a","parent",{occurred_at:date}),rate),/timezone/);
  assert.throws(()=>priceCall(make(),{...rate,effective_at:date}),/timezone/);
  assert.throws(()=>priceCall(make(),{...rate,expires_at:date}),/timezone/);
 }
 assert.equal(priceCall(make("a","parent",{occurred_at:"2026-11-05T09:00:00-05:00"}),rate).computed_cost_usd,0.00118);
});
test("call parent must match declared task lineage",()=>{
 const tasks=[{task_id:"parent",parent_task_id:null},{task_id:"child",parent_task_id:"parent"},{task_id:"other",parent_task_id:null}];
 const wrong=priceCall(make("x","child",{parent_task_id:"other"}),rate);
 assert.throws(()=>rollupTasks(tasks,[wrong]),/lineage/);
 const correct=priceCall(make("x","child"),rate);
 assert.equal(rollupTasks(tasks,[correct]).parent.calls,1);
});

test("rollup rejects fabricated, copied and deserialized priced calls",()=>{
 const valid=priceCall(make(),rate);
 const tasks=[{task_id:"parent",parent_task_id:null}];
 for(const untrusted of [{...valid},JSON.parse(JSON.stringify(valid)),{task_id:"parent",computed_cost_usd:0}]) {
  assert.throws(()=>rollupTasks(tasks,[untrusted]),/unverified priced call/);
 }
 assert.equal(rollupTasks(tasks,[valid]).parent.calls,1);
});

test("missing or mismatched pricing classes are unknown, never cheap estimates",()=>{
 for (const changes of [{context_class:null},{context_class:"long"},{cache_write_class:null},{cache_write_class:"long"}]) {
  const result=priceCall(make("x","parent",changes),rate);
  assert.equal(result.computed_cost_usd,null);
  assert.equal(result.unpriced_reason,"unsupported_charge");
 }
 assert.equal(priceCall(make(),{...rate,context_class:null}).unpriced_reason,"unsupported_charge");
 assert.equal(priceCall(make(),{...rate,cache_write_class:"long"}).unpriced_reason,"unsupported_charge");
 const noWrites=make("zero","parent",{cache_write_class:null,usage:{input_tokens:100,output_tokens:0,cache_read_tokens:0,cache_write_tokens:0}});
 assert.equal(priceCall(noWrites,rate).computed_cost_usd,0.0001);
});
