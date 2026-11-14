/**
 * Where GPS-less Listeners are drawn around their Pod (task E6.3; spec 9.2:
 * "A leaf node clustered visually around its Aggregator, not placed on real
 * coordinates, until coordinates are filled in").
 *
 * **Screen space, not geography.** Each leaf gets a pixel offset from its
 * Pod's marker on one or more rings. A pixel offset is the honest encoding: it
 * stays the same distance from the Pod at every zoom, so a leaf can never be
 * mistaken for a surveyed position, and it never wanders into a neighbouring
 * Pod's territory when the operator zooms out.
 *
 * **The upper half only**, from 9 o'clock over the top to 3 o'clock. The
 * Pod's label hangs below its marker, and a leaf drawn there would sit on top
 * of the words that say which Pod it belongs to.
 *
 * Pure and deterministic: the same count always gives the same fan, so a
 * refetch never reshuffles leaves under the operator's cursor.
 */

/** Distance from the Pod's center to the first ring, in px. Clears the
 * largest Pod square (44px) and its alert badge. */
export const FIRST_RING = 48;
/** Center-to-center spacing between leaves and between rings, in px: a 20px
 * leaf plus a 6px gap, so leaves never touch. */
export const LEAF_SPACING = 26;

export interface LeafOffset {
  dx: number;
  dy: number;
  ring: number;
}

/** How many leaves fit on ring `ring` (0-based) across the half-circle,
 * both ends included, without touching. */
export function ringCapacity(ring: number): number {
  const radius = FIRST_RING + ring * LEAF_SPACING;
  return Math.max(1, Math.floor((Math.PI * radius) / LEAF_SPACING) + 1);
}

/**
 * `count` offsets, filling the innermost ring first. A ring's leaves are
 * centered on 12 o'clock at that ring's own spacing, so a partly filled ring
 * stays compact over the Pod and a full one spans 9 to 3 o'clock. Screen
 * coordinates: y grows downward, so every `dy` is at most 0.
 */
export function leafOffsets(count: number): LeafOffset[] {
  const out: LeafOffset[] = [];
  let ring = 0;
  while (out.length < count) {
    const capacity = ringCapacity(ring);
    const onThisRing = Math.min(capacity, count - out.length);
    const radius = FIRST_RING + ring * LEAF_SPACING;
    const step = capacity > 1 ? Math.PI / (capacity - 1) : 0;
    for (let i = 0; i < onThisRing; i += 1) {
      const angle = -Math.PI / 2 + (i - (onThisRing - 1) / 2) * step;
      out.push({
        dx: Math.round(radius * Math.cos(angle) * 100) / 100 + 0,
        dy: Math.round(radius * Math.sin(angle) * 100) / 100 + 0,
        ring,
      });
    }
    ring += 1;
  }
  return out;
}
