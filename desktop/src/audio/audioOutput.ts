export type AudioOutputDevice = {
  id: string;
  name: string;
  hostApi: string;
  deviceIndex: number | null;
  isDefault: boolean;
  maxOutputChannels: number;
  defaultSampleRate: number;
};

export type AudioOutputDevicesResponse = {
  ok: boolean;
  devices: AudioOutputDevice[];
  selectedDeviceId?: string;
  selectionRemapped?: boolean;
};

export const SYSTEM_DEFAULT_OUTPUT_DEVICE_ID = "";
export const NATIVE_OUTPUT_DEVICE_PREFIX = "portaudio-output:";

export function isNativeAudioOutputDeviceId(
  deviceId: string,
): boolean {
  return deviceId
    .trim()
    .startsWith(
      NATIVE_OUTPUT_DEVICE_PREFIX,
    );
}
