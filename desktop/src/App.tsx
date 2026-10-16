
import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  useSyncExternalStore,
} from "react";

import type {
  CSSProperties,
} from "react";

import {
  getCurrentWindow,
} from "@tauri-apps/api/window";
import { invoke } from "@tauri-apps/api/core";
import { listen } from "@tauri-apps/api/event";
import {
  QronosVoicePlayer,
} from "./audio/QronosVoicePlayer";
import {
  sendConversationMessage,
  useConversationStore,
} from "./conversationStore";
import { useLibraryStore } from "./libraryStore";
import {
  WorkflowApprovalController,
  applyWorkflowEvent,
  createWorkflowApprovalController,
} from "./appWorkflowApproval";
import {
  ScreenApprovalController,
  applyScreenApprovalEvent,
  createScreenApprovalController,
  startScreenApprovalRuntimeWatch,
} from "./screenApproval";
import {
  refreshSettings,
  setSetting,
  useSettingsStore,
} from "./settingsStore";
import {
  DeviceLinkController,
  applyDeviceLinkEvent,
  bindPairingDelivery,
  createDeviceLinkController,
} from "./deviceLink";

import "./App.css";

import ConversationSpine from "./components/ConversationSpine";
import ConversationsView from "./components/ConversationsView";
import LibraryView from "./components/LibraryView";
import PermissionsView from "./components/PermissionsView";
import SettingsView from "./components/SettingsView";
import SetupStatus from "./components/SetupStatus";
import VoiceTurnNotice, { type VoiceNoticeState } from "./components/VoiceTurnNotice";
import RuntimeConnectionStatus from "./components/RuntimeConnectionStatus";
import {
  createRuntimeConnection,
  RUNTIME_CONNECTION_INITIAL,
  type RuntimeConnection,
  type RuntimeConnectionView,
} from "./runtimeConnection";
import { refreshSetup, setupNeedsAttention, useSetupStore } from "./setupStore";
import CrossViewParticleTransition from "./components/CrossViewParticleTransition";
import RightTelemetryPanel from "./components/RightTelemetryPanel";
import QueueDrawer from "./components/QueueDrawer";
import HistoryDrawer from "./components/HistoryDrawer";
import MediaDrawer from "./components/MediaDrawer";
import DeviceHubDrawer from "./components/DeviceHubDrawer";
import ScreenApprovalDialog from "./components/ScreenApprovalDialog";
import AppWorkflowDialog from "./components/AppWorkflowDialog";
import QronosOrb from "./components/QronosOrb";
import OrbTaskRenderer from "./components/OrbTaskRenderer";
import type { OrbState } from "./components/OrbState";

import "./components/QronosResponsive.css";

type DebugScenario =
  | "idle"
  | "listening"
  | "thinking"
  | "responding"
  | "chat"
  | "userVoice"
  | "qronosVoice";


type ViewPhase =
  | "home"
  | "entering-conversations"
  | "conversations"
  | "leaving-conversations"
  | "entering-library"
  | "library"
  | "leaving-library"
  | "entering-permissions"
  | "permissions"
  | "leaving-permissions"
  | "entering-settings"
  | "settings"
  | "leaving-settings";

type HotkeyBinding = {
  actionId: string;
  accelerator: string | null;
  scope: "global" | "inApp";
  enabled: boolean;
};

type HotkeySettings = {
  bindings: HotkeyBinding[];
};

type FileActionApproval = {
  actionToken: string;
  actionId: string;
  requestId: string;
  permission: "require_ui_confirmation";
  operation:
    | "copy_file"
    | "move_file"
    | "rename_file"
    | "create_folder";
  source: string;
  destination: string;
  newName: string;
};

function fileActionLabel(
  operation: FileActionApproval["operation"],
) {
  switch (operation) {
    case "copy_file":
      return "کپی فایل";
    case "move_file":
      return "انتقال فایل";
    case "rename_file":
      return "تغییر نام فایل";
    case "create_folder":
      return "ساخت پوشه";
  }
}

function eventMatchesShortcut(event: KeyboardEvent, accelerator: string) {
  const parts = accelerator.toLowerCase().split("+").map((part) => part.trim());
  const keyPart = parts.find((part) => !["ctrl", "control", "alt", "shift", "super", "meta", "command", "commandorcontrol"].includes(part));
  const eventKey = event.key === " " ? "space" : event.key.toLowerCase();
  return Boolean(keyPart)
    && eventKey === keyPart
    && event.ctrlKey === parts.some((part) => ["ctrl", "control", "commandorcontrol"].includes(part))
    && event.altKey === parts.includes("alt")
    && event.shiftKey === parts.includes("shift")
    && event.metaKey === parts.some((part) => ["super", "meta", "command"].includes(part));
}


const orbStateByScenario: Record<
  DebugScenario,
  OrbState
> = {
  idle: "idle",
  listening: "listening",
  thinking: "thinking",
  responding: "responding",
  chat: "thinking",
  userVoice: "listening",
  qronosVoice: "responding",
};

const statusByScenario: Record<
  DebugScenario,
  string
> = {
  idle: "آماده شنیدن",
  listening: "گوش می‌دهم",
  thinking: "در حال پردازش",
  responding: "پاسخ آماده است",
  chat: "در حال گفتگو",
  userVoice:
    "در حال شنیدن صدای شما",
  qronosVoice:
    "کرونوس در حال صحبت است",
};

const subtitleByScenario: Record<
  DebugScenario,
  string
> = {
  idle: "QRONOS CORE ONLINE",
  listening: "VOICE GATE OPEN",
  thinking: "COGNITIVE FLOW ACTIVE",
  responding:
    "RESPONSE CHANNEL READY",
  chat: "TEXT CHANNEL ACTIVE",
  userVoice:
    "USER VOICE VISUAL MODE",
  qronosVoice:
    "QRONOS VOICE VISUAL MODE",
};

