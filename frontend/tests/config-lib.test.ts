/**
 * Gate 37: the pure config helpers (task E2.7). The level-rule truth table
 * (editableAt, D50), provenance resolution, the one-PUT draft builder, and
 * unit derivation live HERE, unit-tested, so the JSX stays declarative.
 */
import { describe, expect, it } from "vitest";

import {
  buildDraftPut,
  CatalogKey,
  editableAt,
  EntityLevel,
  groupOf,
  isSecretSet,
  LEVELS,
  provenanceOf,
  REVERT,
  unitOf,
} from "../src/lib/config";

function def(partial: Partial<CatalogKey> & { key: string }): CatalogKey {
  return {
    value_type: "string",
    enum_values: null,
    min_value: null,
    max_value: null,
    default: null,
    lowest_level: "listener",
    secret: false,
    resolution: "override",
    write_restricted: null,
    notes: "",
    ...partial,
  };
}

describe("editableAt — the D50 truth table", () => {
  it("allows at-or-above the lowest level, never below", () => {
    const cases: Array<[CatalogKey["lowest_level"], EntityLevel[], EntityLevel[]]> = [
      ["listener", ["organization", "deployment", "pod", "aggregator", "listener"], []],
      ["aggregator", ["organization", "deployment", "pod", "aggregator"], ["listener"]],
      ["pod", ["organization", "deployment", "pod"], ["aggregator", "listener"]],
      ["deployment", ["organization", "deployment"], ["pod", "aggregator", "listener"]],
      ["any", ["organization", "deployment", "pod", "aggregator", "listener"], []],
    ];
    for (const [lowest, allowed, denied] of cases) {
      const item = def({ key: `probe.${lowest}`, lowest_level: lowest });
      for (const level of allowed) {
        expect(editableAt(item, level), `${lowest} at ${level}`).toBe(true);
      }
      for (const level of denied) {
        expect(editableAt(item, level), `${lowest} at ${level}`).toBe(false);
      }
    }
    expect(LEVELS).toHaveLength(5);
  });

  it("never lets inventory or service keys edit through overrides", () => {
    const inventory = def({ key: "identity.name", resolution: "inventory" });
    const service = def({
      key: "telemetry.influx_url",
      lowest_level: "deployment",
      write_restricted: "service_onboarding",
    });
    for (const level of LEVELS) {
      expect(editableAt(inventory, level)).toBe(false);
      expect(editableAt(service, level)).toBe(false);
    }
  });
});

describe("provenanceOf", () => {
  const item = def({ key: "audio.sample_rate_hz" });
  it("orders edited > overridden-here > inherited > default", () => {
    const staged = new Map([["audio.sample_rate_hz", 96000]]);
    expect(provenanceOf(item, undefined, "pod", {}, staged)).toBe("edited");
    expect(
      provenanceOf(
        item,
        { value: 96000, source: "pod", source_entity_id: "p" },
        "pod",
        { "audio.sample_rate_hz": 96000 },
        new Map(),
      ),
    ).toBe("overridden");
    expect(
      provenanceOf(
        item,
        { value: 96000, source: "deployment", source_entity_id: "d" },
        "pod",
        {},
        new Map(),
      ),
    ).toBe("inherited");
    expect(
      provenanceOf(
        item,
        { value: 48000, source: "default", source_entity_id: null },
        "pod",
        {},
        new Map(),
      ),
    ).toBe("default");
  });

  it("inventory keys are always inventory", () => {
    const inventory = def({ key: "identity.mac", resolution: "inventory" });
    expect(
      provenanceOf(
        inventory,
        { value: "02:00", source: "inventory", source_entity_id: "02:00" },
        "listener",
        {},
        new Map(),
      ),
    ).toBe("inventory");
  });
});

describe("buildDraftPut — the one-PUT body", () => {
  it("folds staged edits in and drops reverts", () => {
    const server = { "network.wifi_ssid": "old", "capture.duty_on_seconds": 90 };
    const staged = new Map<string, unknown | typeof REVERT>([
      ["network.wifi_ssid", "new-mesh"],
      ["capture.duty_on_seconds", REVERT],
      ["logging.verbosity", "debug"],
    ]);
    expect(buildDraftPut(server, staged)).toEqual({
      "network.wifi_ssid": "new-mesh",
      "logging.verbosity": "debug",
    });
    // Inputs untouched.
    expect(server).toEqual({ "network.wifi_ssid": "old", "capture.duty_on_seconds": 90 });
  });

  it("round-trips kept secrets untouched (the sentinel passes through)", () => {
    const server = { "network.wifi_password": { $secret_set: true } };
    expect(buildDraftPut(server, new Map())).toEqual(server);
    expect(isSecretSet(buildDraftPut(server, new Map())["network.wifi_password"])).toBe(true);
  });
});

describe("small helpers", () => {
  it("groupOf takes the dotted prefix", () => {
    expect(groupOf("audio.sample_rate_hz")).toBe("audio");
    expect(groupOf("test.demo_knob")).toBe("test");
    expect(groupOf("plain")).toBe("plain");
  });

  it("unitOf derives presentational suffixes from key tails", () => {
    expect(unitOf("capture.duty_on_seconds")).toBe("s");
    expect(unitOf("audio.sample_rate_hz")).toBe("Hz");
    expect(unitOf("buffering.sd_max_bytes")).toBe("bytes");
    expect(unitOf("capture.mode")).toBeNull();
  });

  it("isSecretSet recognizes exactly the sentinel", () => {
    expect(isSecretSet({ $secret_set: true })).toBe(true);
    expect(isSecretSet({ $secret_set: false })).toBe(false);
    expect(isSecretSet("hunter2")).toBe(false);
    expect(isSecretSet(null)).toBe(false);
  });
});
