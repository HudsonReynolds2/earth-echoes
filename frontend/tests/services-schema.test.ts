/**
 * Cross-language parity for the services field table (task E5.12a).
 *
 * E5.12a says the five forms are "rendered from the schema rather than
 * hardcoded field lists". The canonical schema is
 * `backend/app/services/schemas.py` — one Pydantic model per service with
 * `extra="forbid"`, which is what actually decides whether a credential is
 * stored. `src/lib/services.ts` mirrors it so the UI can render from it, and
 * a mirror nobody checks is a mirror that drifts: a field added to the model
 * and not to the table is a field the operator can never enter, and a field
 * in the table but not in the model is a 422 on save.
 *
 * This is the same discipline `rbac.test.tsx` holds `lib/rbac.ts` to, for the
 * same reason. The parse is deliberately literal — if `schemas.py` grows a
 * shape this cannot read, the right answer is to teach it that shape, not to
 * loosen the assertion.
 */
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

import { describe, expect, it } from "vitest";

import { SERVICE_KEYS, SERVICE_SCHEMA, ServiceField } from "../src/lib/services";

const HERE = dirname(fileURLToPath(import.meta.url));
const PYTHON_SCHEMAS = join(HERE, "..", "..", "backend", "app", "services", "schemas.py");

interface PythonField {
  name: string;
  /** The annotation, without the default. */
  annotation: string;
  required: boolean;
}

interface PythonModel {
  serviceKey: string;
  secretFields: string[];
  fields: PythonField[];
}

/** Docstrings legally contain lines that look like field declarations. */
function stripDocstrings(source: string): string {
  return source.replace(/"""[\s\S]*?"""/g, "");
}

function parseTuple(literal: string): string[] {
  return [...literal.matchAll(/"([^"]+)"/g)].map((match) => match[1]);
}

/**
 * Required means "no default on the Python side", which takes two forms:
 * a bare annotation (`password: str | KeepSecret`), and a `Field(...)` call
 * carrying constraints but no `default=` (`host: str = Field(min_length=1)`).
 * Anything else — `= None`, `= True`, `Field(default=None, ...)` — is
 * optional.
 */
function isRequired(rest: string): boolean {
  const equals = rest.indexOf("=");
  if (equals === -1) {
    return true;
  }
  const assigned = rest.slice(equals + 1).trim();
  return assigned.startsWith("Field(") && !assigned.includes("default=");
}

function parsePythonModels(): Map<string, PythonModel> {
  const source = stripDocstrings(readFileSync(PYTHON_SCHEMAS, "utf-8"));
  const models = new Map<string, PythonModel>();

  for (const block of source.split(/\nclass /).slice(1)) {
    if (!block.split("\n")[0].includes("(ServiceSettings)")) {
      continue;
    }
    const serviceKey = block.match(/service_key: ClassVar\[str\] = "([a-z0-9_]+)"/)?.[1];
    expect(serviceKey, "every ServiceSettings subclass declares a service_key").toBeDefined();

    const secretMatch = block.match(
      /secret_fields: ClassVar\[tuple\[str, \.\.\.\]\] = (\([^)]*\))/,
    );
    const secretFields = secretMatch ? parseTuple(secretMatch[1]) : [];

    const fields: PythonField[] = [];
    for (const line of block.split("\n")) {
      const match = line.match(/^ {4}([a-z_][a-z0-9_]*): (.+)$/);
      if (!match || match[2].startsWith("ClassVar[")) {
        continue;
      }
      const [, name, rest] = match;
      const annotation = rest.split("=")[0].trim();
      fields.push({ name, annotation, required: isRequired(rest) });
    }
    models.set(serviceKey!, { serviceKey: serviceKey!, secretFields, fields });
  }
  return models;
}

const PYTHON = parsePythonModels();

/** What the descriptor must say a field is, given the Python annotation. */
function expectedTypes(field: PythonField, secret: boolean): ServiceField["type"][] {
  if (secret) {
    return ["secret"];
  }
  if (field.annotation === "int") {
    return ["number"];
  }
  if (field.annotation === "bool") {
    return ["boolean"];
  }
  // Everything else is a string on the wire. Which widget it gets — a line or
  // a box — is the frontend's own call and not the model's.
  return ["text", "textarea"];
}

