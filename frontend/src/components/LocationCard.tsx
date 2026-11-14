/**
 * Where a Deployment or a Pod sits on the map (task E6.2a; spec 9.2; D189).
 *
 * One component for both levels, because the rule is the same: both
 * coordinates or neither, in range, and no guessed fallback. A level with no
 * position is not drawn on the map; this card is where an operator fixes that,
 * and the map's Unplaced panel (E6.2b) links here.
 *
 * The half-pair check runs here so the message appears before a round trip,
 * and the API enforces the same rule with a 422 for any client that skips it.
 */
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { FormEvent, useState } from "react";

import type { Coordinates } from "../lib/inventory";
import { Can } from "./Can";

export const HALF_PAIR_MESSAGE = "Give both latitude and longitude, or neither.";

/** Parse the two inputs into a PATCH body, or say what is wrong. */
export function parseCoordinates(lat: string, lon: string): Coordinates | string {
  const latText = lat.trim();
  const lonText = lon.trim();
  if ((latText === "") !== (lonText === "")) return HALF_PAIR_MESSAGE;
  if (latText === "") return { gps_lat: null, gps_lon: null };
  const gpsLat = Number(latText);
  const gpsLon = Number(lonText);
  if (Number.isNaN(gpsLat) || Number.isNaN(gpsLon)) {
    return "Latitude and longitude must be numbers.";
  }
  if (gpsLat < -90 || gpsLat > 90) return "Latitude must be between -90 and 90.";
  if (gpsLon < -180 || gpsLon > 180) return "Longitude must be between -180 and 180.";
  return { gps_lat: gpsLat, gps_lon: gpsLon };
}

export function LocationCard({
  title,
  what,
  value,
  deploymentId,
  onSave,
  testId,
}: {
  title: string;
  /** What is being placed, for the copy: "this deployment", "this pod". */
  what: string;
  value: Coordinates;
  deploymentId: string;
  onSave: (coordinates: Coordinates) => Promise<unknown>;
  testId: string;
}) {
  const queryClient = useQueryClient();
  const [editing, setEditing] = useState(false);
  const [lat, setLat] = useState("");
  const [lon, setLon] = useState("");
  const [problem, setProblem] = useState<string | null>(null);
  const save = useMutation({
    mutationFn: onSave,
    onSuccess: () => {
      setEditing(false);
      // Inventory and the map both read these columns.
      void queryClient.invalidateQueries();
    },
  });

  const placed = value.gps_lat !== null && value.gps_lon !== null;

  const start = () => {
    setLat(value.gps_lat === null ? "" : String(value.gps_lat));
    setLon(value.gps_lon === null ? "" : String(value.gps_lon));
    setProblem(null);
    save.reset();
    setEditing(true);
  };

  const submit = (event: FormEvent) => {
    event.preventDefault();
    const parsed = parseCoordinates(lat, lon);
    if (typeof parsed === "string") {
      setProblem(parsed);
      return;
    }
    setProblem(null);
    save.mutate(parsed);
  };

  return (
    <section className="card" data-testid={testId}>
      <h2>{title}</h2>
      <dl className="import-summary">
        <dt>GPS</dt>
        <dd className={placed ? "mono" : "muted"} data-testid={`${testId}-value`}>
          {placed
            ? `${value.gps_lat}, ${value.gps_lon}`
            : `Not placed. ${capitalize(what)} is not drawn on the map until it has coordinates.`}
        </dd>
      </dl>
      {!editing && (
        <Can permission="manage_devices" deploymentId={deploymentId}>
          <button type="button" className="btn-secondary" onClick={start}>
            {placed ? "Edit location" : "Set location"}
          </button>
        </Can>
      )}
      {editing && (
        <form className="form" onSubmit={submit} data-testid={`${testId}-form`} noValidate>
          <div className="form-field">
            <label htmlFor={`${testId}-lat`}>Latitude</label>
            <input
              id={`${testId}-lat`}
              inputMode="decimal"
              value={lat}
              onChange={(event) => setLat(event.target.value)}
            />
          </div>
          <div className="form-field">
            <label htmlFor={`${testId}-lon`}>Longitude</label>
            <input
              id={`${testId}-lon`}
              inputMode="decimal"
              value={lon}
              onChange={(event) => setLon(event.target.value)}
            />
            <p className="form-help">
              Decimal degrees, for example 41.2130 and -124.0040. Leave both empty to clear.
            </p>
          </div>
          <div className="form-actions">
            <button type="submit" disabled={save.isPending}>
              Save location
            </button>
            <button type="button" className="btn-tertiary" onClick={() => setEditing(false)}>
              Cancel
            </button>
          </div>
          {(problem ?? (save.isError ? (save.error as Error).message : null)) && (
            <p className="form-error" role="alert" data-testid={`${testId}-error`}>
              {problem ?? (save.error as Error).message}
            </p>
          )}
        </form>
      )}
    </section>
  );
}

function capitalize(text: string): string {
  return text.charAt(0).toUpperCase() + text.slice(1);
}
