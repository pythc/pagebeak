// Keep OS file drops separate from Cloudreve's internal file/folder dragging.
export function bindFileDropZone(
  zone: HTMLElement,
  handlers: { enter: (e: DragEvent) => void; leave: (e: DragEvent) => void; drop: (e: DragEvent) => void },
  canUpload: () => boolean,
) {
  const containsFiles = (e: DragEvent) => !!e.dataTransfer?.types.includes("Files");
  const enter = (e: DragEvent) => { if (containsFiles(e) && canUpload()) handlers.enter(e); };
  const leave = (e: DragEvent) => { if (containsFiles(e) && canUpload()) handlers.leave(e); };
  const over = (e: DragEvent) => {
    if (!containsFiles(e)) return;
    // Without cancelling dragover, browsers do not deliver a normal file drop.
    e.preventDefault();
    if (e.dataTransfer) e.dataTransfer.dropEffect = canUpload() ? "copy" : "none";
  };
  const drop = (e: DragEvent) => {
    if (!containsFiles(e)) return;
    e.preventDefault(); // Also keep a read-only page from navigating to the dropped file.
    if (canUpload()) handlers.drop(e);
  };
  zone.addEventListener("dragenter", enter);
  zone.addEventListener("dragleave", leave);
  zone.addEventListener("dragover", over);
  zone.addEventListener("drop", drop);
  return () => {
    zone.removeEventListener("dragenter", enter);
    zone.removeEventListener("dragleave", leave);
    zone.removeEventListener("dragover", over);
    zone.removeEventListener("drop", drop);
  };
}
