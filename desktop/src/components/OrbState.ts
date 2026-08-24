export type OrbState =
  | "idle"
  | "listening"
  | "thinking"
  | "responding";

export const ORB_STATE_LABELS: Record<OrbState, string> = {
  idle: "Idle",
  listening: "Listening",
  thinking: "Thinking",
  responding: "Responding",
};