function App() {
  const {
    pendingRequestId,
  } =
    useConversationStore();

  const { unreadCount: libraryUnreadCount } = useLibraryStore();
  const settingsState = useSettingsStore();
  const setupState = useSetupStore();
  const [conversationFocusMessageId, setConversationFocusMessageId] = useState<string | null>(null);

  const [
    homeDraft,
    setHomeDraft,
  ] =
    useState("");

  const homeComposerRef =
    useRef<HTMLTextAreaElement | null>(
      null,
    );

  const [
    scenario,
    setScenario,
  ] =
    useState<DebugScenario>(
      "idle",
    );

  const [
    runtimePaused,
    setRuntimePaused,
  ] = useState(false);

  const [
    pendingFileApproval,
    setPendingFileApproval,
  ] = useState<FileActionApproval | null>(
    null,
  );

  // Screen/Paint approvals: one nonblocking app-native dialog at a time,
  // answered only with each prompt's single-use backend approvalId. Runtime
  // events keep flowing while it is shown, so a revocation dismisses the
  // matching prompt immediately and sends nothing.
  const screenApprovalRef =
    useRef<ScreenApprovalController | null>(null);

  if (screenApprovalRef.current === null) {
    screenApprovalRef.current = createScreenApprovalController(
      invoke,
      (error) => {
        console.error(
          "[Qronos Screen Approval] decision not confirmed:",
          error,
        );
      },
    );
  }

  const screenApproval = screenApprovalRef.current;

  const screenApprovalView = useSyncExternalStore(
    screenApproval.subscribe,
    screenApproval.getSnapshot,
  );

  useEffect(() => {
    // While a prompt is visible, notice a runtime that ended without an
    // event (read-only status checks; never starts the runtime).
    const watch = startScreenApprovalRuntimeWatch(
      screenApproval,
      invoke,
    );

    return () => {
      // Unmount: stop checking, ignore late results, and drop every
      // displayed/waiting prompt without answering.
      watch.dispose();
      screenApproval.clear();
    };
  }, [screenApproval]);

  const answerScreenApproval = useCallback(
    (approvalId: string, approved: boolean) => {
      void screenApproval.answer(approvalId, approved);
    },
    [screenApproval],
  );

  // Telegram / print / browser workflow steps: one app-native prompt at a
  // time, answered only with the step's single-use backend actionToken.
  const workflowApprovalRef =
    useRef<WorkflowApprovalController | null>(null);

  if (workflowApprovalRef.current === null) {
    workflowApprovalRef.current = createWorkflowApprovalController(
      invoke,
      (error) => {
        console.error(
          "[Qronos Workflow Approval] decision not confirmed:",
          error,
        );
      },
    );
  }

  const workflowApproval = workflowApprovalRef.current;

  // The local runtime's connection state (connecting / ready / failed / degraded) and its one safe retry.
  const [runtimeConnection, setRuntimeConnection] =
    useState<RuntimeConnectionView>(RUNTIME_CONNECTION_INITIAL);
  const runtimeConnectionRef = useRef<RuntimeConnection | null>(null);
  const retryRuntimeConnection = useCallback(() => {
    runtimeConnectionRef.current?.retry();
  }, []);

  // A failed voice turn (voice_turn_failed): its own notice, separate from the connection state. Retry is one
  // explicit new push-to-talk turn per click: while it is pending the button is disabled, a rejected attempt stays
  // visible as failed with its retry, a definite refusal (runtime_busy or runtime_paused_rejected) shows as refused
  // with its retry, and only voice_listening (the turn really started) closes the notice. retryVoiceTurn is defined
  // after requestPushToTalk below.
  const [voiceNotice, setVoiceNotice] = useState<VoiceNoticeState | null>(null);
  const voiceRetryPendingRef = useRef(false);
  const settleVoiceNotice = useCallback((next: VoiceNoticeState | null) => {
    voiceRetryPendingRef.current = false;
    setVoiceNotice(next);
  }, []);

  // The Device Link page: one controller, fed by link_* runtime events.
  const deviceLinkRef = useRef<DeviceLinkController | null>(null);

  if (deviceLinkRef.current === null) {
    deviceLinkRef.current = createDeviceLinkController(
      invoke,
      async (enabled) => {
        await setSetting("link.enabled", enabled);
        const status = await invoke<{ running: boolean }>("get_runtime_status");
        if (status.running) {
          await invoke("send_runtime_action", { actionId: "qronos.settings_reload" });
        }
      },
      (error) => {
        console.error("[Qronos Device Link] request not confirmed:", error);
      },
      undefined, // random: the controller's default
      undefined, // clock: the controller's default
      // The global "access from outside" opt-in, through the same local settings save and reload.
      async (enabled) => {
        await setSetting("link.remote_access_enabled", enabled);
        const status = await invoke<{ running: boolean }>("get_runtime_status");
        if (status.running) {
          await invoke("send_runtime_action", { actionId: "qronos.settings_reload" });
        }
      },
    );
  }

  const deviceLink = deviceLinkRef.current;

  useEffect(() => {
    // Pairing replies come only to this window, on their own event.
    let unlisten: (() => void) | null = null;
    let disposed = false;
    void bindPairingDelivery(listen, deviceLink).then((stop) => {
      if (disposed) stop();
      else unlisten = stop;
    });
    return () => {
      disposed = true;
      unlisten?.();
    };
  }, [deviceLink]);

  const workflowApprovalView = useSyncExternalStore(
    workflowApproval.subscribe,
    workflowApproval.getSnapshot,
  );

  useEffect(() => {
    // Unmount drops every prompt without answering.
    return () => workflowApproval.clear();
  }, [workflowApproval]);

  const answerWorkflowApproval = useCallback(
    (actionToken: string, approved: boolean, secret?: string) => {
      void workflowApproval.answer(actionToken, approved, secret);
    },
    [workflowApproval],
  );

  const openWorkflowRecovery = useCallback(() => {
    void workflowApproval.openRecovery();
  }, [workflowApproval]);

  const acknowledgeWorkflowAttempt = useCallback(
    (operationKey: string) => {
      void workflowApproval.acknowledge(operationKey);
    },
    [workflowApproval],
  );

  const [
    fileApprovalSubmitting,
    setFileApprovalSubmitting,
  ] = useState(false);

  const [
    audioFrame,
    setAudioFrame,
  ] = useState<{
    level: number;
    bands: number[];
  }>({
    level: 0,
    bands: Array.from(
      { length: 32 },
      () => 0,
    ),
  });

  const audioFrameLastCommitRef =
    useRef(0);

  const [
    uiScale,
    setUiScale,
  ] =
    useState(1);

  const userScalePercent =
    typeof settingsState.values["appearance.ui_scale_percent"] === "number"
      ? Math.max(
          90,
          Math.min(
            110,
            Math.round(
              settingsState.values["appearance.ui_scale_percent"] as number,
            ),
          ),
        )
      : 100;

  const keepInTray =
    settingsState.values["system.keep_in_tray"] === true;

  const outputDeviceId =
    typeof settingsState.values["voice.output_device_id"] === "string"
      ? (settingsState.values["voice.output_device_id"] as string)
      : "";

  const [
    viewPhase,
    setViewPhase,
  ] =
    useState<ViewPhase>(
      "home",
    );

  const [
    crossViewTarget,
    setCrossViewTarget,
  ] =
    useState<
      | "conversations"
      | "library"
      | null
    >(null);

  const [
    permissionsExitTarget,
    setPermissionsExitTarget,
  ] =
    useState<
      | "conversations"
      | "library"
      | null
    >(null);

  const [
    permissionsEntrySource,
    setPermissionsEntrySource,
  ] =
    useState<
      | "conversations"
      | "library"
      | null
    >(null);

  const [
    settingsExitTarget,
    setSettingsExitTarget,
  ] = useState<
    | "conversations"
    | "library"
    | "permissions"
    | null
  >(null);

  const [
    settingsEntrySource,
    setSettingsEntrySource,
  ] = useState<
    | "conversations"
    | "library"
    | "permissions"
    | null
  >(null);

  const viewTransitionTimerRef =
    useRef<number | null>(
      null,
    );

  const clearViewTransitionTimer =
    () => {
      if (
        viewTransitionTimerRef.current ===
        null
      ) {
        return;
      }

      window.clearTimeout(
        viewTransitionTimerRef.current,
      );

      viewTransitionTimerRef.current =
        null;
    };

  const openConversations =
    () => {
      if (
        viewPhase ===
          "conversations" ||
        viewPhase ===
          "entering-conversations"
      ) {
        return;
      }

      clearViewTransitionTimer();

      if (
        viewPhase === "settings" ||
        viewPhase === "entering-settings" ||
        viewPhase === "leaving-settings"
      ) {
        setCrossViewTarget(null);
        setPermissionsExitTarget(null);
        setPermissionsEntrySource(null);
        setSettingsEntrySource(null);
        setSettingsExitTarget("conversations");
        setViewPhase("entering-conversations");
        viewTransitionTimerRef.current = window.setTimeout(() => {
          setViewPhase("conversations");
          setSettingsExitTarget(null);
          viewTransitionTimerRef.current = null;
        }, 620);
        return;
      }

      if (
        viewPhase ===
          "permissions" ||
        viewPhase ===
          "entering-permissions"
      ) {
        setCrossViewTarget(
          null,
        );

        setPermissionsEntrySource(
          null,
        );

        setPermissionsExitTarget(
          "conversations",
        );

        setViewPhase(
          "entering-conversations",
        );

        viewTransitionTimerRef.current =
          window.setTimeout(
            () => {
              setViewPhase(
                "conversations",
              );

              setPermissionsExitTarget(
                null,
              );

              viewTransitionTimerRef.current =
                null;
            },
            520,
          );

        return;
      }

      if (
        viewPhase ===
          "library" ||
        viewPhase ===
          "entering-library"
      ) {
        setCrossViewTarget(
          "conversations",
        );

        /*
         * Direct cross-fade:
         * source Library stays visually alive through a parent CSS override
         * while Conversations enters underneath/over it.
         */
        setViewPhase(
          "entering-conversations",
        );

        viewTransitionTimerRef.current =
          window.setTimeout(
            () => {
              setViewPhase(
                "conversations",
              );

              setCrossViewTarget(
                null,
              );

              viewTransitionTimerRef.current =
                null;
            },
            420,
          );

        return;
      }

      setCrossViewTarget(
        null,
      );

      setViewPhase(
        "entering-conversations",
      );

      viewTransitionTimerRef.current =
        window.setTimeout(
          () => {
            setViewPhase(
              "conversations",
            );

            viewTransitionTimerRef.current =
              null;
          },
          1120,
        );
    };

  const openLibrary =
    () => {
      if (
        viewPhase ===
          "library" ||
        viewPhase ===
          "entering-library"
      ) {
        return;
      }

      clearViewTransitionTimer();

      if (
        viewPhase === "settings" ||
        viewPhase === "entering-settings" ||
        viewPhase === "leaving-settings"
      ) {
        setCrossViewTarget(null);
        setPermissionsExitTarget(null);
        setPermissionsEntrySource(null);
        setSettingsEntrySource(null);
        setSettingsExitTarget("library");
        setViewPhase("entering-library");
        viewTransitionTimerRef.current = window.setTimeout(() => {
          setViewPhase("library");
          setSettingsExitTarget(null);
          viewTransitionTimerRef.current = null;
        }, 620);
        return;
      }

      if (
        viewPhase ===
          "permissions" ||
        viewPhase ===
          "entering-permissions"
      ) {
        setCrossViewTarget(
          null,
        );

        setPermissionsEntrySource(
          null,
        );

        setPermissionsExitTarget(
          "library",
        );

        setViewPhase(
          "entering-library",
        );

        viewTransitionTimerRef.current =
          window.setTimeout(
            () => {
              setViewPhase(
                "library",
              );

              setPermissionsExitTarget(
                null,
              );

              viewTransitionTimerRef.current =
                null;
            },
            520,
          );

        return;
      }

      if (
        viewPhase ===
          "conversations" ||
        viewPhase ===
          "entering-conversations"
      ) {
        setCrossViewTarget(
          "library",
        );

        /*
         * Direct cross-fade:
         * source Conversations stays visually alive through a parent CSS override
         * while Library enters underneath/over it.
         */
        setViewPhase(
          "entering-library",
        );

        viewTransitionTimerRef.current =
          window.setTimeout(
            () => {
              setViewPhase(
                "library",
              );

              setCrossViewTarget(
                null,
              );

              viewTransitionTimerRef.current =
                null;
            },
            420,
          );

        return;
      }

      setCrossViewTarget(
        null,
      );

      setViewPhase(
        "entering-library",
      );

      viewTransitionTimerRef.current =
        window.setTimeout(
          () => {
            setViewPhase(
              "library",
            );

            viewTransitionTimerRef.current =
              null;
          },
          1120,
        );
    };

  const openPermissions =
    () => {
      if (
        viewPhase ===
          "permissions" ||
        viewPhase ===
          "entering-permissions"
      ) {
        return;
      }

      clearViewTransitionTimer();

      if (
        viewPhase === "settings" ||
        viewPhase === "entering-settings" ||
        viewPhase === "leaving-settings"
      ) {
        setPermissionsExitTarget(null);
        setPermissionsEntrySource(null);
        setCrossViewTarget(null);
        setSettingsEntrySource(null);
        setSettingsExitTarget("permissions");
        setViewPhase("entering-permissions");
        viewTransitionTimerRef.current = window.setTimeout(() => {
          setViewPhase("permissions");
          setSettingsExitTarget(null);
          viewTransitionTimerRef.current = null;
        }, 620);
        return;
      }

      setPermissionsExitTarget(
        null,
      );

      setCrossViewTarget(
        null,
      );

      if (
        viewPhase ===
          "library" ||
        viewPhase ===
          "entering-library"
      ) {
        setPermissionsEntrySource(
          "library",
        );

        setViewPhase(
          "entering-permissions",
        );

        viewTransitionTimerRef.current =
          window.setTimeout(
            () => {
              setViewPhase(
                "permissions",
              );

              setPermissionsEntrySource(
                null,
              );

              viewTransitionTimerRef.current =
                null;
            },
            620,
          );

        return;
      }

      if (
        viewPhase ===
          "conversations" ||
        viewPhase ===
          "entering-conversations"
      ) {
        setPermissionsEntrySource(
          "conversations",
        );

        setViewPhase(
          "entering-permissions",
        );

        viewTransitionTimerRef.current =
          window.setTimeout(
            () => {
              setViewPhase(
                "permissions",
              );

              setPermissionsEntrySource(
                null,
              );

              viewTransitionTimerRef.current =
                null;
            },
            620,
          );

        return;
      }

      setPermissionsEntrySource(
        null,
      );

      setViewPhase(
        "entering-permissions",
      );

      viewTransitionTimerRef.current =
        window.setTimeout(
          () => {
            setViewPhase(
              "permissions",
            );

            viewTransitionTimerRef.current =
              null;
          },
          980,
        );
    };

  const openSettings =
    () => {
      if (
        viewPhase ===
          "settings" ||
        viewPhase ===
          "entering-settings"
      ) {
        return;
      }

      clearViewTransitionTimer();

      if (viewPhase !== "home") {
        setCrossViewTarget(null);
        setPermissionsExitTarget(null);
        setPermissionsEntrySource(null);
        setSettingsExitTarget(null);
        setSettingsEntrySource(
          viewPhase.includes("library")
            ? "library"
            : viewPhase.includes("permissions")
              ? "permissions"
              : "conversations",
        );
        setViewPhase("entering-settings");
        viewTransitionTimerRef.current = window.setTimeout(() => {
          setViewPhase("settings");
          setSettingsEntrySource(null);
          viewTransitionTimerRef.current = null;
        }, 820);
        return;
      }

      setCrossViewTarget(
        null,
      );

      setSettingsExitTarget(null);
      setSettingsEntrySource(null);

      setPermissionsExitTarget(
        null,
      );

      setPermissionsEntrySource(
        null,
      );

      setViewPhase(
        "entering-settings",
      );

      viewTransitionTimerRef.current =
        window.setTimeout(
          () => {
            setViewPhase(
              "settings",
            );

            viewTransitionTimerRef.current =
              null;
          },
          820,
        );
    };

  const returnHome =
    () => {
      if (
        viewPhase ===
          "home" ||
        viewPhase ===
          "leaving-conversations" ||
        viewPhase ===
          "leaving-library" ||
        viewPhase ===
          "leaving-permissions"
        || viewPhase ===
          "leaving-settings"
      ) {
        return;
      }

      clearViewTransitionTimer();

      setPermissionsExitTarget(
        null,
      );

      setPermissionsEntrySource(
        null,
      );

      setCrossViewTarget(
        null,
      );

      if (
        viewPhase ===
          "library" ||
        viewPhase ===
          "entering-library"
      ) {
        setViewPhase(
          "leaving-library",
        );
      } else if (
        viewPhase ===
          "permissions" ||
        viewPhase ===
          "entering-permissions"
      ) {
        setViewPhase(
          "leaving-permissions",
        );
      } else if (
        viewPhase ===
          "settings" ||
        viewPhase ===
          "entering-settings"
      ) {
        setViewPhase(
          "leaving-settings",
        );
      } else {
        setViewPhase(
          "leaving-conversations",
        );
      }

      viewTransitionTimerRef.current =
        window.setTimeout(
          () => {
            setViewPhase(
              "home",
            );

            viewTransitionTimerRef.current =
              null;
          },
          1120,
        );
    };

  useEffect(() => {
    let disposed = false;
    let inAppBindings: HotkeyBinding[] = [];
    const executeAction = (actionId: string) => {
      if (actionId === "navigation.home") returnHome();
      else if (actionId === "navigation.conversations") openConversations();
      else if (actionId === "navigation.library") openLibrary();
      else if (actionId === "navigation.permissions") openPermissions();
      else if (actionId === "navigation.settings") openSettings();
      else if (actionId === "qronos.focus_command") {
        document.querySelector<HTMLInputElement>(".command-input")?.focus();
      } else {
        window.dispatchEvent(new CustomEvent("qronos:hotkey-action", { detail: { actionId } }));
      }
    };
    const applySettings = (settings: HotkeySettings) => {
      inAppBindings = settings.bindings.filter((item) => item.scope === "inApp" && item.enabled && item.accelerator);
    };
    invoke<HotkeySettings>("get_hotkey_settings").then(applySettings).catch(() => undefined);
    const keyHandler = (event: KeyboardEvent) => {
      if (event.repeat) return;
      const target = event.target as HTMLElement | null;
      if (target?.closest(".settings-hotkey-capture")) return;
      const binding = inAppBindings.find((item) => item.accelerator && eventMatchesShortcut(event, item.accelerator));
      if (!binding) return;
      event.preventDefault();
      executeAction(binding.actionId);
    };
    window.addEventListener("keydown", keyHandler);
    const unlisteners = Promise.all([
      listen<{ actionId: string }>("qronos://hotkey", (event) => executeAction(event.payload.actionId)),
      listen<HotkeySettings>("qronos://hotkeys-updated", (event) => applySettings(event.payload)),
    ]);
    return () => {
      disposed = true;
      window.removeEventListener("keydown", keyHandler);
      void unlisteners.then((items) => { if (disposed) items.forEach((unlisten) => unlisten()); });
    };
  }, [viewPhase]);

  const voicePlaybackIdRef =
    useRef<number | null>(
      null,
    );

  // A response may contain several TTS WAV chunks. "ended" from the player
  // only means its queue is empty *right now*; it is not proof that the TTS
  // producer has finished emitting chunks. Runtime sends a separate
  // voice_audio_stream_complete event when the producer is genuinely closed.
  const voiceAudioStreamClosedRef =
    useRef(false);

  const voicePlayerDrainedRef =
    useRef(false);

  const voicePlaybackCompletionSentRef =
    useRef(false);

  const voicePlayerRef =
    useRef<QronosVoicePlayer | null>(
      null,
    );

  const acknowledgeVoicePlaybackIfComplete =
    useCallback(
      () => {
        const playbackId =
          voicePlaybackIdRef.current;

        if (
          playbackId === null ||
          !voiceAudioStreamClosedRef.current ||
          !voicePlayerDrainedRef.current ||
          voicePlaybackCompletionSentRef.current
        ) {
          return false;
        }

        voicePlaybackCompletionSentRef.current =
          true;

        void invoke(
          "send_runtime_action",
          {
            actionId:
              "qronos.voice_playback_complete",
            playbackId,
          },
        )
          .then(
            () => {
              if (
                voicePlaybackIdRef.current ===
                playbackId
              ) {
                voicePlaybackIdRef.current =
                  null;
                voiceAudioStreamClosedRef.current =
                  false;
                voicePlayerDrainedRef.current =
                  false;
                voicePlaybackCompletionSentRef.current =
                  false;
              }
            },
          )
          .catch(
            (error) => {
              voicePlaybackCompletionSentRef.current =
                false;

              console.error(
                "[Qronos Voice Playback] completion acknowledgement failed:",
                error,
              );
            },
          );

        return true;
      },
      [],
    );

  useEffect(() => {
    const player =
      new QronosVoicePlayer({
        onStateChange: (state) => {
          if (state === "playing") {
            voicePlayerDrainedRef.current =
              false;

            setScenario(
              "qronosVoice",
            );
            return;
          }

          if (
            state === "ended" ||
            state === "error"
          ) {
            // Temporary queue starvation between TTS chunks is allowed.
            // Do not tell Runtime that the whole response finished until the
            // producer has also emitted voice_audio_stream_complete.
            voicePlayerDrainedRef.current =
              true;

            const acknowledged =
              acknowledgeVoicePlaybackIfComplete();

            setAudioFrame({
              level: 0,
              bands: Array.from(
                { length: 32 },
                () => 0,
              ),
            });

            setScenario(
              acknowledged
                ? "idle"
                : "responding",
            );
            return;
          }

          if (state === "stopped") {
            voicePlaybackIdRef.current =
              null;
            voiceAudioStreamClosedRef.current =
              false;
            voicePlayerDrainedRef.current =
              false;
            voicePlaybackCompletionSentRef.current =
              false;

            setAudioFrame({
              level: 0,
              bands: Array.from(
                { length: 32 },
                () => 0,
              ),
            });

            setScenario(
              "idle",
            );
          }
        },
        onSpectrumFrame: (frame) => {
          setAudioFrame({
            level: frame.level,
            bands: frame.bands,
          });
        },
        onPlaybackStarted: (startedAtMs) => {
          const playbackId =
            voicePlaybackIdRef.current;

          console.info(
            "[Qronos Voice Diagnostics] playback_started",
            {
              performanceMs:
                Number(
                  startedAtMs.toFixed(3),
                ),
              playbackId,
            },
          );

          if (playbackId !== null) {
            void invoke(
              "send_runtime_action",
              {
                actionId:
                  "qronos.voice_playback_started",
                playbackId,
              },
            ).catch(
              (error) => {
                console.error(
                  "[Qronos Voice Playback] start acknowledgement failed:",
                  error,
                );
              },
            );
          }
        },
        onError: (error) => {
          console.error(
            "[Qronos Voice Playback]",
            error,
          );
        },
      });

    voicePlayerRef.current =
      player;

    return () => {
      player.dispose();

      if (
        voicePlayerRef.current ===
        player
      ) {
        voicePlayerRef.current =
          null;
      }
    };
  }, [
    acknowledgeVoicePlaybackIfComplete,
  ]);

  const stopCurrentResponse =
    useCallback(
      async () => {
        // Audio must stop before any backend bookkeeping. For native WASAPI
        // output, QronosVoicePlayer.stop() also sends native_audio_output_stop.
        voicePlayerRef.current?.stop();

        voicePlaybackIdRef.current =
          null;
        voiceAudioStreamClosedRef.current =
          false;
        voicePlayerDrainedRef.current =
          false;
        voicePlaybackCompletionSentRef.current =
          false;

        setAudioFrame({
          level: 0,
          bands: Array.from(
            { length: 32 },
            () => 0,
          ),
        });

        setScenario(
          "idle",
        );

        try {
          const status =
            await invoke<{
              running: boolean;
              status: string;
              message: string;
            }>(
              "get_runtime_status",
            );

          if (!status.running) {
            return;
          }

          await invoke(
            "send_runtime_action",
            {
              actionId:
                "qronos.stop_response",
            },
          );
        } catch (error) {
          console.error(
            "[Qronos Stop Response] runtime acknowledgement failed:",
            error,
          );
        }
      },
      [],
    );

  // One explicit push-to-talk request, shared by the hotkey and the voice notice's retry. Valid speech that is still
  // playing is stopped first, because the person asked to speak; the runtime is started only if it is not running.
  const requestPushToTalk = useCallback(async () => {
    if (voicePlayerRef.current?.isActive()) {
      await stopCurrentResponse();
    }
    const status = await invoke<{ running: boolean }>("get_runtime_status");
    if (!status.running) {
      await invoke("start_runtime");
    }
    await invoke("send_runtime_action", { actionId: "qronos.push_to_talk" });
  }, [stopCurrentResponse]);

  const retryVoiceTurn = useCallback(async () => {
    if (voiceRetryPendingRef.current) return;
    voiceRetryPendingRef.current = true;
    setVoiceNotice("retrying");
    try {
      await requestPushToTalk();
    } catch (error) {
      console.error("[Qronos Runtime] voice retry failed:", error);
      voiceRetryPendingRef.current = false;
      setVoiceNotice((current) => (current === "retrying" ? "retryFailed" : current));
    }
  }, [requestPushToTalk]);

  useEffect(() => {
    let disposed = false;
    let unlistenRuntime:
      | (() => void)
      | undefined;

    // Only the first check sends the existing idempotent Wake start to an already running runtime (a new runtime
    // starts Wake itself on runtime_ready). A retry only re-checks, starts or pings the runtime.
    let wakeOnRunning = true;
    const connection = createRuntimeConnection({
      probe: async () => {
        const status = await invoke<{ running: boolean }>(
          "get_runtime_status",
        );
        const firstCheck = wakeOnRunning;
        wakeOnRunning = false;

        if (!status.running) {
          await invoke("start_runtime");
          return "spawned";
        }

        if (firstCheck) {
          // A runtime that was already alive may have emitted runtime_ready before this effect subscribed.
          await invoke("send_runtime_action", {
            actionId: "qronos.wake_listener_start",
          }).catch((error) => {
            console.error(
              "[Qronos Wake Word] startup failed:",
              error,
            );
          });
        }
        return "running";
      },
      ping: async () => {
        await invoke("ping_runtime");
      },
      onChange: (view) => {
        if (!disposed) {
          setRuntimeConnection(view);
        }
      },
    });
    runtimeConnectionRef.current = connection;

    const connectRuntimeActions = async () => {
      type RuntimeEvent = {
        eventType: string;
        status: string;
        message: string;
      };

      unlistenRuntime =
        await listen<RuntimeEvent>(
          "qronos://runtime-event",
          (event) => {
            if (
              event.payload.eventType !==
                "voice_audio_spectrum" &&
              event.payload.eventType !==
                "microphone_input_level" &&
              // Holds a new phone's key: never logged.
              event.payload.eventType !==
                "link_pairing"
            ) {
              console.info(
                "[Qronos Runtime]",
                event.payload,
              );
            }

            // runtime_ready / runtime_pong / runtime_error / runtime_stopping update the visible connection state.
            connection.handleEvent(event.payload.eventType);

            // Screen/Paint prompts, their revocation and the end of the
            // runtime that asked them. Never blocks this handler.
            if (screenApprovalRef.current !== null) {
              applyScreenApprovalEvent(
                screenApprovalRef.current,
                event.payload.eventType,
                event.payload.message,
              );
            }

            // Device Link status, devices, pairing code and refusals.
            if (deviceLinkRef.current !== null) {
              applyDeviceLinkEvent(
                deviceLinkRef.current,
                event.payload.eventType,
                event.payload.message,
              );
            }

            // Telegram / print / browser workflow prompts and results.
            if (workflowApprovalRef.current !== null) {
              applyWorkflowEvent(
                workflowApprovalRef.current,
                event.payload.eventType,
                event.payload.message,
              );
            }

            switch (
              event.payload.eventType
            ) {
              case "voice_audio_spectrum": {
                try {
                  const parsed = JSON.parse(
                    event.payload.message,
                  ) as {
                    level?: unknown;
                    bands?: unknown;
                  };

                  const level =
                    typeof parsed.level === "number"
                      ? Math.max(
                          0,
                          Math.min(
                            1,
                            parsed.level,
                          ),
                        )
                      : 0;

                  const bands =
                    Array.isArray(parsed.bands)
                      ? parsed.bands
                          .slice(0, 32)
                          .map((value) =>
                            typeof value === "number"
                              ? Math.max(
                                  0,
                                  Math.min(
                                    1,
                                    value,
                                  ),
                                )
                              : 0,
                          )
                      : [];

                  while (bands.length < 32) {
                    bands.push(0);
                  }

                  const now =
                    performance.now();

                  if (
                    level > 0.001 &&
                    now -
                      audioFrameLastCommitRef.current <
                      120
                  ) {
                    break;
                  }

                  audioFrameLastCommitRef.current =
                    now;

                  setAudioFrame({
                    level,
                    bands,
                  });
                } catch {
                  setAudioFrame({
                    level: 0,
                    bands: Array.from(
                      { length: 32 },
                      () => 0,
                    ),
                  });
                }
                break;
              }

              case "wake_word_listening":
                setScenario(
                  "idle",
                );
                break;

              case "wake_word_detected":
                setScenario(
                  "listening",
                );
                break;

              case "runtime_busy":
              case "runtime_paused_rejected":
                // A definite refusal of the request. Only a pending voice-notice retry is affected: it shows as
                // refused with its retry again; nothing is retried or replayed on its own.
                if (voiceRetryPendingRef.current) {
                  settleVoiceNotice("retryRefused");
                }
                break;

              case "voice_turn_failed":
                // A voice turn ended in an error: say so, and re-read the local setup so a missing speech
                // service shows as incomplete setup. The diagnostic itself stays in runtime_error.
                settleVoiceNotice("failed");
                void refreshSetup();
                break;

              case "voice_listening":
                settleVoiceNotice(null);
                // Explicit Push-to-Talk already stops active speech before
                // asking Runtime to listen. A generic listening event must
                // never kill healthy playback on its own; doing that turns a
                // later TTS-chunk failure into an apparently random cutoff.
                if (
                  !voicePlayerRef.current
                    ?.isActive()
                ) {
                  setScenario(
                    "listening",
                  );
                }
                break;

              case "voice_transcribing":
                setAudioFrame({
                  level: 0,
                  bands: Array.from(
                    { length: 32 },
                    () => 0,
                  ),
                });
                setScenario(
                  "thinking",
                );
                break;

              case "voice_transcript":
              case "voice_routed":
                setScenario(
                  "thinking",
                );
                break;

              case "voice_response":
                setScenario(
                  "responding",
                );
                break;

              case "voice_audio_ready": {
                try {
                  const parsed = JSON.parse(
                    event.payload.message,
                  ) as {
                    path?: unknown;
                    playbackId?: unknown;
                  };

                  if (
                    typeof parsed.path !==
                      "string" ||
                    parsed.path.trim().length ===
                      0
                  ) {
                    throw new Error(
                      "voice_audio_ready did not contain a valid path.",
                    );
                  }

                  if (
                    typeof parsed.playbackId ===
                      "number" &&
                    Number.isInteger(
                      parsed.playbackId,
                    ) &&
                    parsed.playbackId > 0
                  ) {
                    if (
                      voicePlaybackIdRef.current !==
                        parsed.playbackId
                    ) {
                      voiceAudioStreamClosedRef.current =
                        false;
                      voicePlaybackCompletionSentRef.current =
                        false;
                    }

                    voicePlaybackIdRef.current =
                      parsed.playbackId;
                    voicePlayerDrainedRef.current =
                      false;
                  }

                  const player =
                    voicePlayerRef.current;

                  if (!player) {
                    throw new Error(
                      "Qronos voice player is not ready.",
                    );
                  }

                  void player
                    .playPath(
                      parsed.path,
                    )
                    .catch(
                      (error) => {
                        console.error(
                          "[Qronos Voice Playback] play failed:",
                          error,
                        );
                        setScenario(
                          "idle",
                        );
                      },
                    );
                } catch (error) {
                  console.error(
                    "[Qronos Voice Playback] invalid event:",
                    error,
                  );
                  setScenario(
                    "idle",
                  );
                }
                break;
              }

              case "voice_audio_stream_complete": {
                try {
                  const parsed = JSON.parse(
                    event.payload.message,
                  ) as {
                    playbackId?: unknown;
                    generatedChunkCount?: unknown;
                    plannedChunkCount?: unknown;
                    stoppedByUser?: unknown;
                    partialTtsError?: unknown;
                  };

                  if (
                    typeof parsed.playbackId !==
                      "number" ||
                    !Number.isInteger(
                      parsed.playbackId,
                    ) ||
                    parsed.playbackId <= 0
                  ) {
                    throw new Error(
                      "voice_audio_stream_complete did not contain a valid playbackId.",
                    );
                  }

                  if (
                    voicePlaybackIdRef.current !==
                      parsed.playbackId
                  ) {
                    // Explicit Stop may have already cleared this playback.
                    break;
                  }

                  voiceAudioStreamClosedRef.current =
                    true;

                  const player =
                    voicePlayerRef.current;

                  if (
                    !player ||
                    !player.isActive()
                  ) {
                    voicePlayerDrainedRef.current =
                      true;
                  }

                  console.info(
                    "[Qronos Voice Diagnostics] audio_stream_complete",
                    {
                      playbackId:
                        parsed.playbackId,
                      generatedChunkCount:
                        parsed.generatedChunkCount,
                      plannedChunkCount:
                        parsed.plannedChunkCount,
                      stoppedByUser:
                        parsed.stoppedByUser,
                      partialTtsError:
                        parsed.partialTtsError,
                      playerDrained:
                        voicePlayerDrainedRef.current,
                    },
                  );

                  acknowledgeVoicePlaybackIfComplete();
                } catch (error) {
                  console.error(
                    "[Qronos Voice Playback] invalid stream-complete event:",
                    error,
                  );
                }
                break;
              }

              case "voice_needs_screen":
                // The prompt itself was routed to the approval dialog above.
                setScenario("thinking");
                break;

              case "screen_approval_stale": {
                console.info(
                  "[Qronos Screen Approval] answer refused as stale:",
                  event.payload.message,
                );
                if (
                  !voicePlayerRef.current
                    ?.isActive()
                ) {
                  setScenario("idle");
                }
                break;
              }

              case "file_action_approval_required": {
                try {
                  const parsed = JSON.parse(
                    event.payload.message,
                  ) as {
                    actionToken?: unknown;
                    actionId?: unknown;
                    requestId?: unknown;
                    permission?: unknown;
                    operation?: unknown;
                    source?: unknown;
                    destination?: unknown;
                    newName?: unknown;
                  };

                  const actionToken =
                    typeof parsed.actionToken ===
                    "string"
                      ? parsed.actionToken.trim()
                      : "";
                  const actionId =
                    typeof parsed.actionId ===
                    "string"
                      ? parsed.actionId.trim()
                      : "";
                  const requestId =
                    typeof parsed.requestId ===
                    "string"
                      ? parsed.requestId.trim()
                      : "";
                  const permission =
                    parsed.permission;
                  const operation:
                    | FileActionApproval["operation"]
                    | null =
                    parsed.operation ===
                      "copy_file" ||
                    parsed.operation ===
                      "move_file" ||
                    parsed.operation ===
                      "rename_file" ||
                    parsed.operation ===
                      "create_folder"
                      ? parsed.operation
                      : null;

                  if (
                    !actionToken ||
                    !actionId ||
                    !requestId ||
                    permission !==
                      "require_ui_confirmation" ||
                    operation === null
                  ) {
                    throw new Error(
                      "Invalid file-action approval payload.",
                    );
                  }

                  setPendingFileApproval({
                    actionToken,
                    actionId,
                    requestId,
                    permission:
                      "require_ui_confirmation",
                    operation,
                    source:
                      typeof parsed.source ===
                      "string"
                        ? parsed.source
                        : "",
                    destination:
                      typeof parsed.destination ===
                      "string"
                        ? parsed.destination
                        : "",
                    newName:
                      typeof parsed.newName ===
                      "string"
                        ? parsed.newName
                        : "",
                  });
                  setFileApprovalSubmitting(
                    false,
                  );
                  setScenario(
                    "thinking",
                  );
                } catch (error) {
                  console.error(
                    "[Qronos File Approval] invalid event:",
                    error,
                  );
                }
                break;
              }

              case "file_action_completed":
              case "file_action_declined":
              case "file_action_refused":
              case "file_action_failed":
              // Stop/Pause/shutdown revoked the pending approval, or an
              // answer named one that is no longer usable: close the
              // dialog; the runtime will not write.
              case "file_action_cancelled":
              case "file_action_stale":
                setPendingFileApproval(
                  null,
                );
                setFileApprovalSubmitting(
                  false,
                );
                if (
                  !voicePlayerRef.current
                    ?.isActive()
                ) {
                  setScenario(
                    "idle",
                  );
                }
                break;

              case "runtime_error":
                // Runtime errors are diagnostics, not user Stop commands.
                // If valid audio is already playing, preserve it. Explicit
                // Stop, Push-to-Talk interruption, Pause, and disposal remain
                // the only paths allowed to terminate user-visible playback.
                console.error(
                  "[Qronos Runtime]",
                  event.payload.message,
                );

                if (
                  !voicePlayerRef.current
                    ?.isActive()
                ) {
                  setAudioFrame({
                    level: 0,
                    bands: Array.from(
                      { length: 32 },
                      () => 0,
                    ),
                  });
                  setScenario(
                    "idle",
                  );
                }
                break;

              case "runtime_ready":
                // A new Python runtime always starts unpaused. Global pause is
                // deliberately temporary and never survives runtime restart.
                setRuntimePaused(false);

                // start_runtime() only means that the Python process was
                // spawned. runtime_ready is the authoritative signal that
                // the bridge is ready to accept startup actions.
                void invoke(
                  "send_runtime_action",
                  {
                    actionId:
                      "qronos.wake_listener_start",
                  },
                ).catch(
                  (error) => {
                    console.error(
                      "[Qronos Wake Word] ready-start failed:",
                      error,
                    );
                  },
                );
                break;

              case "runtime_pause_changed": {
                try {
                  const parsed = JSON.parse(
                    event.payload.message,
                  ) as {
                    paused?: unknown;
                  };

                  const paused =
                    parsed.paused === true;

                  setRuntimePaused(
                    paused,
                  );

                  if (paused) {
                    voicePlayerRef.current?.stop();

                    setAudioFrame({
                      level: 0,
                      bands: Array.from(
                        { length: 32 },
                        () => 0,
                      ),
                    });

                    setScenario(
                      "idle",
                    );
                  }
                } catch {
                  // Ignore malformed pause telemetry; the control remains
                  // authoritative only when the runtime sends valid state.
                }
                break;
              }

              case "voice_turn_complete":
                if (
                  !voicePlayerRef.current
                    ?.isActive()
                ) {
                  window.setTimeout(
                    () => {
                      if (
                        !voicePlayerRef.current
                          ?.isActive()
                      ) {
                        setScenario(
                          "idle",
                        );
                      }
                    },
                    350,
                  );
                }
                break;

              default:
                break;
            }
          },
        );

      // Startup is visible: connecting until the bridge answers (runtime_ready or runtime_pong), failed with a
      // safe Persian message and a retry otherwise. A spawned process alone is not "ready".
      void connection.start();

      const handleRuntimeAction = async (
        event: Event,
      ) => {
        const customEvent =
          event as CustomEvent<{
            actionId?: string;
          }>;

        const actionId =
          customEvent.detail?.actionId?.trim();

        if (!actionId) {
          return;
        }

        if (
          actionId ===
          "qronos.stop_response"
        ) {
          await stopCurrentResponse();
          return;
        }

        if (
          actionId !==
          "qronos.push_to_talk"
        ) {
          return;
        }

        try {
          await requestPushToTalk();
        } catch (error) {
          console.error(
            "[Qronos Runtime] action failed:",
            error,
          );
        }
      };

      window.addEventListener(
        "qronos:hotkey-action",
        handleRuntimeAction,
      );

      return () => {
        window.removeEventListener(
          "qronos:hotkey-action",
          handleRuntimeAction,
        );
      };
    };

    let removeWindowListener:
      | (() => void)
      | undefined;

    void connectRuntimeActions()
      .then((cleanup) => {
        if (disposed) {
          cleanup();
          unlistenRuntime?.();
          return;
        }

        removeWindowListener = cleanup;
      });

    return () => {
      disposed = true;
      connection.dispose();
      if (runtimeConnectionRef.current === connection) {
        runtimeConnectionRef.current = null;
      }
      removeWindowListener?.();
      unlistenRuntime?.();
    };
  }, [
    stopCurrentResponse,
  ]);

  const answerFileActionApproval =
    useCallback(
      async (
        approved: boolean,
      ) => {
        if (
          !pendingFileApproval ||
          fileApprovalSubmitting
        ) {
          return;
        }

        setFileApprovalSubmitting(
          true,
        );

        try {
          await invoke(
            "send_runtime_action",
            {
              actionId:
                "qronos.file_action_decision",
              data: {
                actionToken:
                  pendingFileApproval.actionToken,
                approved,
              },
            },
          );
        } catch (error) {
          console.error(
            "[Qronos File Approval] decision failed:",
            error,
          );
          setFileApprovalSubmitting(
            false,
          );
        }
      },
      [
        pendingFileApproval,
        fileApprovalSubmitting,
      ],
    );

  const toggleFullscreen =
    async () => {
      try {
        const appWindow =
          getCurrentWindow();

        const isFullscreen =
          await appWindow.isFullscreen();

        await appWindow.setFullscreen(
          !isFullscreen,
        );
      } catch (error) {
        console.error(
          "Failed to toggle fullscreen:",
          error,
        );
      }
    };

  const orbState =
    useMemo(
      () =>
        orbStateByScenario[
          scenario
        ],
      [scenario],
    );

  const statusLabel =
    useMemo(
      () =>
        scenario === "idle" && (setupState.loading || setupState.error || !setupState.snapshot || setupNeedsAttention(setupState.snapshot))
          ? (setupState.loading ? "بررسی راه‌اندازی" : "راه‌اندازی را بررسی کنید")
          :
        statusByScenario[
          scenario
        ],
      [scenario, setupState],
    );

  const statusSubtitle =
    useMemo(
      () =>
        scenario === "idle" && (setupState.loading || setupState.error || !setupState.snapshot || setupNeedsAttention(setupState.snapshot))
          ? "LOCAL SETUP"
          :
        subtitleByScenario[
          scenario
        ],
      [scenario, setupState],
    );

  const setRuntimePause =
    useCallback(
      async (
        paused: boolean,
      ) => {
        try {
          await invoke(
            "runtime_set_paused",
            { paused },
          );

          /*
           * IPC accepted and flushed the command. Reflect the requested state
           * immediately so the same control can always Pause -> Active.
           * runtime_pause_changed remains authoritative and reconciles the
           * state after Python processes the command.
           *
           * Wake resume is intentionally not a second frontend command:
           * Python owns the full Pause/Resume lifecycle atomically.
           */
          setRuntimePaused(
            paused,
          );
        } catch (error) {
          console.error(
            "[Qronos Runtime Pause]",
            error,
          );
        }
      },
      [],
    );

  const sendHomeMessage =
    useCallback(
      async () => {
        const text =
          homeDraft.trim();

        if (
          !text ||
          pendingRequestId
        ) {
          return;
        }

        setHomeDraft("");

        if (
          homeComposerRef.current
        ) {
          homeComposerRef.current.style.height =
            "24px";
        }

        await sendConversationMessage(
          text,
        );
      },
      [
        homeDraft,
        pendingRequestId,
      ],
    );

  useEffect(() => {
    void refreshSettings().catch((error) => {
      console.error(
        "[Qronos Settings] startup refresh failed:",
        error,
      );
    });
  }, []);

  useEffect(() => {
    if (settingsState.loading) {
      return;
    }

    const player =
      voicePlayerRef.current;

    if (!player) {
      return;
    }

    void player
      .setOutputDeviceId(
        outputDeviceId,
      )
      .catch((error) => {
        console.error(
          "[Qronos Voice Output] selected device could not be applied; system default is active:",
          error,
        );
      });
  }, [
    outputDeviceId,
    settingsState.loading,
  ]);

  useEffect(() => {
    if (settingsState.loading) {
      return;
    }

    void invoke(
      "set_keep_in_tray",
      {
        enabled: keepInTray,
      },
    ).catch((error) => {
      console.error(
        "[Qronos System Tray] startup apply failed:",
        error,
      );
    });
  }, [
    keepInTray,
    settingsState.loading,
  ]);

  useEffect(() => {
    const updateScale =
      () => {
        /*
         * One shared desktop scale remains authoritative for every major
         * Qronos region. 1600 × 900 is still the approved 100% reference.
         *
         * The old implementation hard-capped at 1, which made 4K displays
         * physically tiny. Default 100% stays at 1 on ordinary 1080p-class
         * windows, then grows smoothly on genuinely high-resolution windows.
         */
        const widthScale =
          window.innerWidth /
          1600;

        const heightScale =
          window.innerHeight /
          900;

        const fitScale =
          Math.min(
            widthScale,
            heightScale,
          );

        const automaticScale =
          fitScale <= 1.25
            ? Math.min(
                1,
                fitScale,
              )
            : Math.min(
                2.15,
                1 +
                  (
                    fitScale -
                    1.25
                  ) *
                    0.9,
              );

        const preferredScale =
          automaticScale *
          (
            userScalePercent /
            100
          );

        const next =
          Math.max(
            0.62,
            Math.min(
              2.4,
              fitScale,
              preferredScale,
            ),
          );

        setUiScale(
          Number(
            next.toFixed(4),
          ),
        );
      };

    updateScale();

    window.addEventListener(
      "resize",
      updateScale,
    );

    return () => {
      window.removeEventListener(
        "resize",
        updateScale,
      );
    };
  }, [userScalePercent]);

  useEffect(() => {
    return () => {
      clearViewTransitionTimer();
    };
  }, []);

  useEffect(() => {
    const handleKeyDown = (
      event: KeyboardEvent,
    ) => {
      if (
        event.key ===
        "F11"
      ) {
        event.preventDefault();
        event.stopPropagation();

        void toggleFullscreen();
      }
    };

    window.addEventListener(
      "keydown",
      handleKeyDown,
    );

    return () => {
      window.removeEventListener(
        "keydown",
        handleKeyDown,
      );
    };
  }, []);


  return (
    <main
      className={`app app-view-${viewPhase} ${
        crossViewTarget
          ? `app-cross-to-${crossViewTarget}`
          : ""
      } ${
        permissionsExitTarget
          ? `app-permissions-direct-to-${permissionsExitTarget}`
          : ""
      } ${
        permissionsEntrySource
          ? `app-permissions-direct-from-${permissionsEntrySource}`
          : ""
      } ${
        settingsExitTarget
          ? `app-settings-direct-to-${settingsExitTarget}`
          : ""
      } ${
        settingsEntrySource
          ? `app-settings-direct-from-${settingsEntrySource}`
          : ""
      }`}
      dir="rtl"
      style={
        {
          "--qronos-ui-scale":
            uiScale,
        } as CSSProperties
      }
    >
      <div className="ambient ambient-cyan" />
      <div className="ambient ambient-violet" />
      <div className="ui-grid" />

      <header className="brand">
        <div className="brand-name">
          Q R O N O S
        </div>

        <div className="brand-beam">
          <div className="brand-beam-base" />
          <div className="brand-beam-runner" />
        </div>
      </header>


      <ConversationSpine />


      <RightTelemetryPanel />
      <SetupStatus />
      <RuntimeConnectionStatus
        view={runtimeConnection}
        onRetry={retryRuntimeConnection}
      />
      {voiceNotice && <VoiceTurnNotice state={voiceNotice} onRetry={() => { void retryVoiceTurn(); }}
        onDismiss={() => settleVoiceNotice(null)} />}

      {/* The Smart Queue: queued requests, their progress and outcomes,
          with hold/pause/resume/cancel. Its hold is the queue's own and is
          separate from the runtime-wide Pause owned by PermissionsView. */}
      <QueueDrawer
        hidden={
          viewPhase !== "home" &&
          viewPhase !== "conversations"
        }
        onShowConversation={(messageId) => {
          setConversationFocusMessageId(messageId);
          openConversations();
        }}
      />

      {/* What Qronos did, what was allowed, what failed, and what can actually be undone. */}
      <HistoryDrawer
        hidden={
          viewPhase !== "home" &&
          viewPhase !== "conversations"
        }
      />

      {/* The media library: chosen folders, background indexing, search with evidence cards, open at a time
          and reports, all through the runtime's one media host (qronos.media actions). */}
      <MediaDrawer
        hidden={
          viewPhase !== "home" &&
          viewPhase !== "conversations"
        }
      />

      {/* The Device Hub: printers Windows already has (local and LAN connections), their driver capabilities,
          the print queue with own-job pause/resume/cancel, and printing one exact file (qronos.device_hub). */}
      <DeviceHubDrawer
        hidden={
          viewPhase !== "home" &&
          viewPhase !== "conversations"
        }
      />

      <section className="core-zone">
        <div className="orb-shell">
          <QronosOrb
            size={460}
            state={orbState}
            audioLevel={audioFrame.level}
            audioSpectrum={audioFrame.bands}
          />

          <OrbTaskRenderer
            state={orbState}
          />
        </div>
      </section>

      <section className="orb-status">
        <div className="status-line status-line-left" />

        <div className="status-content">
          <span className="status-dot" />

          <span className="status-label">
            {statusLabel}
          </span>

          <span className="status-subtitle">
            {statusSubtitle}
          </span>
        </div>

        <div className="status-line status-line-right" />
      </section>

      <section className="command-dock">
        <div className="command-shell">
          <div className="command-glass-highlight" />

          <div className="command-stream">
            <span className="command-stream-dot dot-1" />
            <span className="command-stream-dot dot-2" />
            <span className="command-stream-dot dot-3" />
            <span className="command-stream-dot dot-4" />
            <span className="command-stream-dot dot-5" />
          </div>

          <button
            type="button"
            className="command-action command-action-mic"
            aria-label="میکروفون"
          >
            <svg
              viewBox="0 0 24 24"
              aria-hidden="true"
            >
              <path
                d="M12 3.4a3 3 0 0 0-3 3v5.1a3 3 0 0 0 6 0V6.4a3 3 0 0 0-3-3Z"
                fill="none"
                stroke="currentColor"
                strokeWidth="1.35"
              />

              <path
                d="M6.8 10.9v.7a5.2 5.2 0 0 0 10.4 0v-.7M12 16.8v3M9.3 19.8h5.4"
                fill="none"
                stroke="currentColor"
                strokeLinecap="round"
                strokeWidth="1.35"
              />
            </svg>
          </button>

          <textarea
            ref={homeComposerRef}
            className="command-input"
            rows={1}
            placeholder="از کرونوس بپرس..."
            aria-label="دستور کرونوس"
            value={homeDraft}
            onChange={(
              event,
            ) => {
              setHomeDraft(
                event.currentTarget.value,
              );
            }}
            onInput={(
              event,
            ) => {
              const element =
                event.currentTarget;

              element.style.height =
                "0px";

              const nextHeight =
                Math.min(
                  element.scrollHeight,
                  116,
                );

              element.style.height =
                `${Math.max(
                  24,
                  nextHeight,
                )}px`;
            }}
            onKeyDown={(
              event,
            ) => {
              if (
                event.key ===
                  "Enter" &&
                !event.shiftKey
              ) {
                event.preventDefault();
                void sendHomeMessage();
              }
            }}
          />

          {scenario ===
          "qronosVoice" ? (
            <button
              type="button"
              className="command-action command-action-send"
              aria-label="توقف پاسخ"
              title="توقف پاسخ · Ctrl+Alt+X"
              onClick={() => {
                void stopCurrentResponse();
              }}
            >
              <svg
                viewBox="0 0 24 24"
                aria-hidden="true"
              >
                <rect
                  x="7.2"
                  y="7.2"
                  width="9.6"
                  height="9.6"
                  rx="1.2"
                  fill="currentColor"
                />
              </svg>
            </button>
          ) : (
            <button
              type="button"
              className="command-action command-action-send"
              aria-label="ارسال"
              onClick={() => {
                void sendHomeMessage();
              }}
              disabled={
                !homeDraft.trim() ||
                Boolean(
                  pendingRequestId,
                )
              }
            >
              <svg
                viewBox="0 0 24 24"
                aria-hidden="true"
              >
                <path
                  d="m5 12.1 13.2-6.3-4.8 12.7-2.2-4.4L5 12.1Z"
                  fill="none"
                  stroke="currentColor"
                  strokeLinejoin="round"
                  strokeWidth="1.35"
                />

                <path
                  d="m11.2 14.1 7-8.3"
                  fill="none"
                  stroke="currentColor"
                  strokeLinecap="round"
                  strokeWidth="1.35"
                />
              </svg>
            </button>
          )}
        </div>

        <div className="command-hint">
          <span>
            صدا
          </span>

          <i />

          <span>
            متن
          </span>

          <i />

          <span>
            {scenario ===
            "qronosVoice"
              ? "Ctrl+Alt+X توقف"
              : "اجرای امن"}
          </span>
        </div>
      </section>

      <ScreenApprovalDialog
        view={screenApprovalView}
        onAnswer={answerScreenApproval}
      />

      <AppWorkflowDialog
        view={workflowApprovalView}
        onAnswer={answerWorkflowApproval}
        onDismissResult={workflowApproval.dismissResult}
        onOpenRecovery={openWorkflowRecovery}
        onAcknowledge={acknowledgeWorkflowAttempt}
        onCloseRecovery={workflowApproval.closeRecovery}
      />

      {pendingFileApproval && (
        <div
          className="file-approval-backdrop"
          role="presentation"
        >
          <section
            className="file-approval-dialog"
            role="dialog"
            aria-modal="true"
            aria-labelledby="file-approval-title"
          >
            <div
              className="file-approval-kicker"
            >
              FILE CONTROL · PERMISSION REQUIRED
            </div>

            <h2
              id="file-approval-title"
            >
              اجازه اجرای عملیات فایل؟
            </h2>

            <p
              className="file-approval-copy"
            >
              این تأیید، عملیات امن Copy / Move / Rename / Create Folder
              را برای ۱۰ دقیقه فعال می‌کند. بعد از ۱۰ دقیقه دوباره اجازه می‌پرسد.
            </p>

            <div
              className="file-approval-operation"
            >
              <span>
                عملیات
              </span>
              <strong>
                {fileActionLabel(
                  pendingFileApproval.operation,
                )}
              </strong>
            </div>

            {pendingFileApproval.source && (
              <div
                className="file-approval-path"
              >
                <span>
                  مبدأ
                </span>
                <code dir="ltr">
                  {
                    pendingFileApproval.source
                  }
                </code>
              </div>
            )}

            {pendingFileApproval.destination && (
              <div
                className="file-approval-path"
              >
                <span>
                  مقصد
                </span>
                <code dir="ltr">
                  {
                    pendingFileApproval.destination
                  }
                </code>
              </div>
            )}

            {pendingFileApproval.newName && (
              <div
                className="file-approval-path"
              >
                <span>
                  نام جدید
                </span>
                <code dir="ltr">
                  {
                    pendingFileApproval.newName
                  }
                </code>
              </div>
            )}

            <div
              className="file-approval-warning"
            >
              این مجوز فقط در همین اجرای Qronos و برای ۱۰ دقیقه است؛
              با Pause یا Restart باطل می‌شود. Delete شامل این مجوز نیست.
            </div>

            <div
              className="file-approval-actions"
            >
              <button
                type="button"
                className="file-approval-button file-approval-deny"
                disabled={
                  fileApprovalSubmitting
                }
                onClick={() => {
                  void answerFileActionApproval(
                    false,
                  );
                }}
              >
                رد کردن
              </button>

              <button
                type="button"
                className="file-approval-button file-approval-allow"
                disabled={
                  fileApprovalSubmitting
                }
                onClick={() => {
                  void answerFileActionApproval(
                    true,
                  );
                }}
              >
                {fileApprovalSubmitting
                  ? "در حال ارسال..."
                  : "تأیید برای ۱۰ دقیقه"}
              </button>
            </div>
          </section>
        </div>
      )}

      <CrossViewParticleTransition
        target={
          crossViewTarget
        }
      />

      <ConversationsView
        phase={
          settingsEntrySource === "conversations" &&
          viewPhase === "entering-settings"
            ? "conversations"
            : permissionsEntrySource ===
            "conversations" &&
          viewPhase ===
            "entering-permissions"
            ? "conversations"
            : viewPhase ===
                  "entering-library" ||
                viewPhase ===
                  "library" ||
                viewPhase ===
                  "leaving-library" ||
                viewPhase ===
                  "entering-permissions" ||
                viewPhase ===
                  "permissions" ||
                viewPhase ===
                  "leaving-permissions" ||
                viewPhase ===
                  "entering-settings" ||
                viewPhase ===
                  "settings" ||
                viewPhase ===
                  "leaving-settings"
              ? "home"
              : viewPhase
        }
        onClose={
          returnHome
        }
        focusMessageId={conversationFocusMessageId}
        onFocusConsumed={() => setConversationFocusMessageId(null)}
      />

      <LibraryView
        phase={
          settingsEntrySource === "library" &&
          viewPhase === "entering-settings"
            ? "library"
            : permissionsEntrySource ===
            "library" &&
          viewPhase ===
            "entering-permissions"
            ? "library"
            : viewPhase ===
                  "entering-permissions" ||
                viewPhase ===
                  "permissions" ||
                viewPhase ===
                  "leaving-permissions" ||
                viewPhase ===
                  "entering-settings" ||
                viewPhase ===
                  "settings" ||
                viewPhase ===
                  "leaving-settings"
              ? "home"
              : viewPhase
        }
        onClose={
          returnHome
        }
        onShowConversation={(messageId) => {
          setConversationFocusMessageId(messageId);
          openConversations();
        }}
      />

      <PermissionsView
        runtimePaused={
          runtimePaused
        }
        onToggleRuntimePaused={
          setRuntimePause
        }
        phase={
          settingsEntrySource === "permissions" &&
          viewPhase === "entering-settings"
            ? "permissions"
            : permissionsExitTarget
            ? "leaving-permissions"
            : viewPhase ===
                  "entering-settings" ||
                viewPhase ===
                  "settings" ||
                viewPhase ===
                  "leaving-settings"
              ? "home"
              : viewPhase
        }
        onClose={
          returnHome
        }
      />


      <SettingsView
        phase={
          settingsExitTarget
            ? "leaving-settings"
            : viewPhase ===
              "entering-settings" ||
            viewPhase ===
              "settings" ||
            viewPhase ===
              "leaving-settings"
            ? viewPhase
            : "home"
        }
        onClose={
          returnHome
        }
        onOpenPermissions={
          openPermissions
        }
        deviceLink={deviceLink}
      />

      <nav
        className="bottom-nav"
        aria-label="ناوبری کرونوس"
      >
        <button
          type="button"
          className={
            viewPhase ===
              "home" ||
            viewPhase ===
              "leaving-conversations" ||
            viewPhase ===
              "leaving-library" ||
            viewPhase ===
              "leaving-permissions"
            || viewPhase ===
              "leaving-settings"
              ? "nav-item nav-item-active"
              : "nav-item"
          }
          aria-label="خانه"
          onClick={
            returnHome
          }
        >
          <span className="nav-icon">
            <svg
              viewBox="0 0 28 28"
              aria-hidden="true"
            >
              <circle
                cx="14"
                cy="14"
                r="8.2"
              />

              <path
                d="M5.2 14c2.4-5.8 15.2-8.4 18.1-1.7 2.2 5-4.4 9.4-9.9 9.1"
              />

              <circle
                className="nav-icon-core"
                cx="14"
                cy="14"
                r="2"
              />
            </svg>
          </span>

          <span className="nav-label">
            <strong>خانه</strong>
            <small>Home</small>
          </span>
        </button>

        <button
          type="button"
          className={
            viewPhase ===
              "conversations" ||
            viewPhase ===
              "entering-conversations"
              ? "nav-item nav-item-active"
              : "nav-item"
          }
          aria-label="گفتگوها"
          onClick={
            openConversations
          }
        >
          <span className="nav-icon">
            <svg
              viewBox="0 0 28 28"
              aria-hidden="true"
            >
              <path
                d="M5 7.5h13.5a4 4 0 0 1 4 4v3.3a4 4 0 0 1-4 4H12l-4.5 3v-3.4A4 4 0 0 1 5 14.8V7.5Z"
              />

              <path
                d="M9 13h1.4M13.3 13h1.4M17.6 13H19"
              />
            </svg>
          </span>

          <span className="nav-label">
            <strong>گفتگوها</strong>
            <small>Chat</small>
          </span>
        </button>

        <button
          type="button"
          className={
            viewPhase ===
              "library" ||
            viewPhase ===
              "entering-library"
              ? "nav-item nav-item-active"
              : "nav-item"
          }
          aria-label="کتابخانه"
          onClick={
            openLibrary
          }
        >
          <span className="nav-icon">
            <svg
              viewBox="0 0 28 28"
              aria-hidden="true"
            >
              <path
                d="M5.5 8.2 14 4l8.5 4.2L14 12.4 5.5 8.2Z"
              />

              <path
                d="M5.5 13.1 14 17.3l8.5-4.2M5.5 18 14 22.2l8.5-4.2"
              />

              <circle
                className="nav-icon-core"
                cx="14"
                cy="12.4"
                r="1.5"
              />
            </svg>
          </span>

          {libraryUnreadCount > 0 && (
            <span
              className="nav-notification-badge"
              aria-label={`${libraryUnreadCount} مورد جدید در کتابخانه`}
            >
              {libraryUnreadCount > 99 ? "99+" : libraryUnreadCount}
            </span>
          )}

          <span className="nav-label">
            <strong>کتابخانه</strong>
            <small>Library</small>
          </span>
        </button>

        <button
          type="button"
          data-qronos-nav="permissions"
          className={
            viewPhase ===
              "permissions" ||
            viewPhase ===
              "entering-permissions"
              ? "nav-item nav-item-active"
              : "nav-item"
          }
          aria-label="مجوزها"
          onClick={
            openPermissions
          }
        >
          <span className="nav-icon">
            <svg
              viewBox="0 0 28 28"
              aria-hidden="true"
            >
              <path
                d="M14 4.5 22 7v6.3c0 5.2-3.2 8.4-8 10.2-4.8-1.8-8-5-8-10.2V7l8-2.5Z"
              />

              <path
                d="m10.4 14 2.3 2.4 5-5.2"
              />
            </svg>
          </span>

          <span className="nav-label">
            <strong>مجوزها</strong>
            <small>Permissions</small>
          </span>
        </button>

        <button
          type="button"
          className={
            viewPhase ===
                "settings" ||
              viewPhase ===
                "entering-settings"
              ? "nav-item nav-item-active"
              : "nav-item"
          }
          aria-label="تنظیمات"
          onClick={
            openSettings
          }
        >
          <span className="nav-icon">
            <svg
              viewBox="0 0 28 28"
              aria-hidden="true"
            >
              <path
                d="M6 8h16M6 14h16M6 20h16"
              />

              <circle
                cx="11"
                cy="8"
                r="2"
              />

              <circle
                cx="18"
                cy="14"
                r="2"
              />

              <circle
                cx="9"
                cy="20"
                r="2"
              />
            </svg>
          </span>

          <span className="nav-label">
            <strong>تنظیمات</strong>
            <small>Settings</small>
          </span>
        </button>
      </nav>
    </main>
  );
}

export default App;
