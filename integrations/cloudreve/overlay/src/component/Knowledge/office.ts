import { FileResponse } from "../../api/explorer.ts";
import { fileExtension, getFileLinkedUri } from "../../util";

export function usesLocalOfficePreview(file: FileResponse) {
  return getFileLinkedUri(file).startsWith("cloudreve://my/") &&
    ["doc", "docx", "xls", "xlsx", "ppt", "pptx", "odt", "ods", "odp", "rtf"].includes(fileExtension(file.name) || "");
}
