import { useEffect, useState } from "react";

import { invoke } from "@tauri-apps/api/core";
import { listen } from "@tauri-apps/api/event";

import {
  approvalOf, createDeviceHubClient, devicesOf, jobsOf, printProblem, refusalText, unsupportedClassesOf,
} from "../deviceHubStore";
import type {
  Device, DeviceApproval, DeviceHubClient, DeviceHubTransport, DeviceJob, JobCommand, PrintColor, RuntimeEvent,
  UnsupportedClass,
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
    setMessage(result.ok ? (approved ? "انجام شد." : "لغو شد؛ چیزی تغییر نکرد.") : refusalText(result.code));
    if (device) await loadJobs(device.id);
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
              {item.unsupported && <span className="device-unsupported">پشتیبانی‌نشده: {item.unsupported}</span>}
            </button>
          </li>
        ))}
      </ul>

      {device && (
        <div className="device-detail">
          <h3>{device.name}</h3>
          {device.kind === "3d_printer" ? (
            <p className="device-unsupported">چاپ سه‌بعدی هنوز گردش کار محلی ندارد؛ این دستگاه فقط شناسایی شده است.</p>
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

          {approval && (
            <div className="device-approval" role="dialog" aria-label="تأیید تغییر کار چاپ">
              <p>
                {COMMAND_FA[(approval.preview.command as JobCommand) ?? "pause"] ?? ""} «{String(approval.preview.document ?? "")}»
                در «{String(approval.preview.device ?? "")}»؟
              </p>
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
        </div>
      )}

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
