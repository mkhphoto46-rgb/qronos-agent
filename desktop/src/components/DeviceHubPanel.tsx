import { useEffect, useState } from "react";

import { invoke } from "@tauri-apps/api/core";
import { listen } from "@tauri-apps/api/event";

import {
  approvalOf, createDeviceHubClient, devicesOf, handoffText, IMAGING_UNSUPPORTED_TEXT, imagingDevicesOf,
  isoWithOffset, jobsOf, limitText, print3dProblem, SCAN_LIMIT_TEXT, scanProblem, scanSavedText,
  printProblem, refusalText, unsupportedClassesOf, wakeArmedOf, wakeArmedText, wakeCapabilityOf, WAKE_LIMIT_TEXT,
  WAKE_VERDICT_TEXT,
} from "../deviceHubStore";
import type {
  Device, DeviceApproval, DeviceHubClient, DeviceHubTransport, DeviceJob, JobCommand, PrintColor, RuntimeEvent,
  ImagingDevice, UnsupportedClass, WakeArmed, WakeCapability,
} from "../deviceHubStore";

/** The production transport: the registered send_runtime_action command and the runtime event stream. */
export function createTauriDeviceHubTransport(): DeviceHubTransport {
  return {
    send: async (actionId, data) => {
      await invoke("send_runtime_action", { actionId, data });
    },
    listen: (handler) => listen<RuntimeEvent>("qronos://runtime-event", (event) => handler(event.payload)),
    setTimer: (callback, ms) => window.setTimeout(callback, ms),
    clearTimer: (handle) => window.clearTimeout(handle as number),
  };
}

export function createDesktopDeviceHubClient(): DeviceHubClient {
  return createDeviceHubClient(createTauriDeviceHubTransport());
}

const COMMAND_FA: Record<JobCommand, string> = { pause: "توقف", resume: "ادامه", cancel: "لغو" };
const COLOR_FA: Record<PrintColor, string> = { default: "پیش‌فرض چاپگر", color: "رنگی", monochrome: "سیاه و سفید" };
const JOB_STATUS_FA: Record<string, string> = {
  paused: "متوقف", error: "خطا", deleting: "در حال حذف", spooling: "در صف", printing: "در حال چاپ", offline: "آفلاین",
  paper_out: "کاغذ تمام", printed: "چاپ‌شده", deleted: "حذف‌شده", blocked: "مسدود", user_intervention: "نیاز به رسیدگی",
  restarting: "شروع دوباره", complete: "کامل",
};

function isScanApproval(approval: DeviceApproval): boolean {
  return approval.preview.action === "scan";
}

function isWakeApproval(approval: DeviceApproval): boolean {
  return approval.preview.action === "wake_schedule" || approval.preview.action === "wake_cancel";
}

type DeviceHubPanelProps = { client: DeviceHubClient; visible: boolean };

