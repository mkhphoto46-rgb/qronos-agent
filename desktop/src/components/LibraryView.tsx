import {
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";

import type {
  CSSProperties,
  MouseEvent,
} from "react";

import {
  libraryCopy,
  libraryDelete,
  libraryMove,
  libraryOpen,
  libraryOpenFolder,
  libraryPickFolder,
  libraryRename,
  markLibrarySeen,
  refreshLibrary,
  useLibraryStore,
} from "../libraryStore";
import type { ArtifactCategory, LibraryArtifact } from "../libraryStore";

import "./LibraryView.css";

type ViewPhase =
  | "home"
  | "entering-conversations"
  | "conversations"
  | "leaving-conversations"
  | "entering-library"
  | "library"
  | "leaving-library";

type SortMode = "newest" | "oldest" | "az" | "za" | "type" | "size";
type FilterMode = "all" | ArtifactCategory;
type ViewMode = "compact" | "gallery";
type LibraryViewProps = {
  phase: ViewPhase;
  onClose: () => void;
  onShowConversation: (messageId: string) => void;
};
type MenuState = { x:number; y:number; artifactId:string };
type ConfirmState = { kind:"delete"|"forever"; ids:string[] } | null;
type Particle = {
  id:number; originX:number; originY:number; radiusX:number; radiusY:number; phase:number; speed:number; size:number; alpha:number;
  tone:"cyan"|"indigo"|"white";
};

const MAX_DPR = 1;
const TRANSITION_MS = 1120;
const PARTICLE_COUNT = 420;

function clamp01(value: number) {
  return Math.max(0, Math.min(1, value));
}

function smootherStep(value: number) {
  const t = clamp01(value);
  return t * t * t * (t * (t * 6 - 15) + 10);
}

function seededRandom(seed: number) {
  let value = seed >>> 0;

  return () => {
    value = (value * 1664525 + 1013904223) >>> 0;
    return value / 4294967296;
  };
}

function buildParticles() {
  const random = seededRandom(772451);

  return Array.from(
    { length: PARTICLE_COUNT },
    (_, index) => ({
      id: index,
      originX: 0.86 + random() * 0.1,
      originY: 0.12 + random() * 0.76,
      radiusX: 0.04 + random() * 0.18,
      radiusY: 0.025 + random() * 0.12,
      phase: random() * Math.PI * 2,
      speed: 0.3 + random() * 0.8,
      size:
        index % 7 === 0
          ? 0.42 + random() * 0.55
          : 0.2 + random() * 0.38,
      alpha: 0.12 + random() * 0.4,
      tone:
        index % 13 === 0
          ? "white"
          : index % 4 === 0
            ? "indigo"
            : "cyan",
    } satisfies Particle),
  );
}

const particles = buildParticles();

function LibraryParticleCanvas({
  phase,
}: {
  phase: ViewPhase;
}) {
  const canvasRef = useRef<HTMLCanvasElement | null>(null);
  const phaseRef = useRef<ViewPhase>(phase);
  const phaseStartedRef = useRef(performance.now());

  useEffect(() => {
    phaseRef.current = phase;
    phaseStartedRef.current = performance.now();
  }, [phase]);

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;

    const context = canvas.getContext("2d", { alpha: true });
    if (!context) return;

    let width = 1;
    let height = 1;
    let dpr = 1;
    let frame = 0;
    let visible = !document.hidden;

    const resize = () => {
      width = Math.max(1, window.innerWidth);
      height = Math.max(1, window.innerHeight);
      dpr = Math.min(window.devicePixelRatio || 1, MAX_DPR);

      canvas.width = Math.round(width * dpr);
      canvas.height = Math.round(height * dpr);
      canvas.style.width = `${width}px`;
      canvas.style.height = `${height}px`;

      context.setTransform(dpr, 0, 0, dpr, 0, 0);
    };

    resize();

    const handleResize = () => resize();
    const handleVisibility = () => {
      visible = !document.hidden;
    };

    window.addEventListener("resize", handleResize);
    document.addEventListener("visibilitychange", handleVisibility);

    const render = (timestamp: number) => {
      frame = window.requestAnimationFrame(render);
      if (!visible) return;

      context.clearRect(0, 0, width, height);

      const currentPhase = phaseRef.current;

      if (
        currentPhase === "home" ||
        currentPhase === "conversations" ||
        currentPhase === "entering-conversations" ||
        currentPhase === "leaving-conversations"
      ) {
        return;
      }

      const elapsed = timestamp - phaseStartedRef.current;
      let transition = 1;

      if (currentPhase === "entering-library") {
        transition = smootherStep(elapsed / TRANSITION_MS);
      } else if (currentPhase === "leaving-library") {
        transition = 1 - smootherStep(elapsed / TRANSITION_MS);
      }

      const time = timestamp * 0.001;

      for (const particle of particles) {
        const originX = particle.originX * width;
        const originY = particle.originY * height;

        const centerX =
          width * (0.47 + ((particle.id % 7) - 3) * 0.025);
        const centerY =
          height * (0.48 + ((particle.id % 9) - 4) * 0.016);

        const angle =
          particle.phase +
          time *
            particle.speed *
            (particle.id % 2 === 0 ? 1 : -1);

        const targetX =
          centerX +
          Math.cos(angle) * particle.radiusX * width;

        const targetY =
          centerY +
          Math.sin(angle * 1.14) * particle.radiusY * height;

        const controlX = width * (0.74 - particle.radiusX * 0.35);
        const controlY =
          height * (0.46 + Math.sin(particle.phase) * 0.18);

        const inv = 1 - transition;

        let x =
          inv * inv * originX +
          2 * inv * transition * controlX +
          transition * transition * targetX;

        let y =
          inv * inv * originY +
          2 * inv * transition * controlY +
          transition * transition * targetY;

        const travelEnvelope = Math.sin(transition * Math.PI);

        x +=
          Math.sin(particle.phase + transition * 7.2) *
          7 *
          travelEnvelope;

        y +=
          Math.cos(particle.phase * 1.4 + transition * 6.1) *
          5 *
          travelEnvelope;

        const alpha =
          particle.alpha *
          smootherStep(transition / 0.16) *
          (currentPhase === "library"
            ? 0.34 +
              ((Math.sin(time * (0.8 + particle.speed) + particle.phase) + 1) /
                2) *
                0.32
            : 0.76);

        if (alpha < 0.008) continue;

        context.globalAlpha = Math.min(1, alpha);
        context.fillStyle =
          particle.tone === "indigo"
            ? "rgba(102,126,255,1)"
            : particle.tone === "white"
              ? "rgba(242,252,255,1)"
              : "rgba(69,218,255,1)";

        context.beginPath();
        context.arc(x, y, particle.size, 0, Math.PI * 2);
        context.fill();
      }

      context.globalAlpha = 1;
    };

    frame = window.requestAnimationFrame(render);

    return () => {
      window.cancelAnimationFrame(frame);
      window.removeEventListener("resize", handleResize);
      document.removeEventListener("visibilitychange", handleVisibility);
    };
  }, []);

  return (
    <canvas
      ref={canvasRef}
      className="library-particle-canvas"
      aria-hidden="true"
    />
  );
}