describe("the parse itself", () => {
  // A parser that silently found nothing would make every assertion below
  // vacuously true, which is the failure mode this whole file exists to avoid.
  it("found all five models with fields in them", () => {
    expect([...PYTHON.keys()].sort()).toEqual([...SERVICE_KEYS].sort());
    for (const model of PYTHON.values()) {
      expect(model.fields.length, `${model.serviceKey} parsed no fields`).toBeGreaterThan(0);
    }
  });

  it("reads required-ness in both of its Python forms", () => {
    const mqtt = PYTHON.get("mqtt")!;
    // `host: str = Field(min_length=1, max_length=255)` — constrained, no default.
    expect(mqtt.fields.find((f) => f.name === "host")!.required).toBe(true);
    // `password: str | KeepSecret` — bare annotation.
    expect(mqtt.fields.find((f) => f.name === "password")!.required).toBe(true);
    // `tls_enabled: bool = True` — a plain default.
    expect(mqtt.fields.find((f) => f.name === "tls_enabled")!.required).toBe(false);
    // `admin_username: str | None = Field(default=None, max_length=255)`.
    const grafana = PYTHON.get("grafana")!;
    expect(grafana.fields.find((f) => f.name === "admin_username")!.required).toBe(false);
  });
});

describe("SERVICE_SCHEMA mirrors backend/app/services/schemas.py", () => {
  it("covers exactly the five services, in the spec 16.2 order", () => {
    expect(SERVICE_SCHEMA.map((descriptor) => descriptor.key)).toEqual([...SERVICE_KEYS]);
  });

  it("declares exactly the model's fields, in the model's order", () => {
    for (const descriptor of SERVICE_SCHEMA) {
      const model = PYTHON.get(descriptor.key)!;
      expect(
        descriptor.fields.map((field) => field.name),
        `${descriptor.key}: the form's fields and the Pydantic model's have diverged`,
      ).toEqual(model.fields.map((field) => field.name));
    }
  });

  it("marks exactly the model's secret_fields as secrets", () => {
    for (const descriptor of SERVICE_SCHEMA) {
      const model = PYTHON.get(descriptor.key)!;
      const declared = descriptor.fields
        .filter((field) => field.type === "secret")
        .map((field) => field.name);
      expect(
        declared.sort(),
        `${descriptor.key}: a field the API treats as a credential must be write-only in the UI`,
      ).toEqual([...model.secretFields].sort());
    }
  });

  it("agrees with the model on which fields are required", () => {
    for (const descriptor of SERVICE_SCHEMA) {
      const model = PYTHON.get(descriptor.key)!;
      for (const field of descriptor.fields) {
        const python = model.fields.find((candidate) => candidate.name === field.name)!;
        expect(
          field.required,
          `${descriptor.key}.${field.name}: required-ness disagrees with the model`,
        ).toBe(python.required);
      }
    }
  });

  it("gives every field an input type its annotation can carry", () => {
    for (const descriptor of SERVICE_SCHEMA) {
      const model = PYTHON.get(descriptor.key)!;
      for (const field of descriptor.fields) {
        const python = model.fields.find((candidate) => candidate.name === field.name)!;
        const secret = model.secretFields.includes(field.name);
        expect(
          expectedTypes(python, secret),
          `${descriptor.key}.${field.name}: ${python.annotation} cannot be entered as ${field.type}`,
        ).toContain(field.type);
      }
    }
  });

  it("gives every field a human label", () => {
    // The labels are the frontend's own half and nothing checks them against
    // Python — so check they exist at all, rather than shipping a form of
    // snake_case field names.
    for (const descriptor of SERVICE_SCHEMA) {
      expect(descriptor.label.length).toBeGreaterThan(0);
      for (const field of descriptor.fields) {
        expect(field.label, `${descriptor.key}.${field.name} has no label`).toBeTruthy();
        expect(field.label).not.toBe(field.name);
      }
    }
  });
});
