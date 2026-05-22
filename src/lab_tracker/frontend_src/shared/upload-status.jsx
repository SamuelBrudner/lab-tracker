import * as React from "react";

import { getUploadQueue } from "./register-sw.js";

function usePendingUploadCount() {
  const [count, setCount] = React.useState(0);

  React.useEffect(() => {
    const queue = getUploadQueue();
    if (!queue) {
      return undefined;
    }
    let active = true;

    const refresh = async () => {
      try {
        const next = await queue.pendingCount();
        if (active) {
          setCount(next);
        }
      } catch {
        // Counting is best-effort; failures should not interrupt the UI.
      }
    };

    const unsubscribe = queue.subscribe(refresh);
    refresh();
    return () => {
      active = false;
      unsubscribe();
    };
  }, []);

  return count;
}

function PendingUploadsBadge() {
  const count = usePendingUploadCount();
  if (count <= 0) {
    return null;
  }
  const label = count === 1 ? "1 capture queued offline" : `${count} captures queued offline`;
  return (
    <div className="pending-uploads-badge" role="status" aria-live="polite">
      <span aria-hidden="true">●</span>
      <span>{label}</span>
    </div>
  );
}

export { PendingUploadsBadge, usePendingUploadCount };
