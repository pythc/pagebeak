import { useCallback, useEffect, useRef, useState } from "react";
import { Alert, Box, Button, Chip, CircularProgress, Dialog, DialogActions, DialogContent, DialogTitle, Divider, Paper, Stack, TextField, Typography } from "@mui/material";
import SessionManager from "../../session";
import { knowledgeRequest } from "./bridge";
import LocalCatalogSection from "./LocalCatalogSection";

type Citation = {statement: string; evidence_id: string; source_version: string; source_title?: string; locator?: Record<string,unknown>; start:number; end:number; quote: string | null; current:boolean};
type Item = {id: string; kind: string; key: string; title: string; revision: number; current: boolean;
  citations?: Citation[]; evidence_status?: string; protected: boolean; source_versions: Record<string,string>; content?: string | null; historical_content?: string; warning?: string; expires_at?: string | null};
type Library = {items: Item[]; total: number; has_more: boolean; offset: number; pending: number;
  local_catalog?: {devices:{id:string;updated_at:string}[]};
  sources: {id: string; title: string; semantic_status: string; coverage: string}[]};
const labels: Record<string,string> = {page: "知识页", preference: "偏好", episode: "任务记忆"};
const locationLabel = (locator?: Record<string,unknown>) => {
  if (!locator) return "原文位置已失效";
  const parts: string[] = [];
  if (typeof locator.sheet === "string") parts.push(`工作表「${locator.sheet}」`);
  for (const [key, label] of [["page","页"],["slide","张幻灯片"],["paragraph","段"],["table","个表格"],["row","行"],["line","行"]]) {
    if (typeof locator[key] === "number") parts.push(`第 ${locator[key]} ${label}`);
  }
  if (typeof locator.range === "string") parts.push(locator.range);
  if (locator.method === "ocr") parts.push("图像识别原文");
  return parts.join(" · ") || "原文片段";
};

