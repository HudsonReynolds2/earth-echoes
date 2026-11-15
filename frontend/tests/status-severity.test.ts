/**
 * E6.4: the client's severity order is the server's (D60, D191). The cluster
 * icon (E6.2b) is the only rollup the browser computes, and a different order
 * there would let a cluster disagree with the Pod marker beneath it.
 */
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

import { describe, expect, it } from "vitest";

import { STATUS_SEVERITY, worstStatus } from "../src/components/StatusChip";

const HERE = dirname(fileURLToPath(import.meta.url));
const DEVICE_STATUS = join(HERE, "..", "..", "backend", "app", "controlplane", "device_status.py");

function backendSeverity(): string[] {
  const source = readFileSync(DEVICE_STATUS, "utf-8");
  const block = source.match(/^SEVERITY:[^=]*=\s*\(([\s\S]*?)^\)/m);
  expect(block, "SEVERITY tuple not found in device_status.py").not.toBeNull();
  const members = [...block![1].matchAll(/DeviceStatus\.([A-Z]+)/g)].map((m) => m[1].toLowerCase());
  return members;
}

describe("status severity", () => {
  it("mirrors device_status.SEVERITY exactly", () => {
    expect([...STATUS_SEVERITY]).toEqual(backendSeverity());
  });

  it("picks the worst, and unknown for nothing", () => {
    expect(worstStatus(["healthy", "unknown", "drifted"])).toBe("drifted");
    expect(worstStatus(["sleeping", "offline", "alerting"])).toBe("alerting");
    expect(worstStatus(["unknown", "healthy"])).toBe("healthy");
    expect(worstStatus([])).toBe("unknown");
  });
});
