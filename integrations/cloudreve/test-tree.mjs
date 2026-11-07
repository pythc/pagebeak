import assert from "node:assert/strict";
import { pathToFileURL } from "node:url";

const { collectFolderChildren } = await import(pathToFileURL(process.argv[2]));
const folder = (name) => ({ name, path: `cloudreve://my/${name}`, type: 1 });
const file = { name: "readme.txt", path: "cloudreve://my/readme.txt", type: 0 };
const page = (files, pagination = {}, mixed_type = false) => ({ files, pagination: { page: 0, page_size: 2, ...pagination }, mixed_type });
let passed = 0;
async function check(name, fn) { await fn(); passed++; console.log(`PASS ${name}`); }

await check("empty folders remain empty", async () => {
  assert.deepEqual(await collectFolderChildren(async () => page([], { total_items: 0 }), () => true), []);
});
await check("stop before unrelated file pages", async () => {
  let calls = 0;
  const result = await collectFolderChildren(async () => { calls++; return page([folder("A"), file], { total_items: 100 }); }, () => true);
  assert.equal(calls, 1); assert.deepEqual(result.map((f) => f.name), ["A"]);
});
await check("folder pages are not truncated", async () => {
  const requests = [];
  const result = await collectFolderChildren(async (p) => {
    requests.push(p);
    return p.page === 0 ? page([folder("A"), folder("B")], { total_items: 3 }) : page([folder("C")], { page: 1, total_items: 3 });
  }, () => true);
  assert.deepEqual(requests, [{ page: 0 }, { page: 1 }]); assert.equal(result.length, 3);
});
await check("cursor pages deduplicate folders", async () => {
  const result = await collectFolderChildren(async (p) => p.next_page_token ? page([folder("A"), folder("B")], { is_cursor: true }) : page([folder("A")], { is_cursor: true, next_token: "next" }), () => true);
  assert.equal(result.length, 2);
});
await check("mixed listings continue after a file", async () => {
  const result = await collectFolderChildren(async (p) => p.page === 0 ? page([file], { page_size: 1, total_items: 2 }, true) : page([folder("A")], { page: 1, page_size: 1, total_items: 2 }, true), () => true);
  assert.deepEqual(result.map((f) => f.name), ["A"]);
});
await check("account changes discard stale results", async () => {
  let current = true;
  assert.equal(await collectFolderChildren(async () => { current = false; return page([folder("private")]); }, () => current), undefined);
  let called = false;
  assert.equal(await collectFolderChildren(async () => { called = true; return page([]); }, () => false), undefined);
  assert.equal(called, false);
});
await check("broken pagination cannot loop forever", async () => {
  await assert.rejects(collectFolderChildren(async () => page([folder("A")], { next_token: "same" }), () => true), /Repeated directory pagination token/);
  await assert.rejects(collectFolderChildren(async () => page([folder("A")], { page: 0, page_size: 1, total_items: 3 }), () => true), /Repeated directory pagination page/);
});
await check("errors propagate without marking a branch complete", async () => {
  await assert.rejects(collectFolderChildren(async () => { throw new Error("unavailable"); }, () => true), /unavailable/);
});
console.log(`${passed} directory-tree checks passed`);
