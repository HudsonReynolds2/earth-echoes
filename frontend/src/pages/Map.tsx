/**
 * Map surface (spec §9). DES.7 built the frame: context band, view tabs and
 * the status legend. E6 fills the region with the map itself.
 *
 * The engine is Leaflet with switchable basemaps, satellite by default (E6.1,
 * D188). This supersedes the Google Maps JS API direction in
 * project-changes #9 (#48, #49).
 */
import { useQuery } from "@tanstack/react-query";
import { useState } from "react";

import { ContextBar } from "../components/ContextBar";
import { EmptyState } from "../components/EmptyState";
import { MapView } from "../components/map/MapView";
import { StatusLegend } from "../components/StatusChip";
import { getMapConfig, mapKeys } from "../lib/map";

const TABS = ["Status", "Reconciliation", "Alerts"];

export function Map() {
  const [tab, setTab] = useState(TABS[0]);
  const config = useQuery({ queryKey: mapKeys.config, queryFn: getMapConfig });

  return (
    <>
      <ContextBar
        crumbs={[{ label: "Organization" }]}
        tabs={TABS}
        activeTab={tab}
        onTabChange={setTab}
      />
      <div className="map-region" data-testid="map-region">
        {config.isLoading ? (
          <div className="skeleton map-skeleton" data-testid="map-loading" />
        ) : config.data === undefined ? (
          <EmptyState title="The map could not load" testId="map-config-error">
            The basemap configuration did not arrive
            {config.error instanceof Error ? ` (${config.error.message})` : ""}. Check that the API
            is reachable and that you are signed in.
          </EmptyState>
        ) : (
          <MapView
            config={config.data}
            overlay={
              <p className="map-notice" data-testid="map-no-devices">
                No devices on the map yet. Markers arrive with the next update (E6.2b).
              </p>
            }
          />
        )}
        <div className="map-legend-slot">
          <StatusLegend />
        </div>
      </div>
    </>
  );
}
