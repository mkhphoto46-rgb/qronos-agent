import { useEffect, useRef, useState } from "react";

import { invoke } from "@tauri-apps/api/core";
import { listen } from "@tauri-apps/api/event";

import {
  approvalOf, createMediaClient, isAssetId, refusalText,
} from "../mediaStore";
import type { MediaApproval, MediaClient, MediaOp, MediaResult, MediaTransport, RuntimeEvent } from "../mediaStore";

import "./MediaPanel.css";

/** The production transport: the registered send_runtime_action command and the runtime event stream. */
export function createTauriMediaTransport(): MediaTransport {
  return {
    send: async (actionId, data) => {
      await invoke("send_runtime_action", { actionId, data });
    },
    listen: (handler) => listen<RuntimeEvent>("qronos://runtime-event", (event) => handler(event.payload)),
    setTimer: (callback, ms) => window.setTimeout(callback, ms),
    clearTimer: (handle) => window.clearTimeout(handle as number),
  };
}

export function createDesktopMediaClient(): MediaClient {
  return createMediaClient(createTauriMediaTransport());
}

type MediaPanelProps = {
  client: MediaClient;
  visible: boolean;
};

type Hit = {
  assetId: string;
  title?: string | null;
  startMs: number;
  endMs?: number;
  dialogue?: string;
  match?: string;
  similarity?: number;
  available?: boolean;
  timing?: string;
  track?: { language?: string | null; source?: string };
};

type Capability = { capability: string; status: string; reason?: string };

const STATUS_FA: Record<string, string> = {
  implemented_unverified: "پیاده‌سازی‌شده، تأییدنشده",
  pending: "هنوز ساخته نشده",
  deferred: "به تعویق افتاده",
  decision_pending: "منتظر تصمیم",
  not_supported: "پشتیبانی نمی‌شود",
};

const CAPABILITY_FA: Record<string, string> = {
  asr: "تبدیل گفتار به متن (Whisper)",
  subtitle_asr_fusion: "تطبیق زیرنویس و گفتار",
  semantic_dialogue: "جست‌وجوی معنایی دیالوگ (bge-m3)",
  visual_embeddings: "جست‌وجوی تصویری",
  vision_captions: "توصیف تصویر",
  ocr: "خواندن متن روی تصویر",
  topics: "موضوع‌ها",
  speaker_turns: "تفکیک گوینده‌ها",
  actions_motion: "حرکت و کنش",
};

const MODE_FA: Record<string, string> = { exact: "دقیق", fuzzy: "تقریبی", semantic: "معنایی" };

export function clock(ms: unknown): string {
  const total = typeof ms === "number" && Number.isFinite(ms) && ms >= 0 ? Math.floor(ms / 1000) : 0;
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const s = total % 60;
  return `${h}:${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}`;
}

/** Search hits from either answer shape (lexical, or the semantic block after the lexical one), validated. */
export function hitsOf(result: MediaResult | null): { exact: Hit[]; semantic: Hit[]; semanticNote: string | null } {
  const pick = (value: unknown): Hit[] => (Array.isArray(value) ? value : [])
    .filter((item): item is Hit => typeof item === "object" && item !== null && isAssetId((item as Hit).assetId)
      && typeof (item as Hit).startMs === "number")
    .slice(0, 50);
  if (!result || !result.ok) return { exact: [], semantic: [], semanticNote: null };
  const lexical = result.results as { results?: unknown } | undefined;
  const exact = result.exact as { results?: unknown } | undefined;
  const semantic = result.semantic as { results?: unknown; status?: string; reason?: string } | undefined;
  const note = semantic && semantic.status === "unavailable"
    ? `جست‌وجوی معنایی در دسترس نیست: ${refusalText(semantic.reason ?? "model_unavailable")}`
    : null;
  return { exact: pick(lexical?.results ?? exact?.results), semantic: pick(semantic?.results), semanticNote: note };
}

