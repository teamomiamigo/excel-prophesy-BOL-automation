import { useEffect } from 'react';

const EDGE_SCROLL_ZONE_PX = 140;
const EDGE_SCROLL_SPEED_PX = 8;

// Drag-near-the-edge horizontal auto-scroll for a wide, horizontally-scrollable
// table. scrollRef must point at the `overflowX: auto` container; enabled gates
// whether the listeners are attached (e.g. pass a "content is rendered" boolean
// so this doesn't attach to an empty/loading table).
export default function useEdgeScroll(scrollRef, enabled) {
  useEffect(() => {
    const el = scrollRef.current;
    if (!el) return;

    let velocity = 0;
    let rafId = null;

    const step = () => {
      if (velocity !== 0) {
        el.scrollLeft += velocity;
        rafId = requestAnimationFrame(step);
      } else {
        rafId = null;
      }
    };

    const handleMouseMove = e => {
      if (el.scrollWidth <= el.clientWidth) {
        velocity = 0;
        return;
      }
      const rect = el.getBoundingClientRect();
      const x = e.clientX - rect.left;
      if (x < EDGE_SCROLL_ZONE_PX) {
        velocity = -EDGE_SCROLL_SPEED_PX;
      } else if (x > rect.width - EDGE_SCROLL_ZONE_PX) {
        velocity = EDGE_SCROLL_SPEED_PX;
      } else {
        velocity = 0;
      }
      if (velocity !== 0 && rafId === null) {
        rafId = requestAnimationFrame(step);
      }
    };

    const handleMouseLeave = () => {
      velocity = 0;
    };

    el.addEventListener('mousemove', handleMouseMove);
    el.addEventListener('mouseleave', handleMouseLeave);

    return () => {
      el.removeEventListener('mousemove', handleMouseMove);
      el.removeEventListener('mouseleave', handleMouseLeave);
      if (rafId !== null) cancelAnimationFrame(rafId);
    };
  }, [scrollRef, enabled]);
}
