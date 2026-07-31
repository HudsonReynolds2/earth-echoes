/**
 * Map surface (spec §9). E6 owns the map engine; DES.7 builds the frame around
 * it — context band, view tabs, and the status legend — so the engine drops
 * into a settled layout rather than arriving with its own chrome.
 *
 * Engine decision is recorded, not implemented (docs/project-changes.md):
 * Google Maps satellite via the official JS API online, an operator-supplied
 * local image for air-gapped hosts (spec §15.1), ESRI later.
 */
import { useState } from "react";

import { ContextBar } from "../components/ContextBar";
import { EmptyState } from "../components/EmptyState";
import { StatusLegend } from "../components/StatusChip";

const TABS = ["Status", "Reconciliation", "Alerts"];

export function Map() {
  const [tab, setTab] = useState(TABS[0]);

  return (
    <>
      <ContextBar
        crumbs={[{ label: "Organization" }]}
        tabs={TABS}
        activeTab={tab}
        onTabChange={setTab}
      />
      <div className="map-region" data-testid="map-region">
        <EmptyState title="No map imagery yet" testId="map-empty">
          Satellite imagery and live device markers arrive with E6. The status vocabulary below is
          final — every marker, badge, and table cell renders these six states with a color, a
          shape, and a label.
        </EmptyState>
        <div className="map-legend-slot">
          <StatusLegend />
        </div>
      </div>
    </>
  );
}