function ArtifactIcon({ artifact }: { artifact: Pick<LibraryArtifact,"category"|"iconKey"> }) {
  const key=artifact.iconKey;
  if(key==="image"||key==="vector") return <svg viewBox="0 0 24 24" aria-hidden="true"><rect x="4" y="5" width="16" height="14" rx="2"/><circle cx="9" cy="10" r="1.5"/><path d="m6 17 4-4 3 3 2-2 3 3"/>{key==="vector"&&<path d="M8 6h8M12 6v12"/>}</svg>;
  if(key==="video") return <svg viewBox="0 0 24 24" aria-hidden="true"><rect x="4" y="5" width="16" height="14" rx="2"/><path d="m10 9 5 3-5 3Z"/></svg>;
  if(key==="pdf") return <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M7 3h7l4 4v14H7Z"/><path d="M14 3v5h5"/><path d="M9 15h6M9 12h4"/></svg>;
  if(key==="audio") return <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M5 14v-4M8 17V7M11 15V9M14 19V5M17 15V9M20 13v-2"/></svg>;
  if(key==="link") return <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M9.5 14.5 14.5 9.5"/><path d="M7.2 16.8 5.7 18.3a3.3 3.3 0 0 1-4.7-4.7l3.5-3.5a3.3 3.3 0 0 1 4.7 0"/><path d="m16.8 7.2 1.5-1.5A3.3 3.3 0 1 1 23 10.4l-3.5 3.5a3.3 3.3 0 0 1-4.7 0"/></svg>;
  if(key==="archive") return <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M5 6h14v14H5Z"/><path d="M7 3h10v3H7ZM12 8v8M10 10h4M10 13h4"/></svg>;
  if(key==="spreadsheet") return <svg viewBox="0 0 24 24" aria-hidden="true"><rect x="4" y="4" width="16" height="16" rx="2"/><path d="M4 9h16M9 4v16M14 9v11"/></svg>;
  if(key==="presentation") return <svg viewBox="0 0 24 24" aria-hidden="true"><rect x="4" y="4" width="16" height="12" rx="2"/><path d="M12 16v4M8 20h8M8 8h8M8 11h5"/></svg>;
  if(key==="json"||key==="code") return <svg viewBox="0 0 24 24" aria-hidden="true"><path d="m9 6-4 6 4 6M15 6l4 6-4 6M13 4l-2 16"/></svg>;
  if(key==="markdown") return <svg viewBox="0 0 24 24" aria-hidden="true"><rect x="3" y="5" width="18" height="14" rx="2"/><path d="M6 15V9l3 3 3-3v6M15 9v6M13.5 13.5 15 15l1.5-1.5"/></svg>;
  if(key==="word") return <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M7 3h7l4 4v14H7Z"/><path d="M14 3v5h5M9 12l1 5 2-4 2 4 1-5"/></svg>;
  return <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M7 3h7l4 4v14H7Z"/><path d="M14 3v5h5"/><path d="M9 12h6M9 15h6"/></svg>;
}

