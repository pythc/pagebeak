import { FileType, type FileResponse, type ListResponse, type PaginationArgs } from "../../../api/explorer.ts";

// List one directory at a time. The native API returns folders first unless mixed_type is set.
export async function collectFolderChildren(
  fetchPage: (pagination: PaginationArgs) => Promise<ListResponse>,
  isCurrent: () => boolean,
): Promise<FileResponse[] | undefined> {
  const folders = new Map<string, FileResponse>();
  const seenTokens = new Set<string>();
  const seenRequests = new Set<string>();
  let pagination: PaginationArgs = { page: 0 };
  while (isCurrent()) {
    const requestKey = JSON.stringify(pagination);
    if (seenRequests.has(requestKey)) throw new Error("Repeated directory pagination page");
    seenRequests.add(requestKey);
    const result = await fetchPage(pagination);
    if (!isCurrent()) return undefined;
    result.files.filter((file) => file.type === FileType.folder).forEach((file) => folders.set(file.path, file));
    if (!result.mixed_type && result.files.some((file) => file.type === FileType.file)) break;
    const { next_token, total_items, page, page_size, is_cursor } = result.pagination;
    if (next_token) {
      if (seenTokens.has(next_token)) throw new Error("Repeated directory pagination token");
      seenTokens.add(next_token);
      pagination = { next_page_token: next_token };
    } else if (!is_cursor && total_items !== undefined && page_size > 0 && (page + 1) * page_size < total_items) {
      pagination = { page: page + 1 };
    } else {
      break;
    }
  }
  return isCurrent() ? [...folders.values()] : undefined;
}
