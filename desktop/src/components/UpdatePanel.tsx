import { useEffect, useState } from "react";

import {
  LAUNCH_TOKEN_KEPT, launchUpdate, refusalText, shortDigest, UpdateOutcomeUnknown, UpdateRefused,
} from "../updateStore";
import type { UpdateCandidate, UpdateClient, UpdateOffer, UpdateStatus } from "../updateStore";

type UpdatePanelProps = {
  client: UpdateClient;
  visible: boolean;
  /** Native dialogs (Control): one installer + its manifest, or a folder; null when cancelled. */
  pickPackage: () => Promise<{ installer: string; manifest: string } | null>;
  pickFolder: () => Promise<string | null>;
  /** Really stops the runtime bridge and reports whether it stopped (Control/Runtime). */
  stopRuntime: () => Promise<boolean>;
  restartRuntime: () => Promise<void>;
  exitApp: () => Promise<void>;
};

const KIND_TEXT: Record<UpdateOffer["kind"], string> = {
  upgrade: "ارتقا",
  repair: "تعمیر (نصب دوبارهٔ همین نسخه)",
  rollback: "بازگشت به نسخهٔ قدیمی‌تر",
};

// Install a signed Qronos update from a file the person chose. Nothing is
// downloaded. The approval secret lives only in this component's transient
// state and is cleared on submit, on cancel and whenever the panel is hidden;
// tokens are kept in memory only and dropped the same way.
function UpdatePanel(props: UpdatePanelProps) {
  const { client, visible } = props;
  const [status, setStatus] = useState<UpdateStatus | null>(null);
  const [offer, setOffer] = useState<UpdateOffer | null>(null);
  const [approvalToken, setApprovalToken] = useState<string | null>(null);
  const [launchToken, setLaunchToken] = useState<string | null>(null);
  const [secret, setSecret] = useState("");
  const [candidates, setCandidates] = useState<UpdateCandidate[]>([]);
  // Set once a launch outcome is unknown: nothing more is started from this panel.
  const [outcomeUnknown, setOutcomeUnknown] = useState(false);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<string | null>(null);

  const reset = () => {
    setSecret("");
    setOffer(null);
    setApprovalToken(null);
    setLaunchToken(null);
    setCandidates([]);
  };

  const refresh = async () => {
    try {
      setStatus(await client.status());
    } catch (error) {
      setMessage(error instanceof UpdateRefused ? error.message : refusalText("unknown"));
    }
  };

  useEffect(() => {
    if (visible) {
      void refresh();
    } else {
      reset();
      setMessage(null);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [visible]);

  const run = async (step: () => Promise<void>) => {
    setBusy(true);
    setMessage(null);
    try {
      await step();
    } catch (error) {
      setMessage(error instanceof UpdateRefused ? error.message : refusalText("unknown"));
    } finally {
      setBusy(false);
      void refresh();
    }
  };

  const check = (chosen: { installer: string; manifest: string }) => run(async () => {
    reset();
    const answer = await client.request(chosen.installer, chosen.manifest);
    setOffer(answer.offer);
    setApprovalToken(answer.approvalToken);
  });

  const chooseFile = () => run(async () => {
    const chosen = await props.pickPackage();
    if (chosen) {
      await check(chosen);
    }
  });

  const chooseFolder = () => run(async () => {
    const folder = await props.pickFolder();
    if (folder) {
      setCandidates(await client.discover(folder));
    }
  });

  const approve = () => run(async () => {
    if (!approvalToken || !secret) return;
    const typed = secret;
    setSecret("");
    const answer = await client.approve(approvalToken, typed);
    setApprovalToken(null);
    setLaunchToken(answer.launchToken);
  });

  const launch = () => run(async () => {
    if (!launchToken) return;
    const token = launchToken;
    setLaunchToken(null);
    try {
      await launchUpdate(client, token, props);
    } catch (error) {
      if (error instanceof UpdateOutcomeUnknown) {
        setOutcomeUnknown(true);
      } else if (error instanceof UpdateRefused && LAUNCH_TOKEN_KEPT.has(error.code)) {
        setLaunchToken(token); // refused before the single-use token was spent: still usable
      }
      throw error;
    }
  });

  const discard = () => run(async () => {
    reset();
    await client.discard();
  });

  // An unconfirmed repair: discard it, then check the package again (a new request, approval and launch).
  const retry = () => run(async () => {
    reset();
    await client.retry();
    const chosen = await props.pickPackage();
    if (chosen) {
      await check(chosen);
    }
  });

  const trust = status?.trust;

  return (
    <section className="update-panel" dir="rtl" aria-label="به‌روزرسانی Qronos">
      <h3>به‌روزرسانی از فایل</h3>
      <p>نسخهٔ فعلی: {status?.runningVersion ?? "—"}</p>
      {trust && !trust.configured && (
        <p role="status">
          هیچ کلید امضای مالک تنظیم نشده است؛ تا وقتی تنظیم نشود هیچ بسته‌ای پذیرفته نمی‌شود.
        </p>
      )}
      {status?.outcome && !status.unconfirmed && (
        <p role="status">
          {status.outcome.result === "applied"
            ? `به‌روزرسانی ${status.outcome.version} نصب شد.`
            : status.outcome.result === "unconfirmed"
              ? `معلوم نیست تعمیر ${status.outcome.version} انجام شده باشد (همین نسخه در حال اجراست). بستهٔ آماده نگه داشته شده است؛ می‌توانید دوباره تلاش کنید یا آن را کنار بگذارید.`
              : `به‌روزرسانی ${status.outcome.version} نصب نشد؛ می‌توانید دوباره تلاش کنید یا آن را کنار بگذارید.`}
          {status.outcome.dataRestored && " داده‌های پیش از این بازگشت به حالت قبل برگردانده شد."}
        </p>
      )}
      {status?.updateInProgress && (
        <p role="status">یک به‌روزرسانی هم‌اکنون در حال اجراست؛ تا پایان آن کاری انجام نمی‌شود.</p>
      )}
      {status?.recoveryPending && !status.updateInProgress && (
        <p role="alert">یک به‌روزرسانی نیمه‌کاره هنگام شروع دوبارهٔ Qronos بازیابی می‌شود.</p>
      )}
      {status?.unconfirmed && !status.updateInProgress && (
        <div role="group" aria-label="تعمیر تأییدنشده">
          <p role="status">
            {status.unconfirmed.receipt === "invalid"
              ? `رسید نصب تعمیر ${status.unconfirmed.version} معتبر نبود؛ انجام شدن آن تأیید نمی‌شود.`
              : status.unconfirmed.receipt === "not_requested"
                ? `تعمیر ${status.unconfirmed.version} پیش از آن آغاز شد که Qronos رسید نصب درخواست کند؛ `
                  + "انجام شدن آن تأیید نمی‌شود."
                : `برای تعمیر ${status.unconfirmed.version} هیچ رسید نصبی نرسید؛ انجام شدن آن تأیید نمی‌شود.`}
            {" "}شروع دوبارهٔ Qronos این وضعیت را تغییر نمی‌دهد. می‌توانید آن را کنار بگذارید یا دوباره تلاش کنید
            (فایل نصب‌کننده دوباره بررسی و تأیید می‌شود).
          </p>
          <button type="button" onClick={retry} disabled={busy}>تلاش دوباره…</button>
        </div>
      )}
      {outcomeUnknown && (
        <p role="alert">
          نتیجهٔ اجرای نصب‌کننده معلوم نیست. Qronos دوباره راه‌اندازی نمی‌شود؛ اگر نصب‌کننده باز است آن را تمام کنید و
          سپس Qronos را باز کنید.
        </p>
      )}
      <div className="update-actions">
        <button type="button" onClick={chooseFile} disabled={busy}>انتخاب فایل نصب‌کننده…</button>
        <button type="button" onClick={chooseFolder} disabled={busy}>جستجو در یک پوشه…</button>
        {status && status.phase !== "idle" && (
          <button type="button" onClick={discard} disabled={busy}>کنار گذاشتن به‌روزرسانی آماده</button>
        )}
      </div>
      {candidates.length > 0 && (
        <ul className="update-candidates">
          {candidates.map((item) => (
            <li key={item.installer}>
              <button type="button" onClick={() => check(item)} disabled={busy}>
                {item.installer.split(/[\\/]/).pop()}
              </button>
            </li>
          ))}
        </ul>
      )}
      {offer && (
        <div className="update-offer" role="group" aria-label="تأیید به‌روزرسانی">
          <p>{KIND_TEXT[offer.kind]}: از {offer.fromVersion} به {offer.version}</p>
          <p>امضاکننده: {offer.owner} (کلید {offer.keyId.slice(0, 8)})</p>
          <p dir="ltr">SHA-256: {shortDigest(offer.packageSha256)}…</p>
          {offer.changedStores.length > 0 && (
            <p>قالب این داده‌ها تغییر می‌کند و پیش از نصب نسخهٔ پشتیبان گرفته می‌شود: {offer.changedStores.join("، ")}</p>
          )}
          {offer.restoreSnapshot && (
            <p role="alert">
              این بازگشت داده‌های ذخیره‌شده پیش از ارتقا را برمی‌گرداند. تغییرات بعد از آن ارتقا در همان داده‌ها
              کنار گذاشته می‌شود؛ وضعیت فعلی پیش از آن جداگانه نگه داشته می‌شود.
            </p>
          )}
          {offer.notes && <p className="update-notes">{offer.notes}</p>}
          {approvalToken && (
            <label>
              رمز تأیید
              <input
                type="password"
                autoComplete="off"
                value={secret}
                onChange={(event) => setSecret(event.target.value)}
                disabled={busy}
              />
            </label>
          )}
          {approvalToken && (
            <button type="button" onClick={approve} disabled={busy || !secret}>تأیید و آماده‌سازی</button>
          )}
          {launchToken && status && !status.launchAvailable && (
            <p role="status">اجرای نصب‌کننده هنوز فعال نشده است؛ بستهٔ تأییدشده آماده باقی می‌ماند.</p>
          )}
          {launchToken && (
            <button type="button" onClick={launch}
              disabled={busy || outcomeUnknown || !status?.launchAvailable || status.updateInProgress}>
              بستن Qronos و اجرای نصب‌کننده
            </button>
          )}
        </div>
      )}
      {message && <p role="alert">{message}</p>}
    </section>
  );
}

export default UpdatePanel;
