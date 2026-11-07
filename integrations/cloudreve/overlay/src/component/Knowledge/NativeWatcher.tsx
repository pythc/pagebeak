import { useEffect } from "react";
import SessionManager from "../../session";
import { knowledgeRequest } from "./bridge.ts";
import { pendingNotices, removeNotice } from "./outbox.ts";
export default function NativeWatcher() {
  useEffect(() => {
    let timer: ReturnType<typeof setTimeout>;
    let running = false;
    const flush = async () => {
      if (running) return;
      const userId = SessionManager.currentLoginOrNull()?.user.id;
      if (!userId) return;
      running = true;
      try {
        for (const notice of pendingNotices(userId).slice(0, 20)) {
          await knowledgeRequest("notice", notice);
          if (SessionManager.currentLoginOrNull()?.user.id !== userId) break;
          removeNotice(userId, notice.request_id);
        }
      } catch { /* Retain unacknowledged notices across reloads; the periodic scan checks final state. */ }
      finally { running = false; }
    };
    const wake = () => {
      clearTimeout(timer);
      timer = setTimeout(() => { void flush(); knowledgeRequest("wake", {}).catch(() => {}); }, 500);
    };
    window.addEventListener("kb-file-changed", wake);
    const retry = setInterval(() => { void flush(); }, 5000);
    void flush();
    return () => { clearTimeout(timer); clearInterval(retry); window.removeEventListener("kb-file-changed", wake); };
  }, []);
  return null;
}
