// Drop-in replacement for window.setInterval that automatically
// pauses while the browser tab is hidden and resumes when it is shown
// again. Use it inside a useEffect exactly where you'd call
// setInterval, and return (or call) the result in the effect cleanup
// instead of clearInterval:
//
//   useEffect(() => {
//     refresh();                              // initial load (unchanged)
//     return pausableInterval(refresh, 5000); // visibility-aware poll
//   }, []);
//
// Background polling the operator can't see is wasted backend load and
// laptop battery. Pausing on tab-hide reclaims both with no visible
// behaviour change: the timer simply resumes on re-show (the next tick
// then fires one interval later, exactly as a never-paused timer's
// next tick would have).

export function pausableInterval(fn: () => void, ms: number): () => void {
  let timer: number | null = null;

  const start = () => {
    if (timer === null) timer = window.setInterval(fn, ms);
  };
  const stop = () => {
    if (timer !== null) {
      window.clearInterval(timer);
      timer = null;
    }
  };
  const onVisibility = () => {
    if (document.hidden) stop();
    else start();
  };

  // Start immediately unless the tab is already hidden at mount.
  if (!document.hidden) start();
  document.addEventListener('visibilitychange', onVisibility);

  // Cleanup: detach the listener and clear any live timer.
  return () => {
    document.removeEventListener('visibilitychange', onVisibility);
    stop();
  };
}