function formatBytes(bytes:number){ if(!bytes)return "0 B"; const units=["B","KB","MB","GB","TB"]; let value=bytes, index=0; while(value>=1024&&index<units.length-1){value/=1024;index++;} return `${value>=10||index===0?value.toFixed(0):value.toFixed(1)} ${units[index]}`; }
function dateGroup(timestamp:number){ const now=new Date(); const d=new Date(timestamp*1000); const today=new Date(now.getFullYear(),now.getMonth(),now.getDate()).getTime(); const day=new Date(d.getFullYear(),d.getMonth(),d.getDate()).getTime(); const delta=(today-day)/86400000; if(delta===0)return "Today"; if(delta===1)return "Yesterday"; if(delta<7)return "This week"; if(delta<31)return "This month"; return "Older"; }
function toneFor(a:LibraryArtifact){ if(a.category==="image"||a.category==="audio")return "cyan"; if(a.category==="pdf"||a.category==="link")return "indigo"; if(a.category==="video")return "violet"; return "neutral"; }
function availabilityLabel(a:LibraryArtifact){ if(a.availabilityStatus==="AVAILABLE")return "Available"; return a.category==="link"?"Link unavailable":"Missing file"; }
function canOpen(a:LibraryArtifact){ return a.availabilityStatus==="AVAILABLE"; }
function canOpenFolder(a:LibraryArtifact){ return a.category!=="link"&&a.availabilityStatus==="AVAILABLE"; }

