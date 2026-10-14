import { useEffect, useRef, useState } from "react";

import { invoke } from "@tauri-apps/api/core";
import { listen } from "@tauri-apps/api/event";

import {
  approvalOf, createMediaClient, exportDestinationProblem, interpretationOf, isAssetId, multimodalBlockOf,
  multimodalFields, multimodalRequestProblem, MULTIMODAL_MAX_CONSTRAINT_CHARS, MULTIMODAL_WINDOWS_MS, refusalText,
  REPORT_FORMATS, REPORT_KIND_TEXT, REPORT_KINDS, searchReportPreviewOf, topicBlockOf, visualBlockOf,
} from "../mediaStore";
import type {
  MediaApproval, MediaClient, MediaOp, MediaResult, MediaTransport, MultimodalFrame, MultimodalHit, MultimodalInterpretation,
  ImageHit, ReportFormat, ReportKind, RuntimeEvent, SearchReportPreview, TopicHit, VisualHit, VisualMatch,
} from "../mediaStore";

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

type ReportPreview = {
  kind: ReportKind;
  schema: string;
  generatedAt: string;
  asset: { assetId: string; sha256: string; title?: string | null; durationMs?: number | null; container?: string | null };
  stages: Record<string, { state: string; version: number; finishedAt?: string | null }>;
  summary: { dialogueLines: number; dialogueLinesBySource: Record<string, number>; scenes: number; keyframes: number };
  subtitleTracks: Array<{ source: string; language?: string | null; status: string; cueCount: number; detail?: string | null }>;
  notAvailable: Array<{ capability: string; status: string; stageState?: string | null }>;
  limitations: Array<{ code: string; text: string }>;
  dialogue?: Array<{ startMs: number; endMs: number; source: string; text: string }>;
};

const SOURCE_FA: Record<string, string> = {
  embedded: "زیرنویس درون فایل", sidecar: "زیرنویس کنار فایل", asr: "رونویسی ماشینی (تأییدنشده)",
};

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
  topic_retrieval: "جست‌وجوی موضوعی (بخش‌های بدون برچسب، با جمله‌های نمایه‌شده)",
  visual_embeddings: "جست‌وجوی تصویری",
  multimodal_search: "جست‌وجوی ترکیبی گفتگو + تصویر (هم‌زمانی در یک ویدیو)",
  vision_captions: "توصیف فریم‌ها با مدل بینایی (تولیدشده، تأییدنشده)",
  ocr: "خواندن متن روی تصویر",
  topics: "موضوع‌ها",
  speaker_turns: "تفکیک گوینده‌ها",
  actions_motion: "حرکت و کنش",
};

const MODE_FA: Record<string, string> = { exact: "دقیق", fuzzy: "تقریبی", semantic: "معنایی", topic: "موضوعی",
  visual: "تصویری (متن فریم‌ها + شباهت تصویری)", multimodal: "ترکیبی (گفتگو + تصویر)" };

const DIALOGUE_CHANNEL_FA: Record<string, string> = { exact: "دقیق", fuzzy: "تقریبی", semantic: "هم‌معنا (bge-m3)" };
const VISUAL_CHANNEL_FA: Record<string, string> = { lexical: "هم‌واژه", semantic: "هم‌معنا از روی متن تولیدشده",
  image: "شباهت تصویری (SigLIP 2)" };

