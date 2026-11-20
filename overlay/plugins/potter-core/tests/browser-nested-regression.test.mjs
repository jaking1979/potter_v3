import test from "node:test";
import assert from "node:assert/strict";
import { createGuard } from "../lib/browserguard.js";
import { isOutgoing, outgoingText, detect } from "../lib/outbound.js";

test("nested click matches the consequential snapshot reference", () => {
  const guard = createGuard();
  guard.observe("s", {action:"snapshot",targetId:"t"}, 'button "Place order" [ref=e12]');
  const flat = guard.classify("s", {action:"act",targetId:"t",kind:"click",ref:"e12"});
  const nested = guard.classify("s", {action:"act",targetId:"t",request:{kind:"click",ref:"e12"}});
  assert.equal(flat.risk, "consequential");
  assert.equal(nested.risk, "consequential");
});
test("nested typing passes through outbound inspection", () => {
  const params = {action:"act",request:{kind:"type",text:"111-22-3333"}};
  assert.equal(isOutgoing("browser",params),true);
  assert.deepEqual(detect(outgoingText(params)),["Social Security number"]);
});