export default function LibraryPage() {
  const [data, setData] = useState<Library | null>(null), [selected, setSelected] = useState<Item | null>(null);
  const [error, setError] = useState(""), [notice, setNotice] = useState(""), [busy, setBusy] = useState(false);
  const [editing, setEditing] = useState(false), [draft, setDraft] = useState(""), [title, setTitle] = useState("");
  const [deleting, setDeleting] = useState(false), [offset, setOffset] = useState(0);
  const account = SessionManager.currentLoginOrNull()?.user.id, epoch = useRef(0);
  const fail = (e: unknown) => setError(e instanceof Error ? e.message : "操作未完成，请重试");
  const load = useCallback(async () => {
    const stamp = epoch.current;
    const next = await knowledgeRequest(`library?offset=${offset}&limit=30`);
    if (stamp === epoch.current) setData(next);
  }, [offset]);
  useEffect(() => {
    epoch.current += 1; setSelected(null); setData(null); setEditing(false); setError(""); setNotice("");
    void load().catch(fail);
    return () => {epoch.current += 1;};
  }, [account, load]);
  const read = async (item: Item) => {
    setBusy(true); setError(""); const stamp = epoch.current;
    try {const next = await knowledgeRequest(`library/objects/${item.id}`);
      if (stamp === epoch.current) {setSelected(next); setEditing(false);}}
    catch(e) {fail(e);} finally {setBusy(false);}
  };
  const save = async () => {
    if (!selected) return; setBusy(true); setError("");
    try {
      const result = await knowledgeRequest("library/objects", {request_id: crypto.randomUUID(), kind: selected.kind,
        key: selected.key, title, content: draft, expected_revision: selected.revision, source_versions: selected.source_versions,
        citations: (selected.citations || []).filter(c=>c.current && c.quote && draft.includes(c.statement)).map(c=>({statement:c.statement,evidence:{evidence_id:c.evidence_id,source_version:c.source_version,quote:c.quote,start:c.start}})),
        expires_at: selected.expires_at || null});
      setSelected(await knowledgeRequest(`library/objects/${result.id}`)); setEditing(false); setNotice("已保存，你的修改会被保留。"); await load();
    } catch(e) {fail(e);} finally {setBusy(false);}
  };
  const forget = async () => {
    if (!selected) return; setBusy(true); setError("");
    try {const result = await knowledgeRequest(`library/objects/${selected.id}/forget`, {expected_revision: selected.revision});
      setSelected(null); setDeleting(false); setNotice(result.purge_pending ? "已停止召回，存储清理正在重试。" : "已遗忘此条目，原文件仍保留。"); await load();
    } catch(e) {fail(e);} finally {setBusy(false);}
  };
  return <Box role="region" aria-label="知识目录内容" tabIndex={0} sx={{flex: "1 1 0", minHeight:0, overflowY:"auto", width:"100%"}}>
    <Box sx={{maxWidth:1100, mx:"auto", p:{xs:2,md:4}}}>
      <Stack direction="row" justifyContent="space-between" alignItems="center" sx={{mb:2}}>
        <Typography variant="h4" component="h1">知识目录</Typography><Button disabled={busy} onClick={()=>load().catch(fail)}>刷新</Button>
      </Stack>
      <Typography color="text.secondary" sx={{mb:3}}>你和助手共用的资料导航、知识页与记忆。打开页面可查看内容、修改或遗忘。</Typography>
      {error && <Alert severity="error" sx={{mb:2}}>{error}</Alert>}{notice && <Alert severity="info" sx={{mb:2}}>{notice}</Alert>}
      {!data ? !error && <CircularProgress size={22}/> : <>
        {data.pending > 0 && <Typography color="text.secondary" sx={{mb:2}}>有 {data.pending} 份资料待整理，下次与 WorkBuddy 交流时可让助手继续。</Typography>}
        <Stack direction={{xs:"column",md:"row"}} spacing={3} alignItems="stretch">
          <Stack spacing={1} sx={{width:{xs:"100%",md:330}, flexShrink:0}}>
            {data.items.length === 0 && <Paper variant="outlined" sx={{p:2}}>还没有知识页。上传资料后，让 WorkBuddy 整理，就会出现在这里。</Paper>}
            {data.items.map(item=><Paper key={item.id} variant="outlined" sx={{p:2, borderColor:selected?.id === item.id ? "primary.main" : undefined}}>
              <Button sx={{justifyContent:"flex-start", textAlign:"left", p:0}} disabled={busy} onClick={()=>read(item)}>{item.title}</Button>
              <Stack direction="row" gap={1} sx={{mt:1}}><Chip size="small" label={labels[item.kind] || item.kind}/>
                {item.protected && <Chip size="small" label="人工修改"/>}{!item.current && <Chip size="small" label="待更新" color="warning"/>}</Stack>
            </Paper>)}
            <Stack direction="row" justifyContent="space-between"><Button disabled={!offset || busy} onClick={()=>setOffset(Math.max(0,offset-30))}>上一页</Button>
              <Typography variant="caption" sx={{alignSelf:"center"}}>共 {data.total} 项</Typography><Button disabled={!data.has_more || busy} onClick={()=>setOffset(offset+30)}>下一页</Button></Stack>
          </Stack>
          <Paper variant="outlined" sx={{p:3, flex:1, minWidth:0}}>
            {!selected ? <Typography color="text.secondary">选择左侧条目查看内容</Typography> : <>
              {selected.warning && <Alert severity="warning">{selected.warning}</Alert>}
              {editing ? <Stack spacing={2}><TextField label="标题" value={title} onChange={e=>setTitle(e.target.value)} inputProps={{maxLength:200}}/>
                <TextField label="内容（Markdown）" multiline minRows={12} value={draft} onChange={e=>setDraft(e.target.value)} inputProps={{maxLength:16000}}/>
                <Stack direction="row" spacing={1}><Button variant="contained" disabled={busy || !title.trim() || !draft.trim()} onClick={save}>保存修改</Button><Button disabled={busy} onClick={()=>setEditing(false)}>取消</Button></Stack></Stack>
                : <><Typography variant="h6" sx={{mb:2}}>{selected.title}</Typography>
                  <Box component="pre" sx={{fontFamily:"inherit", whiteSpace:"pre-wrap", overflowWrap:"anywhere", m:0, lineHeight:1.8}}>{selected.content || selected.historical_content || "内容已失效，请让助手核对原资料后更新。"}</Box>
                  <Stack direction="row" spacing={1} sx={{mt:3}}><Button disabled={busy || !selected.current} onClick={()=>{setTitle(selected.title);setDraft(selected.content || "");setEditing(true);}}>修改</Button>
                    <Button color="error" disabled={busy} onClick={()=>setDeleting(true)}>遗忘此条目</Button></Stack></>}
              {(selected.citations || []).length > 0 && <><Divider sx={{my:2}}/><Typography variant="subtitle2">事实出处</Typography>
                <Typography variant="caption" color="text.secondary">原文位置已核对；摘要中的解释仍需结合上下文判断。</Typography>
                {selected.citations?.map((c,i)=><Box component="details" key={`${c.evidence_id}-${i}`} sx={{my:1,p:1,border:1,borderColor:"divider",borderRadius:1}}>
                  <Box component="summary" sx={{cursor:"pointer"}}>{c.statement}</Box>
                  <Typography variant="body2" sx={{mt:1}}>{c.source_title || "来源已变化"} · {locationLabel(c.locator)}</Typography>
                  <Typography component="blockquote" sx={{mx:1,whiteSpace:"pre-wrap",overflowWrap:"anywhere"}}>{c.current ? c.quote : "原文已变化，等待重新核对"}</Typography>
                </Box>)}</>}
              {selected.kind === "page" && !(selected.citations || []).length && <Typography variant="caption" color="text.secondary" sx={{display:"block",mt:2}}>此页尚未标注逐条原文出处，请让助手补充后再用于正式填写。</Typography>}
              {Object.keys(selected.source_versions).length > 0 && <><Divider sx={{my:2}}/><Typography variant="subtitle2">依据的原始资料</Typography>
                {Object.keys(selected.source_versions).map(id=><Typography key={id} variant="body2" color="text.secondary">{data.sources.find(s=>s.id===id)?.title || "来源已变化"}</Typography>)}</>}
            </>}
          </Paper>
        </Stack>
        <Divider sx={{my:3}}/><Typography variant="h6" sx={{mb:1}}>原始资料</Typography>
        {data.sources.map(s=><Typography key={s.id} sx={{my:1}}>{s.title} <Typography component="span" variant="caption" color="text.secondary">{s.semantic_status === "organized" ? "已有知识页" : "待整理"}{s.coverage !== "complete" ? " · 解析不完整" : ""}</Typography></Typography>)}
        <LocalCatalogSection key={`${account}-${(data.local_catalog?.devices || []).map(d=>`${d.id}:${d.updated_at}`).join(",")}`} account={account}/>
      </>}
    </Box>
    <Dialog open={deleting} onClose={()=>!busy && setDeleting(false)}><DialogTitle>遗忘“{selected?.title}”？</DialogTitle><DialogContent>此条目将不再参与检索。原文件仍保留。</DialogContent>
      <DialogActions><Button disabled={busy} onClick={()=>setDeleting(false)}>取消</Button><Button color="error" disabled={busy} onClick={forget}>遗忘</Button></DialogActions></Dialog>
  </Box>;
}
