import { Alert, Box, Button, Stack, Typography, useTheme } from "@mui/material";
import i18next from "i18next";
import { useCallback, useEffect, useState } from "react";
import { getFileEntityUrl } from "../../api/api.ts";
import { closePdfViewer } from "../../redux/globalStateSlice.ts";
import { useAppDispatch, useAppSelector } from "../../redux/hooks.ts";
import { downloadFiles } from "../../redux/thunks/download.ts";
import SessionManager from "../../session";
import { getFileLinkedUri } from "../../util";
import { FileManagerIndex } from "../FileManager/FileManager.tsx";
import { knowledgeRequest } from "../Knowledge/bridge.ts";
import { usesLocalOfficePreview } from "../Knowledge/office.ts";
import ViewerDialog, { ViewerLoading } from "./ViewerDialog.tsx";

export default function PdfViewer() {
  const dispatch = useAppDispatch(), theme = useTheme();
  const viewerState = useAppSelector(state => state.globalState.pdfViewer);
  const [src, setSrc] = useState(""), [error, setError] = useState("");
  const [retry, setRetry] = useState(0);
  const owner = SessionManager.currentLoginOrNull()?.user.id;
  const target = `${owner || ''}:${viewerState?.file.id || ''}:${viewerState?.version || ''}`;
  const [displayTarget, setDisplayTarget] = useState("");
  const office = !!viewerState?.file && usesLocalOfficePreview(viewerState.file);
  const onClose = useCallback(() => { dispatch(closePdfViewer()); }, [dispatch]);

  useEffect(() => {
    setSrc(""); setError(""); setDisplayTarget(target);
    if (!viewerState?.open) return;
    let active = true, objectUrl = "";
    const load = async () => {
      try {
        const file = viewerState.file;
        let fileUrl: string;
        if (usesLocalOfficePreview(file)) {
          const blob = await knowledgeRequest("preview", {uri: getFileLinkedUri(file), file_id: file.id, version: viewerState.version});
          if (!active) return;
          objectUrl = URL.createObjectURL(blob);
          fileUrl = objectUrl;
        } else {
          const result = await dispatch(getFileEntityUrl({uris: [getFileLinkedUri(file)], entity: viewerState.version}));
          if (!active || SessionManager.currentLoginOrNull()?.user.id !== owner) return;
          fileUrl = result.urls[0].url;
        }
        const query = new URLSearchParams({file: fileUrl, lng: i18next.language, darkMode: theme.palette.mode === "dark" ? "2" : "1"});
        if (active) setSrc(`/pdfviewer.html?${query.toString()}#pagemode=none&zoom=page-width`);
      } catch(e) { if (active) setError(e instanceof Error ? e.message : "预览暂不可用，请下载原件查看"); }
    };
    void load();
    return () => { active = false; if (objectUrl) URL.revokeObjectURL(objectUrl); };
  }, [viewerState, owner, target, retry, theme.palette.mode]);

  // Never leave the previous account's preview visible after switching accounts.
  const visibleSrc = displayTarget === target ? src : "";
  const visibleError = displayTarget === target ? error : "";
  return <ViewerDialog file={viewerState?.file} readOnly={office} fullScreenToggle dialogProps={{
    open: !!viewerState?.open, onClose, fullWidth: true, maxWidth: "lg",
  }}>
    {office && <Stack direction="row" alignItems="center" spacing={1} sx={{px:2, py:1}}><Typography sx={{flex:1}} variant="caption" color="text.secondary">只读预览，原件保持不变。复杂排版、公式和动画请以原件为准。</Typography>
      {!visibleError && <Button size="small" sx={{flexShrink:0}} onClick={()=>{if (viewerState?.file) dispatch(downloadFiles(FileManagerIndex.main, [viewerState.file]));}}>下载原件</Button>}
    </Stack>}
    {visibleError ? <Stack spacing={2} sx={{p:3, minHeight:240}}><Alert severity="warning">{visibleError}</Alert>
      <Stack direction="row" spacing={1}><Button onClick={()=>setRetry(value=>value+1)}>重试预览</Button>
        <Button onClick={()=>{if (viewerState?.file) dispatch(downloadFiles(FileManagerIndex.main, [viewerState.file]));}}>下载原件</Button></Stack>
    </Stack> : !visibleSrc ? <><ViewerLoading /><Typography sx={{textAlign:'center',pb:2}}>{office ? '正在生成只读预览，通常需要几秒…' : '正在加载预览…'}</Typography></> :
      <Box component="iframe" title={`${viewerState?.file.name || '文件'}在线预览`} src={visibleSrc}
        sx={{width:'100%', flex:1, border:0, minHeight:'calc(100dvh - 220px)'}} />}
  </ViewerDialog>;
}
