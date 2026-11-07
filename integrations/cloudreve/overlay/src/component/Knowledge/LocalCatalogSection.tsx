import {useEffect, useRef, useState} from "react";
import {Alert, Box, Button, Chip, Stack, Typography} from "@mui/material";
import {knowledgeRequest} from "./bridge";

type Entry = {id: string; device_id: string; root_label: string; root_id: string; path: string;
  status: string; coverage: string; imported_current: boolean; observed_at: string};
type Catalog = {items: Entry[]; total: number; has_more: boolean; availability: string};
const labels: Record<string,string> = {indexed:"本机已索引", deferred:"待单独处理", failed:"解析未完成",
  missing:"本机文件缺失", offline:"暂不可读取", changed:"原件已变化"};

export default function LocalCatalogSection({account}: {account?: string}) {
  const [data,setData] = useState<Catalog | null>(null), [offset,setOffset] = useState(0), [error,setError] = useState("");
  const epoch = useRef(0);
  useEffect(()=>{setOffset(0);setData(null);setError("");},[account]);
  useEffect(()=>{
    const stamp = ++epoch.current;
    knowledgeRequest(`local-catalog?offset=${offset}&limit=50`).then(next=>{
      if (epoch.current===stamp) {setData(next);setError("");}
    }).catch(e=>{if(epoch.current===stamp) setError(e instanceof Error ? e.message : "本机清单未能加载，请刷新重试");});
    return ()=>{epoch.current++;};
  },[account,offset]);
  if(error) return <Alert severity="warning">{error}</Alert>;
  if(!data?.total) return null;
  const groups = new Map<string,Entry[]>();
  data.items.forEach(item=>{const key=`${item.device_id}/${item.root_id}`;groups.set(key,[...(groups.get(key)||[]),item]);});
  return <Box sx={{mt:3}}>
    <Typography variant="h6">本机资料</Typography>
    <Typography variant="body2" color="text.secondary" sx={{my:1}}>经你授权同步的文件清单。正文阅读和检索由对应电脑上的 WorkBuddy 完成；清单不代表原件已上传或当前在线。</Typography>
    {[...groups.entries()].map(([key,items])=><Box component="details" key={key} open sx={{my:1,p:2,border:1,borderColor:"divider",borderRadius:1}}>
      <Typography component="summary" sx={{cursor:"pointer"}}>{items[0].root_label}</Typography>
      {items.map(item=><Stack key={item.id} direction={{xs:"column",sm:"row"}} spacing={1} sx={{my:1,pl:2}} alignItems={{sm:"center"}}>
        <Typography variant="body2" sx={{overflowWrap:"anywhere",flex:1}}>{item.path}</Typography>
        <Chip size="small" label={labels[item.status]||"待检查"}/>
        {item.imported_current && <Chip size="small" label="已收录此版本" color="success"/>}
        <Typography variant="caption" color="text.secondary">最近同步 {new Date(item.observed_at).toLocaleString("zh-CN")}</Typography>
      </Stack>)}
    </Box>)}
    <Stack direction="row" justifyContent="space-between" alignItems="center">
      <Button disabled={!offset} onClick={()=>setOffset(Math.max(0,offset-50))}>上一页</Button>
      <Typography variant="caption">共 {data.total} 份本机资料</Typography>
      <Button disabled={!data.has_more} onClick={()=>setOffset(offset+50)}>下一页</Button>
    </Stack>
  </Box>;
}