function DeviceHubPanel({ client, visible }: DeviceHubPanelProps) {
  const [devices, setDevices] = useState<Device[]>([]);
  const [unsupported, setUnsupported] = useState<UnsupportedClass[]>([]);
  const [selected, setSelected] = useState<string>("");
  const [jobs, setJobs] = useState<DeviceJob[]>([]);
  const [message, setMessage] = useState<string>("");
  const [busy, setBusy] = useState(false);
  const [approval, setApproval] = useState<DeviceApproval | null>(null);
  const [secret, setSecret] = useState("");
  const [path, setPath] = useState("");
  const [copies, setCopies] = useState(1);
  const [color, setColor] = useState<PrintColor>("default");
  const [pages, setPages] = useState("all");
  const [path3d, setPath3d] = useState("");
  const [wakeCap, setWakeCap] = useState<WakeCapability | null>(null);
  const [wakeArmed, setWakeArmed] = useState<WakeArmed | null>(null);
  const [wakeLocal, setWakeLocal] = useState("");
  const [wakeStay, setWakeStay] = useState(10);
  const [wakeNote, setWakeNote] = useState("");
  const [imaging, setImaging] = useState<ImagingDevice[]>([]);
  const [scanner, setScanner] = useState("");
  const [scanPath, setScanPath] = useState("");
  const [scanNote, setScanNote] = useState("");

  const device = devices.find((item) => item.id === selected);

  async function refresh(): Promise<void> {
    setBusy(true);
    const result = await client.list();
    setBusy(false);
    if (!result.ok) {
      setMessage(refusalText(result.code));
      return;
    }
    const found = devicesOf(result);
    setDevices(found);
    setUnsupported(unsupportedClassesOf(result));
    if (!found.some((item) => item.id === selected)) setSelected("");
    setMessage(found.length === 0 ? "ویندوز هیچ چاپگری نصب نکرده است." : "");
  }

  async function loadJobs(deviceId: string): Promise<void> {
    const result = await client.jobs(deviceId);
    if (!result.ok) {
      setJobs([]);
      setMessage(refusalText(result.code));
      return;
    }
    setJobs(jobsOf(result));
  }

  useEffect(() => {
    if (visible) {
      void refresh();
      return;
    }
    // Hidden: every pending answer becomes stale and the secret is forgotten.
    client.reset();
    setApproval(null);
    setSecret("");
    setJobs([]);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [visible, client]);

  useEffect(() => {
    if (visible && selected) void loadJobs(selected);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selected, visible]);

  async function control(job: DeviceJob, command: JobCommand): Promise<void> {
    if (!device) return;
    setBusy(true);
    const result = await client.controlJob(device.id, job.jobId, command);
    setBusy(false);
    const asked = approvalOf(result);
    if (asked) {
      setApproval(asked);
      setMessage("این تغییر به تأیید شما نیاز دارد.");
      return;
    }
    setMessage(result.ok ? `«${job.document}»: ${COMMAND_FA[command]} انجام شد.` : refusalText(result.code));
    await loadJobs(device.id);
  }

  async function answer(approved: boolean): Promise<void> {
    if (!approval) return;
    const current = approval;
    const typed = secret;
    setSecret("");  // the secret lives only until this one send
    setApproval(null);
    const result = approved ? await client.decide(current, typed) : await client.decline(current);
    setMessage(!result.ok ? refusalText(result.code)
      : result.status === "handed_to_spooler" ? handoffText(result)
      : result.status === "armed" ? wakeArmedText({ wakeId: String(result.wakeId), dueUtc: String(result.dueUtc),
          stayAwakeMinutes: Number(result.stayAwakeMinutes), state: "armed" }, localTime(String(result.dueUtc)))
      : result.status === "cancelled" ? "زمان بیدار شدن لغو شد."
      : result.status === "saved" ? scanSavedText(result)
      : approved ? "انجام شد." : "لغو شد؛ چیزی تغییر نکرد.");
    if (isScanApproval(current)) {
      setScanNote(!result.ok ? refusalText(result.code) : scanSavedText(result));
      return;
    }
    if (isWakeApproval(current)) {
      setWakeNote(!result.ok ? refusalText(result.code) : "");
      await wakeStatus();
      return;
    }
    if (device) await loadJobs(device.id);
  }

  async function imagingRefresh(): Promise<void> {
    const result = await client.imagingList();
    if (!result.ok) {
      setImaging([]);
      setScanNote(refusalText(result.code));
      return;
    }
    const found = imagingDevicesOf(result);
    setImaging(found);
    if (!found.some((item) => item.id === scanner && item.kind === "scanner")) setScanner("");
    setScanNote(found.some((item) => item.kind === "scanner") ? "" : "ویندوز هیچ اسکنر WIA پشتیبانی‌شده‌ای گزارش نکرد.");
  }

  async function scanNow(): Promise<void> {
    const device = imaging.find((item) => item.id === scanner);
    const problem = scanProblem(device, scanPath);
    if (problem || !device) {
      setScanNote(problem ?? "یک اسکنر انتخاب کنید.");
      return;
    }
    setBusy(true);
    const result = await client.scan(device.id, scanPath);
    setBusy(false);
    const asked = approvalOf(result);
    if (asked) {
      setApproval(asked);
      setScanNote("اسکن به تأیید شما با رمز نیاز دارد.");
      return;
    }
    setScanNote(refusalText(result.code));
  }

  async function wakeStatus(): Promise<void> {
    const result = await client.wakeStatus();
    if (!result.ok) {
      setWakeCap(null);
      setWakeNote(refusalText(result.code));
      return;
    }
    setWakeCap(wakeCapabilityOf(result));
    setWakeArmed(wakeArmedOf(result));
    setWakeNote("");
  }

  async function wakeSchedule(): Promise<void> {
    const chosen = new Date(wakeLocal);
    const at = Number.isNaN(chosen.getTime()) ? null : isoWithOffset(wakeLocal, -chosen.getTimezoneOffset());
    if (!at) {
      setWakeNote("زمان را کامل انتخاب کنید (تاریخ و ساعت).");
      return;
    }
    setBusy(true);
    const result = await client.wakeSchedule(at, wakeStay);
    setBusy(false);
    const asked = approvalOf(result);
    if (asked) {
      setApproval(asked);
      setWakeNote("ثبت زمان بیدار شدن به تأیید شما با رمز نیاز دارد.");
      return;
    }
    setWakeNote(refusalText(result.code));
  }

  async function wakeCancel(): Promise<void> {
    setBusy(true);
    const result = await client.wakeCancel();
    setBusy(false);
    const asked = approvalOf(result);
    if (asked) {
      setApproval(asked);
      setWakeNote("لغو زمان بیدار شدن به تأیید شما نیاز دارد.");
      return;
    }
    setWakeNote(result.ok ? (result.status === "cancelled" ? "زمان بیدار شدن لغو شد." : "زمانی ثبت نشده بود.")
      : refusalText(result.code));
    await wakeStatus();
  }

  function localTime(utc: string): string {
    const when = new Date(utc);
    return Number.isNaN(when.getTime()) ? utc : when.toLocaleString("fa-IR");
  }

  async function print3d(): Promise<void> {
    if (!device) return;
    const fields = { deviceId: device.id, path: path3d };
    const problem = print3dProblem(device, fields);
    if (problem) {
      setMessage(problem);
      return;
    }
    setBusy(true);
    const result = await client.print3d(fields);
    setBusy(false);
    const asked = approvalOf(result);
    if (asked) {
      setApproval(asked);
      setMessage("سپردن این بسته به چاپگر سه‌بعدی به تأیید شما با رمز نیاز دارد.");
      return;
    }
    setMessage(refusalText(result.ok ? "bad_request" : result.code));
  }

  async function print(): Promise<void> {
    if (!device) return;
    const fields = { deviceId: device.id, path, copies, color, pages };
    const problem = printProblem(device, fields);
    if (problem) {
      setMessage(problem);
      return;
    }
    setBusy(true);
    const result = await client.print(fields);
    setBusy(false);
    setMessage(result.ok
      ? "درخواست چاپ فرستاده شد. جزئیات فایل و چاپگر در پنجرهٔ تأیید چاپ نشان داده می‌شود؛ بدون تأیید شما چیزی چاپ نمی‌شود."
      : refusalText(result.code));
  }

  return (
    <section className="device-hub" dir="rtl" aria-label="مرکز دستگاه‌ها">
      <header className="device-hub-header">
        <h2>مرکز دستگاه‌ها</h2>
        <button type="button" onClick={() => void refresh()} disabled={busy}>به‌روزرسانی</button>
      </header>
      <p className="device-hub-note">
        فقط دستگاه‌هایی که ویندوز نصب کرده (محلی یا چاپگرهای شبکه‌ای متصل‌شده) از راه سرویس چاپ و درایور خود ویندوز.
      </p>
      {message && <p className="device-hub-message" role="status">{message}</p>}

      <ul className="device-hub-list">
        {devices.map((item) => (
          <li key={item.id}>
            <button type="button" className={`device-card${item.id === selected ? " device-card-selected" : ""}`}
                    aria-pressed={item.id === selected} onClick={() => setSelected(item.id)}>
              <strong>{item.name}</strong>
              <span>{item.kind === "3d_printer" ? "چاپگر سه‌بعدی" : "چاپگر"} · {item.connection === "network" ? "شبکه" : "محلی"}
                {item.isDefault ? " · پیش‌فرض" : ""}</span>
              <span>{item.status.ready ? "آماده" : `مشکل: ${item.status.problems.join("، ")}`}</span>
              {item.status.warnings.length > 0 && <span>هشدار: {item.status.warnings.join("، ")}</span>}
              {item.unsupported && <span className="device-unsupported">{limitText(item.unsupported)}</span>}
            </button>
          </li>
        ))}
      </ul>

      {device && (
        <div className="device-detail">
          <h3>{device.name}</h3>
          {device.kind === "3d_printer" ? (
            device.workflows.includes("print_3d") ? (
              <dl className="device-capabilities">
                <dt>حجم ساخت (میلی‌متر)</dt>
                <dd dir="ltr">{device.capabilities.buildVolumeMicrons
                  ? [device.capabilities.buildVolumeMicrons.width, device.capabilities.buildVolumeMicrons.depth,
                     device.capabilities.buildVolumeMicrons.height].map((value) => (value / 1000).toFixed(1)).join(" × ")
                  : "نامعلوم"}</dd>
                <dt>نسخهٔ 3MF</dt><dd dir="ltr">{device.capabilities.threeMfVersion || "نامعلوم"}</dd>
                <dt>افزونه‌های 3MF</dt>
                <dd dir="ltr">{(device.capabilities.threeMfExtensions ?? []).join(" ") || "هیچ"}</dd>
              </dl>
            ) : <p className="device-unsupported">{limitText(device.unsupported)}</p>
          ) : (
            <dl className="device-capabilities">
              <dt>حداکثر نسخه</dt><dd>{device.capabilities.copiesMax ?? "نامعلوم"}</dd>
              <dt>رنگی</dt><dd>{device.capabilities.color ? "بله" : "خیر"}</dd>
              <dt>دورو</dt><dd>{device.capabilities.duplex ? "بله" : "خیر"}</dd>
              <dt>اندازهٔ کاغذ</dt><dd>{(device.capabilities.paperSizes ?? []).join("، ") || "نامعلوم"}</dd>
            </dl>
          )}

          {device.workflows.includes("jobs_view") && (
            <div className="device-jobs">
              <h4>صف چاپ</h4>
              {jobs.length === 0 ? <p>کاری در صف نیست.</p> : (
                <ul>
                  {jobs.map((job) => (
                    <li key={job.jobId}>
                      <span>{job.document || `کار ${job.jobId}`}</span>
                      <span>{job.status.map((s) => JOB_STATUS_FA[s] ?? s).join("، ")}</span>
                      <span>{job.totalPages > 0 ? `${job.pagesPrinted}/${job.totalPages} صفحه` : ""}</span>
                      {job.own && device.workflows.includes("job_control_own") ? (
                        <span className="device-job-actions">
                          {(["pause", "resume", "cancel"] as JobCommand[]).map((command) => (
                            <button key={command} type="button" disabled={busy || approval !== null}
                                    onClick={() => void control(job, command)}>{COMMAND_FA[command]}</button>
                          ))}
                        </span>
                      ) : <span className="device-job-foreign">کار کاربر دیگر</span>}
                    </li>
                  ))}
                </ul>
              )}
            </div>
          )}

          {approval && !isWakeApproval(approval) && !isScanApproval(approval) && (
            <div className="device-approval" role="dialog" aria-label="تأیید درخواست دستگاه">
              {typeof approval.preview.jobName === "string" ? (
                <p>
                  فایل <bdi dir="ltr">{String(approval.preview.file ?? "")}</bdi> به چاپگر سه‌بعدی
                  «{String(approval.preview.device ?? "")}» سپرده شود؟
                  {approval.preview.readAfterApproval === true
                    ? " (سیاست فایل‌ها خواندن بسته را به همین تأیید وابسته کرده است؛ بسته پس از تأیید خوانده و بررسی می‌شود.)"
                    : ` (${String(approval.preview.textures ?? 0)} بافت، SHA-256 ${String(approval.preview.sha256 ?? "").slice(0, 12)}…)`}
                </p>
              ) : (
                <p>
                  {COMMAND_FA[(approval.preview.command as JobCommand) ?? "pause"] ?? ""} «{String(approval.preview.document ?? "")}»
                  در «{String(approval.preview.device ?? "")}»؟
                </p>
              )}
              {approval.needsSecret && (
                <input type="password" value={secret} onChange={(event) => setSecret(event.target.value)}
                       placeholder="رمز تأیید" maxLength={256} autoComplete="off" />
              )}
              <button type="button" disabled={approval.needsSecret && !secret} onClick={() => void answer(true)}>تأیید</button>
              <button type="button" onClick={() => void answer(false)}>لغو</button>
            </div>
          )}

          {device.workflows.includes("print_document") && (
            <form className="device-print" onSubmit={(event) => { event.preventDefault(); void print(); }}>
              <h4>چاپ یک فایل مشخص</h4>
              <input type="text" value={path} onChange={(event) => setPath(event.target.value)} dir="ltr"
                     placeholder="D:\...\file.pdf" maxLength={4096} aria-label="مسیر کامل فایل" />
              <label>نسخه
                <input type="number" min={1} max={device.capabilities.copiesMax ?? 99} value={copies}
                       onChange={(event) => setCopies(Number(event.target.value))} />
              </label>
              <label>رنگ
                <select value={color} onChange={(event) => setColor(event.target.value as PrintColor)}>
                  {(Object.keys(COLOR_FA) as PrintColor[]).filter((key) => key !== "color" || device.capabilities.color)
                    .map((key) => <option key={key} value={key}>{COLOR_FA[key]}</option>)}
                </select>
              </label>
              <label>صفحه‌ها
                <input type="text" value={pages} onChange={(event) => setPages(event.target.value)} dir="ltr" maxLength={200} />
              </label>
              <button type="submit" disabled={busy}>درخواست چاپ</button>
            </form>
          )}

          {device.workflows.includes("print_3d") && (
            <form className="device-print" onSubmit={(event) => { event.preventDefault(); void print3d(); }}>
              <h4>سپردن یک فایل 3MF به چاپگر سه‌بعدی</h4>
              <input type="text" value={path3d} onChange={(event) => setPath3d(event.target.value)} dir="ltr"
                     placeholder="D:\...\part.3mf" maxLength={4096} aria-label="مسیر کامل فایل 3MF" />
              <p className="device-hub-note">
                فقط مدل و بافت‌های همین بسته، از راه درایور خود ویندوز. تنظیمات کیفیت، تکیه‌گاه و مواد در درایور یا خود
                دستگاه است، نه اینجا. بعد از تأیید با رمز، Qronos فقط تحویل به صف چاپ را گزارش می‌کند، نه چاپ شدن قطعه را.
              </p>
              <button type="submit" disabled={busy || approval !== null}>درخواست چاپ سه‌بعدی</button>
            </form>
          )}
        </div>
      )}

      <section className="device-wake" aria-label="اسکن به فایل">
        <h3>اسکن یک صفحه به یک فایل تازه</h3>
        <p className="device-hub-note">
          فقط اسکنرهای WIA ویندوز؛ اسکن در پنجرهٔ خود ویندوز انجام می‌شود و نتیجه در یک فایل تازهٔ PNG یا JPEG ذخیره می‌شود.
        </p>
        <button type="button" onClick={() => void imagingRefresh()} disabled={busy}>فهرست اسکنرها</button>
        {scanNote && <p className="device-hub-message" role="status">{scanNote}</p>}
        <ul className="device-hub-list">
          {imaging.map((item) => (
            <li key={item.id}>
              <button type="button" className={`device-card${item.id === scanner ? " device-card-selected" : ""}`}
                      aria-pressed={item.id === scanner} disabled={item.kind !== "scanner"}
                      onClick={() => setScanner(item.id)}>
                <strong>{item.name}</strong>
                <span>{item.kind === "scanner" ? "اسکنر" : item.kind === "camera" ? "دوربین" : item.kind === "video" ? "وب‌کم" : "دستگاه تصویر"}
                  {item.vendor ? ` · ${item.vendor}` : ""}</span>
                {item.unsupported && <span className="device-unsupported">{IMAGING_UNSUPPORTED_TEXT[item.unsupported] ?? item.unsupported}</span>}
              </button>
            </li>
          ))}
        </ul>
        <form className="device-print" onSubmit={(event) => { event.preventDefault(); void scanNow(); }}>
          <input type="text" value={scanPath} onChange={(event) => setScanPath(event.target.value)} dir="ltr"
                 placeholder="D:\...\scan.png" maxLength={4096} aria-label="مسیر فایل تازه" />
          <button type="submit" disabled={busy || approval !== null || !scanner}>اسکن</button>
        </form>
        <ul className="device-wake-limits">
          {Object.values(SCAN_LIMIT_TEXT).map((line) => <li key={line}>{line}</li>)}
        </ul>
        {approval && isScanApproval(approval) && (
          <div className="device-approval" role="dialog" aria-label="تأیید اسکن">
            <p>
              با «{String(approval.preview.device ?? "")}» یک صفحه اسکن و در
              {" "}<bdi dir="ltr">{String(approval.preview.file ?? "")}</bdi> ذخیره شود؟ پنجرهٔ اسکن ویندوز باز می‌شود.
            </p>
            {approval.needsSecret && (
              <input type="password" value={secret} onChange={(event) => setSecret(event.target.value)}
                     placeholder="رمز تأیید" maxLength={256} autoComplete="off" />
            )}
            <button type="button" disabled={approval.needsSecret && !secret} onClick={() => void answer(true)}>تأیید</button>
            <button type="button" onClick={() => void answer(false)}>لغو</button>
          </div>
        )}
      </section>

      <section className="device-wake" aria-label="بیدار کردن این کامپیوتر">
        <h3>بیدار کردن همین کامپیوتر از خواب</h3>
        <p className="device-hub-note">
          با زمان‌سنج سخت‌افزاری ویندوز، نه با صدا یا شبکه. کامپیوتر خواب هیچ برنامه‌ای (از جمله Qronos) را اجرا نمی‌کند.
        </p>
        <button type="button" onClick={() => void wakeStatus()} disabled={busy}>بررسی توانایی</button>
        {wakeNote && <p className="device-hub-message" role="status">{wakeNote}</p>}
        {wakeCap && (
          <>
            <p className={wakeCap.verdict === "supported" ? "" : "device-unsupported"}>
              {WAKE_VERDICT_TEXT[wakeCap.reason] ?? wakeCap.reason}
            </p>
            <dl className="device-capabilities">
              <dt>منبع برق</dt><dd>{wakeCap.powerSource === "battery" ? "باتری" : wakeCap.powerSource === "ac" ? "برق شهری" : "نامعلوم"}</dd>
              <dt>زمان‌سنج بیدارباش (برق شهری / باتری)</dt><dd dir="ltr">{wakeCap.wakeTimersAc ?? "?"} / {wakeCap.wakeTimersDc ?? "?"}</dd>
              <dt>Modern Standby</dt><dd>{wakeCap.modernStandby ? "بله (صفحه خاموش می‌ماند)" : "خیر"}</dd>
            </dl>
          </>
        )}
        {wakeArmed && wakeArmed.state !== "ended" && (
          <p role="status">{wakeArmedText(wakeArmed, localTime(wakeArmed.dueUtc))}</p>
        )}
        <form className="device-print" onSubmit={(event) => { event.preventDefault(); void wakeSchedule(); }}>
          <label>زمان بیدار شدن
            <input type="datetime-local" value={wakeLocal} onChange={(event) => setWakeLocal(event.target.value)} />
          </label>
          <label>بیدار ماندن پس از آن (دقیقه)
            <input type="number" min={0} max={120} value={wakeStay}
                   onChange={(event) => setWakeStay(Number(event.target.value))} />
          </label>
          <button type="submit" disabled={busy || approval !== null || !wakeLocal}>ثبت زمان بیدار شدن</button>
          <button type="button" disabled={busy || approval !== null} onClick={() => void wakeCancel()}>لغو زمان ثبت‌شده</button>
        </form>
        <ul className="device-wake-limits">
          {Object.values(WAKE_LIMIT_TEXT).map((line) => <li key={line}>{line}</li>)}
        </ul>
        {approval && isWakeApproval(approval) && (
          <div className="device-approval" role="dialog" aria-label="تأیید بیدار کردن کامپیوتر">
            <p>
              {approval.preview.action === "wake_cancel"
                ? `زمان بیدار شدنِ ${localTime(String(approval.preview.dueUtc ?? ""))} لغو شود؟`
                : `این کامپیوتر در ${localTime(String(approval.preview.dueUtc ?? ""))} از خواب بیدار شود و `
                  + `${String(approval.preview.stayAwakeMinutes ?? 0)} دقیقه بیدار بماند؟ فقط ثبت در ویندوز گزارش می‌شود.`}
            </p>
            {approval.needsSecret && (
              <input type="password" value={secret} onChange={(event) => setSecret(event.target.value)}
                     placeholder="رمز تأیید" maxLength={256} autoComplete="off" />
            )}
            <button type="button" disabled={approval.needsSecret && !secret} onClick={() => void answer(true)}>تأیید</button>
            <button type="button" onClick={() => void answer(false)}>لغو</button>
          </div>
        )}
      </section>

      {unsupported.length > 0 && (
        <div className="device-hub-unsupported">
          <h4>هنوز پشتیبانی نمی‌شود</h4>
          <ul>{unsupported.map((item) => <li key={item.kind}>{item.detail}</li>)}</ul>
        </div>
      )}
    </section>
  );
}

export default DeviceHubPanel;