/** Where a frame lies relative to the matching line, in words. */
export function relationText(frame: Pick<MultimodalFrame, "relation" | "gapMs">): string {
  if (frame.relation === "during") return "فریم هم‌زمان با همین جمله است";
  const seconds = Math.round(frame.gapMs / 100) / 10;
  return `فریم ${seconds} ثانیه ${frame.relation === "before" ? "پیش از" : "پس از"} این جمله است`;
}

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
  const [mode, setMode] = useState<"exact" | "fuzzy" | "semantic" | "topic" | "visual" | "multimodal">("exact");
  // Multimodal: two independent constraints (typed or confirmed from a proposed split), a window and semantic opt-ins.
  const [saidText, setSaidText] = useState("");
  const [shownText, setShownText] = useState("");
  const [windowMs, setWindowMs] = useState<number>(15_000);
  const [semanticSaid, setSemanticSaid] = useState(false);
  const [shownMatch, setShownMatch] = useState<VisualMatch>("lexical");
  const [interpretation, setInterpretation] = useState<MultimodalInterpretation | null>(null);
  const [searchPreview, setSearchPreview] = useState<SearchReportPreview | null>(null);
  const [brainHints, setBrainHints] = useState(false);
  const [result, setResult] = useState<MediaResult | null>(null);
  const [approval, setApproval] = useState<MediaApproval | null>(null);
  const [secret, setSecret] = useState("");
  const [destination, setDestination] = useState("");
  const [reportFormat, setReportFormat] = useState<ReportFormat>("pdf");
  const [reportKind, setReportKind] = useState<ReportKind>("full");
  const [preview, setPreview] = useState<ReportPreview | null>(null);
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
      setPreview(null);
      setSearchPreview(null);
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

  async function searchMultimodal() {
    const problem = multimodalRequestProblem(saidText, shownText);
    if (problem) {
      setNotice(problem);
      return;
    }
    const answer = await run("search", multimodalFields(saidText, shownText, windowMs, semanticSaid, shownMatch));
    if (answer) setResult(answer);
  }

  // A natural combined request is only SPLIT here: the proposal fills the two fields for the person to check or edit;
  // nothing is searched until they press search.
  async function splitRequest() {
    const text = query.trim();
    if (!text) return;
    const answer = await run("search", { mode: "multimodal", query: text });
    const proposal = interpretationOf(answer);
    setInterpretation(proposal);
    setResult(null);
    if (proposal?.status === "proposed") {
      setSaidText(proposal.dialogue);
      setShownText(proposal.visual);
    }
  }

  async function search() {
    if (mode === "multimodal") {
      await searchMultimodal();
      return;
    }
    const text = query.trim();
    if (!text) return;
    const answer = await run("search", mode === "topic" && brainHints
      ? { query: text, mode, limit: 20, brainHints: true }
      : { query: text, mode, limit: 20 });
    if (answer) setResult(answer);
  }

  // The report of the CURRENT multimodal answer: only its server-held resultId is sent, never the hits themselves.
  async function previewSearchReport(resultId: string) {
    const answer = await run("search.report.preview", { resultId });
    setSearchPreview(searchReportPreviewOf(answer));
  }

  async function generateSearchReport(resultId: string) {
    const answer = await run("search.report.generate", { resultId, format: reportFormat });
    const report = answer?.ok ? (answer.report as Record<string, unknown> | undefined) : undefined;
    if (report) setNotice(`گزارش نتیجه‌های جست‌وجو (${reportFormat}) ساخته شد: ${String(report.path)}`);
  }

  async function exportSearchReport(resultId: string) {
    const problem = exportDestinationProblem(destination, reportFormat);
    if (problem) {
      setNotice(problem);
      return;
    }
    await run("search.report.export", { resultId, destination: destination.trim(), format: reportFormat });
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

  async function generateReport(assetId: string) {
    const answer = await run("report.generate", { assetId, format: reportFormat, kind: reportKind });
    const report = answer?.ok ? (answer.report as Record<string, unknown> | undefined) : undefined;
    if (report) setNotice(`${REPORT_KIND_TEXT[reportKind]} (${reportFormat}) ساخته شد: ${String(report.path)}`);
  }

  async function previewReport(assetId: string) {
    const answer = await run("report.preview", { assetId, kind: reportKind });
    if (answer?.ok && answer.preview && typeof answer.preview === "object") setPreview(answer.preview as ReportPreview);
  }

  async function exportReport(assetId: string) {
    const problem = exportDestinationProblem(destination, reportFormat);
    if (problem) {
      setNotice(problem);
      return;
    }
    await run("report.export", { assetId, destination: destination.trim(), format: reportFormat, kind: reportKind });
  }

  const { exact, semantic, semanticNote } = hitsOf(result);
  const topic = topicBlockOf(result);
  const visual = visualBlockOf(result);
  const multimodal = multimodalBlockOf(result);

  function multimodalCard(hit: MultimodalHit, key: string) {
    return (
      <li key={key} className="media-card media-multimodal-card">
        <div className="media-card-head">
          <strong>{hit.title || "ویدیو"}</strong>
          <span className="ltr">{clock(hit.startMs)} – {clock(hit.endMs)}</span>
        </div>
        <p className="media-multimodal-why">
          هر دو شرط در همین ویدیو و در فاصلهٔ {Math.round(hit.windowMs / 1000)} ثانیه از هم پیدا شدند:
          گفتگو ({hit.dialogue.channels.map((name) => DIALOGUE_CHANNEL_FA[name] ?? name).join(" + ")})
          {" و "}تصویر ({[...new Set(hit.frames.flatMap((frame) => frame.channels))]
            .map((name) => VISUAL_CHANNEL_FA[name] ?? name).join(" + ")}).
        </p>
        <p>
          <small>گفتگو</small> <span className="ltr">{clock(hit.dialogue.startMs)}</span>{" "}
          <span dir="auto">{hit.dialogue.text}</span>{" "}
          <small>— {SOURCE_FA[hit.dialogue.source] ?? hit.dialogue.source} {hit.dialogue.language ?? ""}
            {hit.dialogue.similarity !== null ? ` (شباهت ≈ ${hit.dialogue.similarity.toFixed(2)}، کالیبره‌نشده)` : ""}</small>
        </p>
        <ul className="media-topic-lines">
          {hit.frames.map((frame, i) => (
            <li key={i}>
              <span className="ltr">{clock(frame.timeMs)}</span>{" "}
              {frame.generated && <><span className="media-topic-generated" dir="auto">{frame.caption}</span>{" "}</>}
              <small>({relationText(frame)}
                {frame.generated ? "؛ توصیف تولیدشده با مدل بینایی از همین فریم، تأییدنشده" : ""}
                {frame.similarity !== null ? `؛ شباهت متن ≈ ${frame.similarity.toFixed(2)}، کالیبره‌نشده` : ""}
                {frame.imageSimilarity !== null
                  ? `؛ شباهت تصویری ≈ ${frame.imageSimilarity.toFixed(2)} (SigLIP 2، کالیبره‌نشده، نه شناسایی)` : ""})</small>{" "}
              <small className="ltr" title="SHA-256 همان فریم ذخیره‌شده">{frame.frameSha256.slice(0, 12)}</small>
            </li>
          ))}
        </ul>
        {hit.framesInWindow > hit.frames.length && <small>{hit.framesInWindow - hit.frames.length} فریم هم‌خوان دیگر هم در این پنجره بود.</small>}
        <div className="media-card-actions">
          <button type="button" disabled={busy}
            onClick={() => void run("open.prepare", { assetId: hit.assetId, startMs: Math.max(0, Math.floor(hit.startMs)) })}>
            باز کردن در همین لحظه
          </button>
        </div>
      </li>
    );
  }

  function visualCard(hit: VisualHit, key: string) {
    return (
      <li key={key} className="media-card">
        <div className="media-card-head">
          <strong>{hit.title || "ویدیو"}</strong>
          <span className="ltr">{clock(hit.timeMs)}</span>
          <small className="ltr" title="SHA-256 همان فریم ذخیره‌شده">{hit.frameSha256.slice(0, 12)}</small>
        </div>
        <small>
          {hit.channels.includes("lexical") ? "هم‌واژه" : ""}
          {hit.channels.length === 2 ? " + " : ""}
          {hit.channels.includes("semantic")
            ? `هم‌معنا از روی متن تولیدشده، نه شباهت تصویر (≈ ${(hit.similarity ?? 0).toFixed(2)}، کالیبره‌نشده)` : ""}
        </small>
        <p className="media-topic-generated">
          <span dir="auto">{hit.caption}</span>{" "}
          <small>(توصیف تولیدشده با مدل بینایی از همین فریم؛ تأییدنشده، نه واقعیت و نه شناسایی)</small>
        </p>
        {hit.visibleText.length > 0 && (
          <p><small>متنی که مدل در فریم خواند (OCR نیست):</small> <span dir="auto">{hit.visibleText.join(" / ")}</span></p>
        )}
        {hit.observations.length > 0 && <p><small>مشاهده‌ها:</small> <span dir="auto">{hit.observations.join("، ")}</span></p>}
        {hit.dialogue.length > 0 && (
          <ul className="media-topic-lines">
            {hit.dialogue.map((line, i) => (
              <li key={i}><span className="ltr">{clock(line.startMs)}</span> <span dir="auto">{line.text}</span></li>
            ))}
          </ul>
        )}
        <small>دیالوگ بالا فقط به خاطر هم‌زمانی کنار فریم آمده است، نه به خاطر معنا.</small>
        <div className="media-card-actions">
          <button type="button" disabled={busy}
            onClick={() => void run("open.prepare", { assetId: hit.assetId, startMs: Math.max(0, Math.floor(hit.timeMs)) })}>
            باز کردن در همین لحظه
          </button>
        </div>
      </li>
    );
  }

  function imageCard(hit: ImageHit, key: string) {
    return (
      <li key={key} className="media-card">
        <div className="media-card-head">
          <strong>{hit.title || "ویدیو"}</strong>
          <span className="ltr">{clock(hit.timeMs)}</span>
          <span title="شباهت کسینوسی کالیبره‌نشده؛ احتمال، درصد یا شناسایی نیست">≈ {hit.imageSimilarity.toFixed(2)}</span>
          <small className="ltr" title="SHA-256 همان فریم ذخیره‌شده">{hit.frameSha256.slice(0, 12)}</small>
        </div>
        <small>پیداشده با شباهت تصویری خود این فریم ذخیره‌شده (نه از روی توصیف آن).</small>
        {hit.caption !== null && (
          <p className="media-topic-generated">
            <span dir="auto">{hit.caption}</span>{" "}
            <small>(توصیف تولیدشده با مدل بینایی از همین فریم؛ تأییدنشده)</small>
          </p>
        )}
        {hit.dialogue.length > 0 && (
          <ul className="media-topic-lines">
            {hit.dialogue.map((line, i) => (
              <li key={i}><span className="ltr">{clock(line.startMs)}</span> <span dir="auto">{line.text}</span></li>
            ))}
          </ul>
        )}
        {hit.dialogue.length > 0 && <small>دیالوگ بالا فقط به خاطر هم‌زمانی کنار فریم آمده است، نه به خاطر معنا.</small>}
        <div className="media-card-actions">
          <button type="button" disabled={busy}
            onClick={() => void run("open.prepare", { assetId: hit.assetId, startMs: Math.max(0, Math.floor(hit.timeMs)) })}>
            باز کردن در همین لحظه
          </button>
        </div>
      </li>
    );
  }

  function topicCard(hit: TopicHit, key: string) {
    const parts = hit.components;
    return (
      <li key={key} className="media-card">
        <div className="media-card-head">
          <strong>{hit.title || "ویدیو"}</strong>
          <span className="ltr">{clock(hit.startMs)} – {clock(hit.endMs)}</span>
          <span title="امتیاز رتبه با وزن‌های ثابت؛ احتمال یا درصد نیست">≈ {hit.score.toFixed(2)}</span>
        </div>
        {hit.generated && (
          <p className="media-topic-generated">
            <strong dir="auto">{hit.generated.label}</strong>{" "}
            <small>(برچسب تولیدشده با مغز سریع از جمله‌های زیر؛ تأییدنشده، نقل‌قول نیست)</small>
            <br /><span dir="auto">{hit.generated.summary}</span>
          </p>
        )}
        <ul className="media-topic-lines">
          {hit.evidence.map((line, i) => (
            <li key={i}><span className="ltr">{clock(line.startMs)}</span> <span dir="auto">{line.text}</span></li>
          ))}
        </ul>
        <small>
          چرا: هم‌معنایی بخش {parts.segment.toFixed(2)}، نزدیک‌ترین جمله {parts.evidence.toFixed(2)}، واژه‌های مشترک{" "}
          {parts.terms.toFixed(2)}{parts.expansion > 0 ? `، جست‌وجوی گسترش‌یافته ${parts.expansion.toFixed(2)}` : ""}
          {" — "}{SOURCE_FA[hit.source] ?? hit.source}
        </small>
        <div className="media-card-actions">
          <button type="button" disabled={busy}
            onClick={() => void run("open.prepare", { assetId: hit.assetId, startMs: Math.max(0, Math.floor(hit.startMs)) })}>
            باز کردن در همین لحظه
          </button>
        </div>
      </li>
    );
  }
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
          <button type="button" disabled={busy} onClick={() => void previewReport(hit.assetId)}>
            پیش‌نمایش {REPORT_KIND_TEXT[reportKind]}
          </button>
          <button type="button" disabled={busy} onClick={() => void generateReport(hit.assetId)}>
            ساخت {REPORT_KIND_TEXT[reportKind]} ({reportFormat})
          </button>
          <button type="button" disabled={busy || !destination.trim()} onClick={() => void exportReport(hit.assetId)}>
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
        <input value={query} onChange={(e) => setQuery(e.target.value)}
          placeholder={mode === "multimodal" ? "درخواست ترکیبی (اختیاری)، مثلاً صحنه‌ای در آشپزخانه که دربارهٔ مرگ پدرش حرف می‌زنند" : "چه چیزی گفته شد؟"}
          dir="auto" maxLength={1200} />
        <select value={mode} onChange={(e) => setMode(e.target.value as typeof mode)}>
          {(["exact", "fuzzy", "semantic", "topic", "visual", "multimodal"] as const).map((m) => <option key={m} value={m}>{MODE_FA[m]}</option>)}
        </select>
        {mode === "multimodal" && (
          <button type="button" disabled={busy || !query.trim()} onClick={() => void splitRequest()}
            title="فقط دو بخش را پیشنهاد می‌کند؛ جست‌وجو بعد از تأیید شما انجام می‌شود">
            جدا کردن دو بخش
          </button>
        )}
        {mode === "topic" && (
          <label title="فقط وقتی جست‌وجوی اول ضعیف یا پرسش خیلی کوتاه باشد؛ عبارت‌ها راهنما هستند، نه نتیجه">
            <input type="checkbox" checked={brainHints} onChange={(e) => setBrainHints(e.target.checked)} />
            پیشنهاد عبارت با مغز سریع
          </label>
        )}
        <button type="button" disabled={busy || (mode === "multimodal" ? !saidText.trim() || !shownText.trim() : !query.trim())}
          onClick={() => void search()}>جست‌وجو</button>
      </div>
      {mode === "multimodal" && (
        <div className="media-row media-multimodal-fields">
          <label>
            آنچه گفته می‌شود (گفتگو)
            <input value={saidText} onChange={(e) => setSaidText(e.target.value)} dir="auto" maxLength={MULTIMODAL_MAX_CONSTRAINT_CHARS} />
          </label>
          <label>
            آنچه دیده می‌شود (تصویر)
            <input value={shownText} onChange={(e) => setShownText(e.target.value)} dir="auto" maxLength={MULTIMODAL_MAX_CONSTRAINT_CHARS} />
          </label>
          <label>
            فاصلهٔ زمانی
            <select value={windowMs} onChange={(e) => setWindowMs(Number(e.target.value))}>
              {MULTIMODAL_WINDOWS_MS.map((ms) => <option key={ms} value={ms}>{ms / 1000} ثانیه</option>)}
            </select>
          </label>
          <label title="جمله‌های هم‌معنا با bge-m3 هم پذیرفته می‌شوند؛ شباهت کالیبره‌نشده است">
            <input type="checkbox" checked={semanticSaid} onChange={(e) => setSemanticSaid(e.target.checked)} />
            گفتگوی هم‌معنا
          </label>
          <label title="فریم‌ها چطور با شرط تصویر سنجیده شوند؛ هیچ‌کدام احتمال یا شناسایی نیست">
            تطبیق تصویر
            <select value={shownMatch} onChange={(e) => setShownMatch(e.target.value as VisualMatch)}>
              <option value="lexical">هم‌واژه در متن تولیدشدهٔ فریم‌ها</option>
              <option value="semantic">هم‌معنا از روی متن تولیدشده (bge-m3، نه شباهت تصویر)</option>
              <option value="image">شباهت تصویری خود فریم‌ها (SigLIP 2، کالیبره‌نشده)</option>
            </select>
          </label>
        </div>
      )}
      {mode === "multimodal" && interpretation && (
        <p className="media-topic-hints" role="status">
          {interpretation.status === "proposed"
            ? (interpretation.generated
              ? "پیشنهاد مغز سریع از واژه‌های خود شما (تولیدشده، تأییدنشده): دو بخش بالا را بررسی یا ویرایش کنید و بعد جست‌وجو کنید."
              : "از برچسب‌های خود شما جدا شد: دو بخش بالا را بررسی کنید و بعد جست‌وجو کنید.")
            : interpretation.status === "ambiguous"
              ? `درخواست را نشد با اطمینان به دو بخش جدا کرد (${interpretation.reason ?? "ambiguous"})؛ دو بخش را خودتان بنویسید.`
              : `جدا کردن خودکار در دسترس نیست (${refusalText(interpretation.reason ?? "model_unavailable")})؛ دو بخش را خودتان بنویسید.`}
        </p>
      )}
      <div className="media-row media-report-options">
        <label>
          نوع گزارش
          <select value={reportKind} onChange={(e) => setReportKind(e.target.value as ReportKind)}>
            {REPORT_KINDS.map((kind) => <option key={kind} value={kind}>{REPORT_KIND_TEXT[kind]}</option>)}
          </select>
        </label>
        <label>
          قالب
          <select value={reportFormat} onChange={(e) => setReportFormat(e.target.value as ReportFormat)}>
            {REPORT_FORMATS.map((format) => <option key={format} value={format}>{format.toUpperCase()}</option>)}
          </select>
        </label>
        <input value={destination} onChange={(e) => setDestination(e.target.value)}
          placeholder={`مسیر فایل خروجی گزارش (.${reportFormat})`} dir="ltr" maxLength={4096} />
      </div>
      {semanticNote && <p className="media-notice">{semanticNote}</p>}
      {topic.note && <p className="media-notice">{topic.note}</p>}
      {visual.note && <p className="media-notice">{visual.note}</p>}
      {visual.semanticNote && <p className="media-notice">{visual.semanticNote}</p>}
      {visual.imageNote && <p className="media-notice">{visual.imageNote}</p>}
      {multimodal.note && <p className="media-notice">{multimodal.note}</p>}
      {multimodal.channelNotes.map((text, i) => <p key={`mn${i}`} className="media-notice">{text}</p>)}
      {multimodal.hits.length > 0 && <>
        <h4>نتیجه‌های ترکیبی (هر کدام با هر دو شرط در همان ویدیو و نزدیک هم در زمان؛ رتبه احتمال نیست)</h4>
        <ul className="media-results">{multimodal.hits.map((hit, i) => multimodalCard(hit, `m${i}`))}</ul>
      </>}
      {multimodal.answered && (
        <p className="media-topic-hints">
          گوینده و OCR هنوز ساخته نشده‌اند و در این جست‌وجو به کار نرفته‌اند.
          {shownMatch === "image" ? "" : " شباهت تصویری فریم‌ها فقط با گزینهٔ «شباهت تصویری خود فریم‌ها» به کار می‌رود."}
        </p>
      )}
      {multimodal.resultId && (
        <div className="media-row media-search-report-actions">
          <button type="button" disabled={busy} onClick={() => void previewSearchReport(multimodal.resultId as string)}>
            پیش‌نمایش گزارش همین نتیجه‌ها
          </button>
          <button type="button" disabled={busy} onClick={() => void generateSearchReport(multimodal.resultId as string)}>
            ساخت گزارش نتیجه‌ها ({reportFormat})
          </button>
          <button type="button" disabled={busy || !destination.trim()}
            onClick={() => void exportSearchReport(multimodal.resultId as string)}>
            خروجی گرفتن از گزارش نتیجه‌ها
          </button>
        </div>
      )}
      {searchPreview && (
        <section className="media-report-preview" aria-label="پیش‌نمایش گزارش نتیجه‌های جست‌وجو">
          <div className="media-card-head">
            <strong>گزارش نتیجه‌های جست‌وجوی ترکیبی</strong>
            <button type="button" onClick={() => setSearchPreview(null)}>بستن</button>
          </div>
          <p>
            گفتگو «<span dir="auto">{searchPreview.query.dialogue}</span>» و تصویر «<span dir="auto">{searchPreview.query.visual}</span>»
            {" — "}فاصلهٔ {Math.round(searchPreview.query.windowMs / 1000)} ثانیه — وضعیت {searchPreview.status}
          </p>
          <p>
            بررسی دوباره پیش از گزارش: {searchPreview.recheck.kept} نتیجه ماند؛ {searchPreview.recheck.outOfScope} خارج از محدوده،
            {" "}{searchPreview.recheck.permission} بدون اجازهٔ خواندن، {searchPreview.recheck.changed} با شواهد تغییرکرده حذف شد.
          </p>
          <ul className="media-report-lines">
            {searchPreview.results.map((hit, i) => (
              <li key={i}>
                <span className="ltr">{clock(hit.startMs)}</span> <span dir="auto">{hit.title || "ویدیو"}</span>:{" "}
                <span dir="auto">{hit.text}</span>
                {hit.frames.map((frame, j) => (
                  <small key={j}> — فریم <span className="ltr">{clock(frame.timeMs)}</span>{" "}
                    (توصیف تولیدشده، تأییدنشده): <span dir="auto">{frame.caption}</span></small>
                ))}
              </li>
            ))}
          </ul>
          {searchPreview.resultsOmitted > 0 && <p>{searchPreview.resultsOmitted} نتیجهٔ دیگر در گزارش کامل هست.</p>}
          <h4>محدودیت‌ها</h4>
          <ul>{searchPreview.limitations.map((text, i) => <li key={i}>{text}</li>)}</ul>
        </section>
      )}
      {visual.hits.length > 0 && <>
        <h4>فریم‌های هم‌خوان (جست‌وجوی واژه‌ای در متن تولیدشدهٔ فریم‌ها، نه شباهت تصویر)</h4>
        <ul className="media-results">{visual.hits.map((hit, i) => visualCard(hit, `v${i}`))}</ul>
      </>}
      {visual.imageHits.length > 0 && <>
        <h4>شباهت تصویری فریم‌ها (بردار تصویر خود فریم با بردار متن پرسش؛ SigLIP 2، کالیبره‌نشده، نه شناسایی)</h4>
        <ul className="media-results">{visual.imageHits.map((hit, i) => imageCard(hit, `i${i}`))}</ul>
      </>}
      {topic.brainHints.length > 0 && (
        <p className="media-topic-hints">
          عبارت‌های پیشنهادی مغز سریع (تولیدشده؛ راهنمای جست‌وجو، نه واقعیت و نه نتیجه): <span dir="auto">{topic.brainHints.join("، ")}</span>
        </p>
      )}
      {topic.brainHintsReason && (
        <p className="media-topic-hints">پیشنهاد مغز سریع به کار نرفت ({topic.brainHintsReason})؛ جست‌وجوی موضوعی بدون آن انجام شد.</p>
      )}
      {topic.expanded && topic.hints.length > 0 && (
        <p className="media-topic-hints">
          جست‌وجو با واژه‌های جمله‌های نزدیک گسترش یافت (راهنمای جست‌وجو، نه واقعیت): <span dir="auto">{topic.hints.join("، ")}</span>
        </p>
      )}
      {topic.hits.length > 0 && <>
        <h4>بخش‌های هم‌موضوع (هر نتیجه با جمله‌های نمایه‌شدهٔ خودش)</h4>
        <ul className="media-results">{topic.hits.map((hit, i) => topicCard(hit, `t${i}`))}</ul>
      </>}
      <ul className="media-results">{exact.map((hit, i) => card(hit, `e${i}`))}</ul>
      {semantic.length > 0 && <>
        <h4>نتیجه‌های معنایی (شباهت تأییدنشده)</h4>
        <ul className="media-results">{semantic.map((hit, i) => card(hit, `s${i}`))}</ul>
      </>}

      {preview && (
        <section className="media-report-preview" aria-label="پیش‌نمایش گزارش">
          <div className="media-card-head">
            <strong>{REPORT_KIND_TEXT[preview.kind] ?? preview.kind}: {preview.asset.title || "ویدیو"}</strong>
            <button type="button" onClick={() => setPreview(null)}>بستن</button>
          </div>
          <p>SHA-256: <span className="ltr">{preview.asset.sha256}</span></p>
          <p>
            {preview.summary.dialogueLines} سطر دیالوگ، {preview.summary.scenes} برش، {preview.summary.keyframes} فریم کلیدی
            {" — "}ساخته‌شده از فهرست، بدون اجرای مدل (<span className="ltr">{preview.generatedAt}</span>)
          </p>
          <h4>منابع دیالوگ</h4>
          <ul>
            {preview.subtitleTracks.map((track, i) => (
              <li key={i}>{SOURCE_FA[track.source] ?? track.source} — {track.language ?? "نامشخص"} — {track.cueCount} سطر</li>
            ))}
          </ul>
          {preview.dialogue && preview.dialogue.length > 0 && <>
            <h4>نخستین سطرها</h4>
            <ul className="media-report-lines">
              {preview.dialogue.map((line, i) => (
                <li key={i}><span className="ltr">{clock(line.startMs)}</span> <span dir="auto">{line.text}</span></li>
              ))}
            </ul>
          </>}
          <h4>در این فایل در دسترس نیست</h4>
          <p className="media-report-missing">
            {preview.notAvailable.map((item) => CAPABILITY_FA[item.capability] ?? item.capability).join("، ")}
          </p>
          <h4>محدودیت‌ها</h4>
          <ul>{preview.limitations.map((item) => <li key={item.code}>{item.text}</li>)}</ul>
        </section>
      )}

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
