/**
 * Exposes Leaflet as the global `L` before `leaflet.markercluster` loads.
 *
 * The cluster plugin is a UMD build that extends `window.L` rather than
 * importing Leaflet. Under ES modules there is no such global, so importing it
 * directly fails with "L is not defined" in the browser and in tests alike.
 * This module must be imported BEFORE `leaflet.markercluster`; ES modules
 * evaluate imports in order, which is what makes that guarantee hold.
 */
import L from "leaflet";

// The plugin reads and extends `window.L`; there is no typed way to say so.
(window as unknown as { L: typeof L }).L = L;

export default L;
