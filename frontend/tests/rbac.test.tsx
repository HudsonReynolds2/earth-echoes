/**
 * Gate 7 frontend checks (task E0.7): the can() decision mirror, the Can
 * gate component, and the cross-language parity test that keeps
 * src/lib/rbac.ts in lockstep with backend/app/auth/rbac.py.
 */
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import { http, HttpResponse } from "msw";
import { describe, expect, it } from "vitest";

import { Can } from "../src/components/Can";
import { Assignment, ROLE_PERMISSIONS, can } from "../src/lib/rbac";
import { mePayload, server } from "./msw-server";

const HERE = dirname(fileURLToPath(import.meta.url));
const PYTHON_RBAC = join(HERE, "..", "..", "backend", "app", "auth", "rbac.py");

const DEP_A = "00000000-0000-0000-0000-00000000000a";
const DEP_B = "00000000-0000-0000-0000-00000000000b";

const orgOwner: Assignment[] = [{ role: "owner", deployment_id: null }];
const scopedTech: Assignment[] = [{ role: "field_tech", deployment_id: DEP_A }];

describe("can() decision mirror", () => {
  it("grants org-wide assignments everywhere", () => {
    expect(can(orgOwner, "manage_users")).toBe(true);
    expect(can(orgOwner, "manage_config", DEP_B)).toBe(true);
  });

  it("scopes deployment assignments to their deployment", () => {
    expect(can(scopedTech, "manage_provisioning", DEP_A)).toBe(true);
    expect(can(scopedTech, "manage_provisioning", DEP_B)).toBe(false);
    expect(can(scopedTech, "manage_provisioning")).toBe(false);
    expect(can(scopedTech, "manage_config", DEP_A)).toBe(false);
  });

  it("grants nothing without assignments or for unknown roles", () => {
    expect(can([], "view_status", DEP_A)).toBe(false);
    expect(can([{ role: "made_up", deployment_id: null }], "view_status")).toBe(false);
  });

  // E5.2 (phase-5 fixed choice 9). The parity test below already fails on any
  // divergence from the backend map; this states the rule the wizard reads
  // from, so a UI change that hides the services step behind the wrong
  // permission fails here rather than in review.
  it("lets every role see services and only owner + operator manage them", () => {
    const scopedOperator: Assignment[] = [{ role: "deployment_operator", deployment_id: DEP_A }];
    const orgViewer: Assignment[] = [{ role: "viewer", deployment_id: null }];

    expect(can(orgOwner, "manage_services", DEP_A)).toBe(true);
    expect(can(scopedOperator, "manage_services", DEP_A)).toBe(true);
    expect(can(scopedOperator, "manage_services", DEP_B)).toBe(false);
    expect(can(scopedTech, "manage_services", DEP_A)).toBe(false);
    expect(can(orgViewer, "manage_services", DEP_A)).toBe(false);

    expect(can(scopedTech, "view_services", DEP_A)).toBe(true);
    expect(can(orgViewer, "view_services", DEP_A)).toBe(true);
  });
});

describe("parity with the canonical backend map", () => {
  const python = readFileSync(PYTHON_RBAC, "utf-8");

  it("mirrors every role's permission set", () => {
    for (const [role, permissions] of Object.entries(ROLE_PERMISSIONS)) {
      if (role === "owner") {
        expect(python).toContain("Role.OWNER: frozenset(Permission)");
        continue;
      }
      const constant = `Role.${role.toUpperCase()}: frozenset(`;
      const start = python.indexOf(constant);
      expect(start, `backend map missing role ${role}`).toBeGreaterThan(-1);
      const section = python.slice(start, python.indexOf("    ),", start));
      const backendPermissions = [...section.matchAll(/Permission\.([A-Z_]+)/g)].map((m) =>
        m[1].toLowerCase(),
      );
      expect(backendPermissions.sort(), `role ${role} diverges`).toEqual([...permissions].sort());
    }
  });

  it("mirrors the full permission vocabulary", () => {
    // Anchor both bounds on definitions, not prose: the module docstring also
    // mentions ROLE_PERMISSIONS, which an unanchored indexOf would find first.
    const start = python.indexOf("class Permission");
    const end = python.indexOf("ROLE_PERMISSIONS:", start);
    expect(start).toBeGreaterThan(-1);
    expect(end).toBeGreaterThan(start);
    const enumSection = python.slice(start, end);
    const backendVocabulary = [...enumSection.matchAll(/= "([a-z_]+)"/g)].map((m) => m[1]);
    expect(backendVocabulary.length).toBeGreaterThan(0);
    // The owner set is the full vocabulary by definition on both sides.
    expect([...ROLE_PERMISSIONS.owner].sort()).toEqual(backendVocabulary.sort());
  });
});

describe("Can component", () => {
  function renderGate(permission: Parameters<typeof Can>[0]["permission"]) {
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    return render(
      <QueryClientProvider client={client}>
        <Can permission={permission} fallback={<p data-testid="denied">denied</p>}>
          <p data-testid="granted">granted</p>
        </Can>
      </QueryClientProvider>,
    );
  }

  it("renders children for a permitted user", async () => {
    server.use(
      http.get("http://api.test/api/v1/auth/me", () =>
        HttpResponse.json({
          ...mePayload,
          assignments: [{ role: "owner", deployment_id: null }],
        }),
      ),
    );
    renderGate("manage_users");
    expect(await screen.findByTestId("granted")).toBeInTheDocument();
  });

  it("renders the fallback for a denied user", async () => {
    server.use(
      http.get("http://api.test/api/v1/auth/me", () =>
        HttpResponse.json({
          ...mePayload,
          assignments: [{ role: "viewer", deployment_id: null }],
        }),
      ),
    );
    renderGate("manage_users");
    expect(await screen.findByTestId("denied")).toBeInTheDocument();
  });
});
