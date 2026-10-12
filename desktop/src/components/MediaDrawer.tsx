import { useEffect, useMemo, useState } from "react";

import MediaPanel, { createDesktopMediaClient } from "./MediaPanel";

import "./MediaPanel.css";

type MediaDrawerProps = {
  hidden: boolean;
};

// The media library entry: a toggle beside the queue toggle opening the Persian media panel. The one client
// lives as long as the drawer; hiding the panel resets it (pending answers become stale, the secret is cleared).
function MediaDrawer({ hidden }: MediaDrawerProps) {
  const [open, setOpen] = useState(false);
  const client = useMemo(() => createDesktopMediaClient(), []);

  useEffect(() => () => client.dispose(), [client]);

  if (hidden) {
    return null;
  }

  return (
    <div className="media-drawer" dir="rtl">
      <button
        type="button"
        className={`media-drawer-toggle${open ? " media-drawer-toggle-open" : ""}`}
        aria-expanded={open}
        onClick={() => setOpen((value) => !value)}
      >
        کتابخانهٔ ویدیو
      </button>
      {/* Always mounted: closing passes visible=false, so the panel resets its client and clears the secret. */}
      <div className="media-drawer-body" hidden={!open}>
        <MediaPanel client={client} visible={open} />
      </div>
    </div>
  );
}

export default MediaDrawer;