function LibraryView({ phase,onClose,onShowConversation }:LibraryViewProps){
  const {artifacts,locations,loading,error,duplicateCount}=useLibraryStore(); const unavailableCount=artifacts.filter(a=>a.availabilityStatus!=="AVAILABLE").length;
  const [search,setSearch]=useState(""); const [filter,setFilter]=useState<FilterMode>("all"); const [sort,setSort]=useState<SortMode>("newest"); const [viewMode,setViewMode]=useState<ViewMode>("compact");
  const [selectionMode,setSelectionMode]=useState(false); const [selectedIds,setSelectedIds]=useState<Set<string>>(new Set()); const [anchorId,setAnchorId]=useState<string|null>(null); const [menu,setMenu]=useState<MenuState|null>(null); const [renamingId,setRenamingId]=useState<string|null>(null); const [renameValue,setRenameValue]=useState(""); const [confirm,setConfirm]=useState<ConfirmState>(null); const [busy,setBusy]=useState(false); const [toast,setToast]=useState<string|null>(null);
  const renameRef=useRef<HTMLInputElement|null>(null); const toastTimer=useRef<number|null>(null);
  const visible=phase==="entering-library"||phase==="library"||phase==="leaving-library";
  const phaseClass=phase==="entering-library"?"library-view-entering":phase==="leaving-library"?"library-view-leaving":phase==="library"?"library-view-visible":"";
  const showToast=(message:string)=>{setToast(message); if(toastTimer.current!==null)window.clearTimeout(toastTimer.current); toastTimer.current=window.setTimeout(()=>{setToast(null);toastTimer.current=null;},2400);};
  useEffect(()=>()=>{if(toastTimer.current!==null)window.clearTimeout(toastTimer.current);},[]);
  useEffect(()=>{ if(phase==="library"){ void refreshLibrary(); } },[phase]);
  useEffect(()=>{if(renamingId&&renameRef.current){renameRef.current.focus();const dot=renameRef.current.value.lastIndexOf('.');renameRef.current.setSelectionRange(0,dot>0?dot:renameRef.current.value.length);}},[renamingId]);
  useEffect(()=>{const close=()=>setMenu(null);window.addEventListener("click",close);window.addEventListener("blur",close);return()=>{window.removeEventListener("click",close);window.removeEventListener("blur",close);};},[]);
  const filtered=useMemo(()=>{const q=search.trim().toLowerCase(); return [...artifacts].filter(a=>filter==="all"||a.category===filter).filter(a=>!q||a.name.toLowerCase().includes(q)||a.extension.toLowerCase().includes(q)||a.origin.toLowerCase().includes(q)||a.pathOrUrl.toLowerCase().includes(q)).sort((a,b)=>sort==="newest"?b.createdAt-a.createdAt:sort==="oldest"?a.createdAt-b.createdAt:sort==="az"?a.name.localeCompare(b.name,"fa"):sort==="za"?b.name.localeCompare(a.name,"fa"):sort==="type"?a.category.localeCompare(b.category):b.sizeBytes-a.sizeBytes);},[artifacts,filter,search,sort]);
  const groupOrder=["Today","Yesterday","This week","This month","Older"] as const; const groups=useMemo(()=>groupOrder.map(label=>({label,items:filtered.filter(a=>dateGroup(a.createdAt)===label)})).filter(g=>g.items.length),[filtered]);
  useEffect(()=>{const visibleIds=new Set(filtered.map(a=>a.id));setSelectedIds(current=>{const next=new Set(Array.from(current).filter(id=>visibleIds.has(id)));return next.size===current.size?current:next;});setAnchorId(current=>current&&visibleIds.has(current)?current:null);},[filter,search,artifacts]);
  const selected=artifacts.filter(a=>selectedIds.has(a.id)); const primary=selected.length===1?selected[0]:null; const totalSelectedSize=selected.reduce((n,a)=>n+a.sizeBytes,0);
  const acknowledgeArtifact=(id:string)=>{const item=artifacts.find(a=>a.id===id);if(item?.readState==="NEW")void markLibrarySeen([id]);};
  const setSingle=(id:string)=>{setSelectedIds(new Set([id]));setAnchorId(id);acknowledgeArtifact(id);};
  const toggle=(id:string,shift=false)=>{ if(shift&&anchorId){const a=filtered.findIndex(x=>x.id===anchorId),b=filtered.findIndex(x=>x.id===id); if(a>=0&&b>=0){const next=new Set(selectedIds);filtered.slice(Math.min(a,b),Math.max(a,b)+1).forEach(x=>next.add(x.id));setSelectedIds(next);return;}} const next=new Set(selectedIds);next.has(id)?next.delete(id):next.add(id);setSelectedIds(next);setAnchorId(id); };
  useEffect(()=>{const key=(event:KeyboardEvent)=>{if(!visible)return; if((event.ctrlKey||event.metaKey)&&event.key.toLowerCase()==="a"&&selectionMode){event.preventDefault();setSelectedIds(new Set(filtered.map(x=>x.id)));} if(event.key==="Escape"&&selectionMode){setSelectedIds(new Set());setSelectionMode(false);}};window.addEventListener("keydown",key);return()=>window.removeEventListener("keydown",key);},[visible,selectionMode,filtered]);
  const beginRename=(a:LibraryArtifact)=>{setSingle(a.id);setRenamingId(a.id);setRenameValue(a.name);setMenu(null);};
  const commitRename=async()=>{if(!renamingId)return;const value=renameValue.trim();setRenamingId(null);if(!value)return;try{setBusy(true);await libraryRename(renamingId,value);showToast("نام فایل تغییر کرد");}catch(e){showToast(`Rename failed: ${e instanceof Error?e.message:String(e)}`);}finally{setBusy(false);}};
  const pickAndRun=async(kind:"copy"|"move")=>{if(!selected.length)return;try{const dest=await libraryPickFolder();if(!dest)return;setBusy(true);const results=kind==="copy"?await libraryCopy(selected.map(x=>x.id),dest):await libraryMove(selected.map(x=>x.id),dest);const failed=results.filter(x=>!x.ok).length;showToast(failed?`${results.length-failed} انجام شد؛ ${failed} ناموفق`:`${selected.length} فایل ${kind==="copy"?"کپی":"منتقل"} شد`);setSelectedIds(new Set());}catch(e){showToast(String(e));}finally{setBusy(false);}};
  const doDelete=async()=>{if(!confirm)return;const ids=confirm.ids,forever=confirm.kind==="forever";setConfirm(null);try{setBusy(true);const results=await libraryDelete(ids,forever);const failed=results.filter(x=>!x.ok).length;showToast(failed?`${results.length-failed} حذف شد؛ ${failed} ناموفق`:forever?"حذف دائمی انجام شد":"به Recycle Bin منتقل شد");setSelectedIds(new Set());}catch(e){showToast(String(e));}finally{setBusy(false);}};
  const openContext=(event:MouseEvent<HTMLElement>,a:LibraryArtifact)=>{event.preventDefault();event.stopPropagation();if(!selectedIds.has(a.id))setSingle(a.id);setMenu({x:Math.min(event.clientX,window.innerWidth-220),y:Math.min(event.clientY,window.innerHeight-360),artifactId:a.id});};
  const copyPaths=async()=>{if(!selected.length)return;try{await navigator.clipboard.writeText(selected.map(a=>a.pathOrUrl).join("\n"));showToast(selected.length>1?"آدرس‌ها کپی شدند":"آدرس کپی شد");}catch{showToast("Clipboard در دسترس نیست");}};
  const runOpen=async(a:LibraryArtifact)=>{if(!canOpen(a)){showToast(a.category==="link"?"این لینک در دسترس نیست":"فایل در مسیر ثبت‌شده پیدا نشد");return;}try{await libraryOpen(a.id);}catch(e){showToast(`Open failed: ${e instanceof Error?e.message:String(e)}`);}};
  const runOpenFolder=async(a:LibraryArtifact)=>{if(!canOpenFolder(a)){showToast(a.category==="link"?"لینک وب پوشهٔ ویندوز ندارد":"فایل در مسیر ثبت‌شده پیدا نشد");return;}try{await libraryOpenFolder(a.id);}catch(e){showToast(`Open folder failed: ${e instanceof Error?e.message:String(e)}`);}};
  return <section className={`library-view ${phaseClass}`} aria-hidden={!visible} dir="rtl">
    <LibraryParticleCanvas phase={phase}/><div className="library-storage-origin" aria-hidden="true"><span className="library-storage-origin-core"/><span className="library-storage-origin-ring library-storage-origin-ring-a"/><span className="library-storage-origin-ring library-storage-origin-ring-b"/></div>
    <div className="library-responsive-shell"><header className="library-header"><div className="library-heading"><span>QRONOS ARTIFACT ARCHIVE</span><h1>کتابخانه</h1><p>فقط فایل‌ها و منابعی که Qronos ساخته یا جمع‌آوری کرده است</p></div><div className="library-summary"><strong>{artifacts.length}</strong><span>ARTIFACTS</span><i/><strong>{duplicateCount}</strong><span>DUPLICATE</span><i/><strong>{unavailableCount}</strong><span>UNAVAILABLE</span></div><button type="button" className="library-back" onClick={onClose}><span>بازگشت</span><svg viewBox="0 0 24 24"><path d="M15 5 8 12l7 7"/></svg></button></header>
    <div className="library-toolbar"><label className="library-search"><svg viewBox="0 0 24 24"><circle cx="11" cy="11" r="6"/><path d="m16 16 4 4"/></svg><input value={search} onChange={e=>setSearch(e.target.value)} placeholder="جستجو در کتابخانه..."/></label>
      <div className="library-filters">{([['all','همه'],['image','تصاویر'],['video','ویدیو'],['pdf','PDF'],['document','اسناد'],['audio','صوت'],['link','لینک'],['archive','آرشیو']] as const).map(([value,label])=><button key={value} type="button" className={filter===value?"library-filter library-filter-active":"library-filter"} onClick={()=>setFilter(value)}><span className="library-filter-label">{label}</span></button>)}</div>
      <div className="library-toolbar-right"><button type="button" className="library-select-mode" disabled={busy} onClick={()=>void refreshLibrary()}>REFRESH</button><button type="button" className={selectionMode?"library-select-mode active":"library-select-mode"} onClick={()=>{setSelectionMode(v=>!v);if(selectionMode)setSelectedIds(new Set());}}>SELECT</button>{selectionMode&&<><button type="button" className="library-select-all" onClick={()=>setSelectedIds(new Set(filtered.map(a=>a.id)))}>SELECT ALL</button><span className="library-selected-count">{selected.length} SELECTED</span></>}
      <label className="library-select-wrap"><span>SORT</span><select value={sort} onChange={e=>setSort(e.target.value as SortMode)}><option value="newest">جدیدترین</option><option value="oldest">قدیمی‌ترین</option><option value="az">A → Z</option><option value="za">Z → A</option><option value="type">نوع</option><option value="size">اندازه</option></select></label><div className="library-view-toggle"><button type="button" className={viewMode==="compact"?"active":""} onClick={()=>setViewMode("compact")}>COMPACT</button><button type="button" className={viewMode==="gallery"?"active":""} onClick={()=>setViewMode("gallery")}>GALLERY</button></div></div>
    </div>
    <div className="library-layout"><div className={`library-artifact-area library-artifact-area-${viewMode}`}>
      {loading&&<div className="library-empty"><strong>در حال بارگذاری Library...</strong><span>در حال خواندن رجیستری Artifactهای Qronos</span></div>}{error&&<div className="library-empty"><strong>Library error</strong><span>{error}</span><button type="button" className="library-select-mode" onClick={()=>void refreshLibrary()}>RETRY</button></div>}
      {!loading&&!error&&groups.map(group=><section key={group.label} className="library-date-group"><header className="library-date-heading"><span>{group.label}</span><i/><small>{group.items.length}</small></header><div className="library-artifact-list">{group.items.map(a=>{const isSelected=selectedIds.has(a.id);return <article key={a.id} className={`library-artifact-row library-artifact-${a.category} ${isSelected?"library-artifact-selected":""}`} tabIndex={0} onClick={e=>selectionMode?toggle(a.id,e.shiftKey):setSingle(a.id)} onDoubleClick={()=>{if(!selectionMode)void runOpen(a)}} onContextMenu={e=>openContext(e,a)}>{selectionMode&&<button type="button" className={isSelected?"library-check library-check-active":"library-check"} onClick={e=>{e.stopPropagation();toggle(a.id,e.shiftKey);}} aria-label="انتخاب فایل">{isSelected?"✓":""}</button>}<div className={`library-artifact-preview library-preview-${toneFor(a)}`}><ArtifactIcon artifact={a}/><strong>{a.extension}</strong></div><div className="library-artifact-main">{renamingId===a.id?<input ref={renameRef} className="library-rename-input" value={renameValue} onChange={e=>setRenameValue(e.target.value)} onClick={e=>e.stopPropagation()} onBlur={()=>void commitRename()} onKeyDown={e=>{if(e.key==="Enter")void commitRename();if(e.key==="Escape")setRenamingId(null);}}/>:<button type="button" className="library-artifact-name" onClick={e=>{e.stopPropagation();if(!selectionMode&&selectedIds.has(a.id)&&selectedIds.size===1)beginRename(a);else if(selectionMode)toggle(a.id,e.shiftKey);else setSingle(a.id);}}>{a.name}</button>}<div className="library-artifact-meta-line"><span>{a.extension}</span><i/><span>{new Date(a.createdAt*1000).toLocaleDateString('en-GB')}</span><i/><span>{new Date(a.createdAt*1000).toLocaleTimeString('en-GB',{hour:'2-digit',minute:'2-digit',hour12:false})}</span></div><div className="library-artifact-meta-line library-artifact-meta-secondary"><span>{formatBytes(a.sizeBytes)}</span><i/><span>{a.origin}</span></div></div><div className="library-artifact-status-zone"><span className={`library-artifact-status ${a.availabilityStatus==="LINK_ERROR"?"library-status-link-error":"library-status-available"}`}>{availabilityLabel(a)}</span>{a.duplicateState==="DUPLICATE"&&<span className="library-artifact-status library-status-duplicate">Duplicate</span>}{a.readState==="NEW"&&<small className="library-new-tag">NEW</small>}</div></article>})}</div></section>)}
      {!loading&&!error&&filtered.length===0&&<div className="library-empty"><strong>{artifacts.length===0?"کتابخانه هنوز خالی است":"موردی پیدا نشد"}</strong><span>{artifacts.length===0?"Artifactهای ساخته‌شده یا جمع‌آوری‌شده توسط Qronos اینجا ظاهر می‌شوند.":"Filter یا Search را تغییر بده."}</span>{artifacts.length===0&&<small>{locations?.artifactRoot||"Qronos Library"}</small>}</div>}
    </div>
    <aside className="library-detail-panel">{selected.length===0?<div className="library-detail-placeholder"><strong>SELECT AN ARTIFACT</strong><span>{locations?.artifactRoot||"Qronos Library"}</span></div>:selected.length>1?<><div className="library-multi-summary"><span>MULTIPLE ARTIFACTS</span><strong>{selected.length} selected</strong><p>{formatBytes(totalSelectedSize)}</p></div><div className="library-detail-actions"><button type="button" onClick={()=>void pickAndRun("copy")}>COPY</button><button type="button" onClick={()=>void pickAndRun("move")}>MOVE</button><button type="button" onClick={()=>void copyPaths()}>COPY PATHS</button></div><div className="library-danger-actions"><button type="button" onClick={()=>setConfirm({kind:"delete",ids:selected.map(x=>x.id)})}>DELETE</button><button type="button" onClick={()=>setConfirm({kind:"forever",ids:selected.map(x=>x.id)})}>DELETE FOREVER</button></div></>:primary&&<><div className={`library-detail-preview library-preview-${toneFor(primary)}`}><ArtifactIcon artifact={primary}/><strong>{primary.extension}</strong></div><div className="library-detail-title"><span>SELECTED ARTIFACT</span><h2>{primary.name}</h2></div><dl className="library-detail-grid"><div><dt>TYPE</dt><dd>{primary.extension}</dd></div><div><dt>SIZE</dt><dd>{formatBytes(primary.sizeBytes)}</dd></div><div className="wide"><dt>SOURCE</dt><dd>{primary.origin}</dd></div><div className="wide"><dt>PATH / URL</dt><dd title={primary.pathOrUrl}>{primary.pathOrUrl}</dd></div></dl>{primary.duplicateState==="DUPLICATE"&&<div className="library-duplicate-panel"><span>DUPLICATE DETECTED</span><strong>EXACT CONTENT HASH MATCH</strong><p>{primary.duplicateGroupId}</p></div>}{primary.availabilityStatus==="LINK_ERROR"&&<div className="library-link-error-panel"><span>{primary.category==="link"?"LINK UNAVAILABLE":"FILE UNAVAILABLE"}</span><strong>{primary.linkErrorReason||"unavailable"}</strong></div>}<div className="library-detail-actions"><button type="button" className="library-primary-action" disabled={busy||!canOpen(primary)} title={canOpen(primary)?"باز کردن Artifact":primary.category==="link"?"لینک در دسترس نیست":"فایل پیدا نشد"} onClick={()=>void runOpen(primary)}>OPEN</button><button type="button" onClick={()=>beginRename(primary)}>RENAME</button><button type="button" disabled={busy||!canOpenFolder(primary)} title={primary.category==="link"?"لینک وب پوشهٔ ویندوز ندارد":canOpenFolder(primary)?"نمایش فایل در پوشه":"فایل پیدا نشد"} onClick={()=>void runOpenFolder(primary)}>OPEN FOLDER</button><button type="button" disabled={!primary.sourceMessageId} title={primary.sourceMessageId?"رفتن به پیام منبع":"این Artifact گفتگوی منبع ثبت‌شده ندارد"} onClick={()=>primary.sourceMessageId&&onShowConversation(primary.sourceMessageId)}>SHOW IN CONVERSATION</button><button type="button" onClick={()=>void copyPaths()}>COPY {primary.category==="link"?"LINK":"PATH"}</button></div><div className="library-danger-actions"><button type="button" onClick={()=>setConfirm({kind:"delete",ids:[primary.id]})}>DELETE</button><button type="button" onClick={()=>setConfirm({kind:"forever",ids:[primary.id]})}>DELETE FOREVER</button></div></>}</aside>
    </div></div>
    {selectionMode&&selected.length>0&&<div className="library-selection-bar"><strong>{selected.length} SELECTED</strong><button onClick={()=>void pickAndRun("copy")}>COPY</button><button onClick={()=>void pickAndRun("move")}>MOVE</button><button onClick={()=>void copyPaths()}>COPY PATHS</button><button className="danger" onClick={()=>setConfirm({kind:"delete",ids:selected.map(x=>x.id)})}>DELETE</button><button onClick={()=>setSelectedIds(new Set())}>CLEAR</button></div>}
    {menu&&(()=>{const a=artifacts.find(x=>x.id===menu.artifactId);if(!a)return null;return <div className="library-context-menu" style={{left:`${menu.x}px`,top:`${menu.y}px`} as CSSProperties} onClick={e=>e.stopPropagation()}><button disabled={!canOpen(a)} onClick={()=>void runOpen(a)}>Open</button>{a.category!=="link"&&<button disabled={!canOpenFolder(a)} onClick={()=>void runOpenFolder(a)}>Open folder</button>}{selectedIds.size<=1&&<button onClick={()=>beginRename(a)}>Rename</button>}<button onClick={()=>void copyPaths()}>Copy path</button>{selectedIds.size===1&&<button disabled={!a.sourceMessageId} onClick={()=>a.sourceMessageId&&onShowConversation(a.sourceMessageId)}>Show in conversation</button>}<span className="library-context-separator"/><button className="danger" onClick={()=>setConfirm({kind:"delete",ids:Array.from(selectedIds)})}>Delete</button><button className="danger danger-strong" onClick={()=>setConfirm({kind:"forever",ids:Array.from(selectedIds)})}>Delete forever</button></div>})()}
    {confirm&&<div className="library-modal-backdrop" onMouseDown={()=>!busy&&setConfirm(null)}><div className={`library-confirm-modal ${confirm.kind==="forever"?"library-confirm-forever":""}`} onMouseDown={e=>e.stopPropagation()}><div className="library-confirm-warning">⚠</div><div className="library-confirm-copy"><span>{confirm.kind==="forever"?"PERMANENT DELETE":"RECYCLE BIN"}</span><h3>{confirm.ids.length===1?(artifacts.find(a=>a.id===confirm.ids[0])?.name||"Artifact"):`${confirm.ids.length} Artifacts`}</h3><p>{confirm.kind==="forever"?"این عملیات قابل بازگشت نیست.":"فایل‌ها به Windows Recycle Bin منتقل می‌شوند و قابل بازیابی هستند."}</p>{confirm.ids.length===1&&<small>{artifacts.find(a=>a.id===confirm.ids[0])?.pathOrUrl}</small>}<div className="library-confirm-actions"><button onClick={()=>setConfirm(null)} disabled={busy}>انصراف</button><button className="danger" onClick={()=>void doDelete()} disabled={busy}>{busy?"در حال انجام...":confirm.kind==="forever"?"حذف برای همیشه":"حذف"}</button></div></div></div></div>}
    {toast&&<div className="library-toast">{toast}</div>}
  </section>;
}

export default LibraryView;
