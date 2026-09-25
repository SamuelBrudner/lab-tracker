// Scheduler chaos (npm run test:frontend:chaos) turns a class of load-dependent
// flakes into failures that happen on every run.
//
// React 19.2 yields to the host after every non-sync commit and runs that
// commit's passive effects (useEffect) in a new host task, which its scheduler
// queues with setImmediate under Node. Testing Library's findBy*/waitFor
// resolve and then drain with a setTimeout(0). On a loaded machine that timer
// can fire first, so a test's next fireEvent or assertion runs against
// committed UI whose effects have not run yet.
//
// This file gives React's scheduler a setImmediate that defers each callback
// by N setTimeout(0) hops (default 2, override with
// LAB_TRACKER_SCHEDULER_CHAOS_HOPS). Equal-delay timers fire in the order they
// were armed, so a single hop still runs before a drain armed after it; the
// second hop re-arms behind the drain. The scheduler captures setImmediate
// when its module is evaluated, so the global is swapped only around that
// import and nothing else is slowed down. That is why this file must run
// before anything imports react-dom. The hops use timers captured up front so
// vi.useFakeTimers() cannot stall them.

const HOPS_VARIABLE = "LAB_TRACKER_SCHEDULER_CHAOS_HOPS";
const realSetTimeout = globalThis.setTimeout;
const realSetImmediate = globalThis.setImmediate;

function readHops() {
  const raw = process.env[HOPS_VARIABLE] ?? "2";
  const hops = Number(raw);
  if (!Number.isInteger(hops) || hops < 1) {
    throw new Error(`${HOPS_VARIABLE} must be a positive integer, got "${raw}".`);
  }
  return hops;
}

const hops = readHops();
let deferredCallbacks = 0;

function deferredSetImmediate(callback, ...args) {
  deferredCallbacks += 1;
  const hop = (remaining) =>
    realSetTimeout(() => (remaining > 1 ? hop(remaining - 1) : callback(...args)), 0);
  hop(hops);
}

globalThis.setImmediate = deferredSetImmediate;
// A dynamic import, so the scheduler is evaluated after the swap.
const Scheduler = await import("scheduler");
globalThis.setImmediate = realSetImmediate;

// If the scheduler was loaded before this file ran, or stops using
// setImmediate for host work, chaos runs would pass without any chaos.
const callbacksBeforeProbe = deferredCallbacks;
const probe = new Promise((resolve) => {
  Scheduler.unstable_scheduleCallback(Scheduler.unstable_NormalPriority, () => resolve());
});
if (deferredCallbacks === callbacksBeforeProbe) {
  throw new Error(
    "Scheduler chaos is not in effect: React's scheduler did not use the deferred " +
      "setImmediate. It was imported before test/scheduler-chaos.js ran, or it no " +
      "longer schedules host work with setImmediate."
  );
}
await probe;