// The media library: chosen folders, background indexing, search with evidence cards, open at a time, reports.
// Every action is a typed request to the runtime's one media host; permission and approvals are decided there.
// The approval secret lives only in this component's transient state: cleared on submit, cancel and hide.
function MediaPanel(props: MediaPanelProps) {
  const { client, visible } = props;
  const [status, setStatus] = useState<Record<string, unknown> | null>(null);
  const [roots, setRoots] = useState<Array<{ rootId: number; path: string; kind: string }>>([]);
  const [capabilities, setCapabilities] = useState<Capability[]>([]);
  const [folder, setFolder] = useState("");
  const [query, setQuery] = useState("");
  const [mode, setMode] = useState<"exact" | "fuzzy" | "semantic">("exact");
  const [result, setResult] = useState<MediaResult | null>(null);
  const [approval, setApproval] = useState<MediaApproval | null>(null);
  const [secret, setSecret] = useState("");
  const [destination, setDestination] = useState("");
  const [notice, setNotice] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  // Bumped when the panel hides: an answer for an older generation is ignored.
  const generation = useRef(0);

  const current = () => generation.current;

  async function run(op: MediaOp, fields: Record<string, unknown> = {}): Promise<MediaResult | null> {
    const asked = current();
    setBusy(true);
    try {
      const answer = await client.request(op, fields);
      if (asked !== current()) return null;
      const wanted = approvalOf(answer);
      if (wanted) {
        setApproval(wanted);
        setNotice(null);
      } else if (!answer.ok) {
        setNotice(refusalText(answer.code));
      }
      return answer;
    } finally {
      if (asked === current()) setBusy(false);
    }
  }

  async function refresh() {
    const asked = current();
    const [state, list, caps] = await Promise.all([
      client.request("status"), client.request("roots.list"), client.request("capabilities"),
    ]);
    if (asked !== current()) return;
    if (state.ok) setStatus(state.status as Record<string, unknown>);
    if (list.ok && Array.isArray(list.roots)) setRoots(list.roots as typeof roots);
    if (caps.ok && Array.isArray(caps.capabilities)) setCapabilities(caps.capabilities as Capability[]);
  }

  useEffect(() => {
    if (!visible) {
      generation.current += 1;
      client.reset();
      setSecret("");
      setApproval(null);
      return;
    }
    void refresh();
    const stop = client.onProgress((payload) => {
      if (payload.status && typeof payload.status === "object") setStatus(payload.status as Record<string, unknown>);
    });
    return stop;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [visible]);

  async function addFolder() {
    const path = folder.trim();
    if (!path) return;
    const answer = await run("roots.add", { path, kind: "folder", maxDepth: 8 });
    if (answer?.ok && answer.status === "added") {
      setFolder("");
      await run("scan", { rootId: answer.rootId });
      await refresh();
    }
  }

  async function search() {
    const text = query.trim();
    if (!text) return;
    const answer = await run("search", { query: text, mode, limit: 20 });
    if (answer) setResult(answer);
  }

  async function decide(approved: boolean) {
    const held = approval;
    if (!held) return;
    const typed = secret;
    setSecret(""); // cleared before anything is sent
    setApproval(null);
    const asked = current();
    const answer = approved ? await client.decide(held, typed) : await client.decline(held);
    if (asked !== current()) return;
    if (!answer.ok) setNotice(refusalText(answer.code));
    else if (answer.status === "launched") setNotice(`در ${String(answer.player)} از ${clock((answer.startSeconds as number) * 1000)} باز شد.`);
    else if (answer.status === "written") setNotice(`گزارش ذخیره شد: ${String(answer.path)}`);
    else if (answer.status === "not_verified") setNotice("نسخهٔ فایل دیگر با نسخهٔ فهرست‌شده یکی نیست یا دسترسی آن لغو شده؛ هیچ کاری انجام نشد.");
    else if (answer.status === "added") {
      await run("scan", { rootId: answer.rootId });
    }
    await refresh();
  }

  const { exact, semantic, semanticNote } = hitsOf(result);
  const store = (status?.store ?? null) as Record<string, unknown> | null;
  const running = Boolean(status?.running);

  function card(hit: Hit, key: string) {
    return (
      <li key={key} className="media-card">
        <div className="media-card-head">
          <strong>{hit.title || "ویدیو"}</strong>
          <span className="ltr">{clock(hit.startMs)}</span>
          {typeof hit.similarity === "number" && <span title="شباهت کسینوسی؛ احتمال یا درصد نیست">≈ {hit.similarity.toFixed(2)}</span>}
        </div>
        <p dir="auto">{hit.dialogue}</p>
        <small>{hit.track?.source} {hit.track?.language ?? ""} — {hit.timing}</small>
        <div className="media-card-actions">
          <button type="button" disabled={busy || hit.available === false}
            onClick={() => void run("open.prepare", { assetId: hit.assetId, startMs: Math.max(0, Math.floor(hit.startMs)) })}>
            باز کردن در همین لحظه
          </button>
          {(["json", "html", "csv"] as const).map((format) => (
            <button key={format} type="button" disabled={busy}
              onClick={() => void run("report.generate", { assetId: hit.assetId, format }).then((a) => {
                if (a?.ok) setNotice(`گزارش ${format} ساخته شد.`);
              })}>
              گزارش {format}
            </button>
          ))}
          <button type="button" disabled={busy || !destination.trim()}
            onClick={() => void run("report.export", { assetId: hit.assetId, destination: destination.trim(),
              format: destination.trim().toLowerCase().endsWith(".csv") ? "csv" : destination.trim().toLowerCase().endsWith(".html") ? "html" : "json" })}>
            خروجی گرفتن
          </button>
        </div>
      </li>
    );
  }

  if (!visible) return null;
  return (
    <section className="media-panel" dir="rtl" lang="fa">
      <h2>کتابخانهٔ ویدیو</h2>
      {notice && <p className="media-notice" role="status">{notice}</p>}

      <h3>پوشه‌ها</h3>
      <div className="media-row">
        <input value={folder} onChange={(e) => setFolder(e.target.value)} placeholder="مسیر پوشهٔ ویدیوها" dir="ltr" maxLength={4096} />
        <button type="button" disabled={busy || !folder.trim()} onClick={() => void addFolder()}>افزودن و فهرست‌سازی</button>
      </div>
      <ul>
        {roots.map((root) => (
          <li key={root.rootId}>
            <span dir="ltr">{root.path}</span>
            <button type="button" disabled={busy} onClick={() => void run("scan", { rootId: root.rootId })}>بررسی دوباره</button>
            <button type="button" disabled={busy} onClick={() => void run("roots.remove", { rootId: root.rootId }).then(() => refresh())}>حذف از فهرست</button>
          </li>
        ))}
      </ul>

      <h3>فهرست‌سازی</h3>
      <p>
        {store ? `${String(store.assets ?? 0)} ویدیو، ${String(store.dialogueCues ?? 0)} خط دیالوگ` : "هنوز چیزی فهرست نشده"}
        {" — "}{running ? "در حال کار" : store?.paused ? "مکث" : "آماده"}
      </p>
      <div className="media-row">
        <button type="button" disabled={busy} onClick={() => void run("index.start")}>شروع</button>
        <button type="button" disabled={busy} onClick={() => void run("index.pause")}>مکث</button>
        <button type="button" disabled={busy} onClick={() => void run("index.resume")}>ادامه</button>
        <button type="button" onClick={() => void run("index.stop")}>توقف</button>
      </div>

      <h3>جست‌وجو</h3>
      <div className="media-row">
        <input value={query} onChange={(e) => setQuery(e.target.value)} placeholder="چه چیزی گفته شد؟" dir="auto" maxLength={1200} />
        <select value={mode} onChange={(e) => setMode(e.target.value as typeof mode)}>
          {(["exact", "fuzzy", "semantic"] as const).map((m) => <option key={m} value={m}>{MODE_FA[m]}</option>)}
        </select>
        <button type="button" disabled={busy || !query.trim()} onClick={() => void search()}>جست‌وجو</button>
      </div>
      <div className="media-row">
        <input value={destination} onChange={(e) => setDestination(e.target.value)} placeholder="مسیر فایل خروجی گزارش (.json/.html/.csv)" dir="ltr" maxLength={4096} />
      </div>
      {semanticNote && <p className="media-notice">{semanticNote}</p>}
      <ul className="media-results">{exact.map((hit, i) => card(hit, `e${i}`))}</ul>
      {semantic.length > 0 && <>
        <h4>نتیجه‌های معنایی (شباهت تأییدنشده)</h4>
        <ul className="media-results">{semantic.map((hit, i) => card(hit, `s${i}`))}</ul>
      </>}

      <h3>توانایی‌ها</h3>
      <ul className="media-capabilities">
        {capabilities.map((item) => (
          <li key={item.capability}>{CAPABILITY_FA[item.capability] ?? item.capability}: {STATUS_FA[item.status] ?? item.status}</li>
        ))}
      </ul>

      {approval && (
        <div className="media-approval" role="dialog" aria-modal="true">
          <p>تأیید لازم است: {approval.op === "roots.add" ? "خواندن این پوشه" : approval.op === "open.prepare" ? "باز کردن ویدیو در پخش‌کننده" : "نوشتن گزارش"}</p>
          <pre dir="ltr">{JSON.stringify(approval.preview, null, 1).slice(0, 1500)}</pre>
          {approval.needsSecret && (
            <input type="password" value={secret} onChange={(e) => setSecret(e.target.value)} placeholder="رمز تأیید" maxLength={256} autoComplete="off" />
          )}
          <button type="button" disabled={approval.needsSecret && !secret} onClick={() => void decide(true)}>تأیید</button>
          <button type="button" onClick={() => void decide(false)}>نه</button>
        </div>
      )}
    </section>
  );
}

export default MediaPanel;
