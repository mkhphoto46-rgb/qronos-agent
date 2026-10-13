import { useEffect, useMemo, useState } from "react";

import DeviceHubPanel, { createDesktopDeviceHubClient } from "./DeviceHubPanel";

import "./DeviceHubPanel.css";

type DeviceHubDrawerProps = { hidden: boolean };

// The Device Hub entry: a toggle opening the Persian device panel. The one client lives as long as the drawer;
// hiding the panel resets it (pending answers become stale, the typed secret is cleared).
function DeviceHubDrawer({ hidden }: DeviceHubDrawerProps) {
  const [open, setOpen] = useState(false);
  const client = useMemo(() => createDesktopDeviceHubClient(), []);

  useEffect(() => () => client.dispose(), [client]);

  if (hidden) {
    return null;
  }

  return (
    <div className="device-hub-drawer" dir="rtl">
      <button type="button" className={`device-hub-toggle${open ? " device-hub-toggle-open" : ""}`}
              aria-expanded={open} onClick={() => setOpen((value) => !value)}>
        دستگاه‌ها
      </button>
      {/* Always mounted: closing passes visible=false, so the panel resets its client and clears the secret. */}
      <div className="device-hub-body" hidden={!open}>
        <DeviceHubPanel client={client} visible={open} />
      </div>
    </div>
  );
}

export default DeviceHubDrawer;
