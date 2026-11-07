import assert from "node:assert/strict";
import { pathToFileURL } from "node:url";
const { bindFileDropZone } = await import(pathToFileURL(process.argv[2]));
class FileDrag extends Event {
  constructor(type, types = ["Files"]) { super(type, { cancelable: true }); this.dataTransfer = { types, dropEffect: "none" }; }
}
const zone = new EventTarget();
let uploads = 0, enters = 0, writable = true;
const cleanup = bindFileDropZone(zone, { enter() { enters++; }, leave() {}, drop(e) { assert.ok(e.dataTransfer.types.includes("Files")); uploads++; } }, () => writable);
const enter = new FileDrag("dragenter"); zone.dispatchEvent(enter);
const over = new FileDrag("dragover"); zone.dispatchEvent(over);
const drop = new FileDrag("drop"); zone.dispatchEvent(drop);
assert.equal(enters, 1); assert.equal(over.defaultPrevented, true); assert.equal(over.dataTransfer.dropEffect, "copy"); assert.equal(drop.defaultPrevented, true); assert.equal(uploads, 1);
console.log("PASS native file dragover accepts drop and reaches upload handler");
const internal = new FileDrag("drop", ["application/x-cloudreve-file"]); zone.dispatchEvent(internal);
assert.equal(internal.defaultPrevented, false); assert.equal(uploads, 1);
console.log("PASS internal file moves remain separate");
writable = false;
const readOnlyOver = new FileDrag("dragover"); zone.dispatchEvent(readOnlyOver);
const readOnlyDrop = new FileDrag("drop"); zone.dispatchEvent(readOnlyDrop);
assert.equal(readOnlyOver.dataTransfer.dropEffect, "none"); assert.equal(readOnlyDrop.defaultPrevented, true); assert.equal(uploads, 1);
console.log("PASS read-only targets neither upload nor navigate away");
cleanup(); writable = true;
const detached = new FileDrag("drop"); zone.dispatchEvent(detached);
assert.equal(detached.defaultPrevented, false); assert.equal(uploads, 1);
console.log("PASS unmount removes drag listeners");
console.log("4 file-drop checks passed");
