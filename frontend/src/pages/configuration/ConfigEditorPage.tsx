/**
 * The schema-driven config editor (task E2.7; S3 at v2 values; D50-D51).
 * Everything renders from the catalog — the E2.7 acceptance is that a new
 * catalog row grows a working editor with zero changes here. Edits stage
 * locally (the TagEditor precedent) and save as ONE wholesale PUT of the
 * recomputed sparse map; the draft banner counts unsaved keys and stays
 * literally true — nothing reaches devices until a revision publishes, and
 * publishing is E3's (the Publish button renders disabled naming it).
 * Desktop-only by owner decision (DES open question 3, closed).
 */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useMemo, useState } from "react";
import { useOutletContext, useParams } from "react-router-dom";

import { DraftBanner, DraftDiff } from "../../components/DraftDiff";
import { EmptyState } from "../../components/EmptyState";
import { ChainRung, InheritanceChain } from "../../components/InheritanceChain";
import { PageHeader } from "../../components/PageHeader";
import { ProvenanceTable } from "../../components/ProvenanceTable";
import { TagEditor } from "../../components/TagEditor";
import { ToggleSwitch } from "../../components/ToggleSwitch";
import { useCan } from "../../components/Can";
import {
  buildDraftPut,
  CatalogKey,
  ENTITY_PATHS,
  EntityLevel,
  getCatalog,
  getEffectiveConfig,
  getOverrides,
  listRevisions,
  putOverrides,
  REVERT,
} from "../../lib/config";
import { useHierarchyTree } from "../../lib/hierarchy";
import { ApiError, getListener, getTags } from "../../lib/inventory";
import { CONFIG_ROUTES, ConfigurationOutletContext } from "./ConfigurationLayout";

interface WireError {
  key: string;
  code: string;
  message: string;
}

export function ConfigEditorPage({ level }: { level: EntityLevel }) {
  const params = useParams();
  const { tab } = useOutletContext<ConfigurationOutletContext>();
  const tree = useHierarchyTree(CONFIG_ROUTES);
  const rawId =
    level === "organization"
      ? (tree.org?.id ?? "")
      : level === "listener"
        ? decodeURIComponent(params.mac ?? "")
        : (params[`${level}Id`] ?? "");

  const listener = useQuery({
    queryKey: ["listener", rawId],
    queryFn: () => getListener(rawId),
    enabled: level === "listener" && rawId !== "",
  });

  // The branch this entity sits on, resolved from the shared tree data.
  const branch = useMemo(() => {
    const rungs: ChainRung[] = [];
    if (!tree.org) {
      return { rungs, deploymentId: null as string | null, name: "", found: false };
    }
    rungs.push({ level: "organization", id: tree.org.id, label: tree.org.name });
    if (level === "organization") {
      return { rungs, deploymentId: null, name: tree.org.name, found: true };
    }
    const pod =
      level === "pod"
        ? tree.pods.find((row) => row.id === rawId)
        : level === "aggregator"
          ? tree.pods.find((row) => row.aggregator?.id === rawId)
          : level === "listener" && listener.data
            ? tree.pods.find((row) => row.aggregator?.id === listener.data.aggregator_id)
            : undefined;
    const deploymentId =
      level === "deployment"
        ? rawId
        : (pod?.deployment_id ??
          (level === "listener" ? (listener.data?.deployment_id ?? null) : null));
    const deployment = tree.deployments.find((row) => row.id === deploymentId);
    if (deployment) {
      rungs.push({ level: "deployment", id: deployment.id, label: deployment.name });
    }
    if (level === "deployment") {
      return { rungs, deploymentId, name: deployment?.name ?? "", found: Boolean(deployment) };
    }
    if (pod) {
      rungs.push({ level: "pod", id: pod.id, label: pod.name });
    }
    if (level === "pod") {
      return { rungs, deploymentId, name: pod?.name ?? "", found: Boolean(pod) };
    }
    if (pod?.aggregator) {
      rungs.push({
        level: "aggregator",
        id: pod.aggregator.id,
        label: pod.aggregator.name ?? pod.aggregator.aggregator_uuid,
      });
    }
    if (level === "aggregator") {
      return {
        rungs,
        deploymentId,
        name: pod?.aggregator?.name ?? pod?.aggregator?.aggregator_uuid ?? "",
        found: Boolean(pod?.aggregator),
      };
    }
    if (listener.data) {
      rungs.push({ level: "listener", id: listener.data.mac, label: listener.data.name });
    }
    return {
      rungs,
      deploymentId,
      name: listener.data?.name ?? rawId,
      found: Boolean(listener.data),
    };
  }, [tree.org, tree.deployments, tree.pods, level, rawId, listener.data]);

  const entity = ENTITY_PATHS[level];
  const canEdit = useCan("manage_config", branch.deploymentId);

  const catalog = useQuery({ queryKey: ["config", "catalog"], queryFn: getCatalog });
  const effective = useQuery({
    queryKey: ["config", "effective", entity, rawId],
    queryFn: () => getEffectiveConfig(entity, rawId),
    enabled: rawId !== "",
  });
  const overrides = useQuery({
    queryKey: ["config", "overrides", entity, rawId],
    queryFn: () => getOverrides(entity, rawId),
    enabled: rawId !== "",
  });

  const [staged, setStaged] = useState<Map<string, unknown | typeof REVERT>>(new Map());
  const [errors, setErrors] = useState<Record<string, string>>({});
  const [onlyOverridden, setOnlyOverridden] = useState(false);
  const queryClient = useQueryClient();

  const save = useMutation({
    mutationFn: () =>
      putOverrides(entity, rawId, buildDraftPut(overrides.data?.overrides ?? {}, staged)),
    onSuccess: () => {
      setStaged(new Map());
      setErrors({});
      void queryClient.invalidateQueries({ queryKey: ["config"] });
    },
    onError: (error) => {
      if (error instanceof ApiError && error.detail && typeof error.detail === "object") {
        const wire = (error.detail as { errors?: WireError[] }).errors ?? [];
        setErrors(Object.fromEntries(wire.map((item) => [item.key, item.message])));
      }
    },
  });

  if (tree.isLoading || (level === "listener" && listener.isLoading)) {
    return (
      <div data-testid="config-loading">
        <div className="skeleton skeleton-row" />
        <p className="skeleton-caption">Loading configuration · layout holds final geometry</p>
      </div>
    );
  }
  if (!branch.found || rawId === "") {
    return (
      <EmptyState title="Not found" testId="config-missing">
        This entity does not exist, or it sits outside your assigned scope.
      </EmptyState>
    );
  }

  const byKey = Object.fromEntries(
    (catalog.data?.items ?? []).map((item) => [item.key, item]),
  ) as Record<string, CatalogKey>;
  const stagedCount = staged.size;
  const catalogVersion = catalog.data?.version;

  return (
    <div data-testid="config-editor">
      <PageHeader eyebrow={level} title="Configuration">
        {tab === "Settings" && canEdit && (
          <>
            <button
              type="button"
              className="btn-tertiary"
              disabled={stagedCount === 0}
              onClick={() => {
                setStaged(new Map());
                setErrors({});
              }}
            >
              Discard draft
            </button>
            <button
              type="button"
              className="btn-secondary"
              disabled={stagedCount === 0 || save.isPending}
              data-testid="save-draft"
              onClick={() => save.mutate()}
            >
              Save draft
            </button>
            <button type="button" disabled title="Publishing arrives with E3 (EOE_PUBLISH_ENABLED)">
              Publish revision
            </button>
          </>
        )}
      </PageHeader>
      <p className="scope-caption">
        <span className="level-badge">{level} level</span> <span>{branch.name}</span>
      </p>
      {!canEdit && (
        <p className="config-locked" data-testid="config-locked" id="config-locked-note">
          Configuration is read-only for your role. Editing needs manage_config access
          {branch.deploymentId ? " in this deployment." : " organization-wide."}
        </p>
      )}

      {tab === "Settings" && (
        <>
          <DraftBanner count={stagedCount} />
          <div
            className="config-editor"
            aria-describedby={canEdit ? undefined : "config-locked-note"}
          >
            <div className="config-main">
              <label className="config-filter">
                <ToggleSwitch
                  checked={onlyOverridden}
                  onChange={setOnlyOverridden}
                  label="Only overridden"
                />
                <span>Only overridden</span>
              </label>
              {catalog.data && effective.data && overrides.data ? (
                <ProvenanceTable
                  catalog={catalog.data.items}
                  effective={effective.data}
                  overrides={overrides.data.overrides}
                  staged={staged}
                  level={level}
                  canEdit={canEdit}
                  onlyOverridden={onlyOverridden}
                  errors={errors}
                  onStage={(key, value) => {
                    setStaged((current) => new Map(current).set(key, value));
                  }}
                  onRevert={(key) => {
                    setStaged((current) => {
                      const next = new Map(current);
                      if (key in (overrides.data?.overrides ?? {})) {
                        next.set(key, REVERT); // saved override: removal must reach the PUT
                      } else {
                        next.delete(key); // staged-only edit: just forget it
                      }
                      return next;
                    });
                  }}
                />
              ) : (
                <div className="skeleton skeleton-row" />
              )}
              {catalogVersion !== undefined && (
                <p className="config-footer muted">
                  {Object.keys(effective.data?.config ?? {}).length} keys resolved · catalog schema
                  v{catalogVersion}
                </p>
              )}
              {save.isError && !Object.keys(errors).length && (
                <p className="form-error">{(save.error as Error).message}</p>
              )}
            </div>
            <aside className="config-rail">
              <InheritanceChain
                rungs={branch.rungs}
                current={level}
                descendantsNote={descendantsNote(level, tree, rawId)}
              />
              <section className="card" data-testid="draft-diff-card">
                <h2>Draft changes</h2>
                <DraftDiff
                  entries={[...staged.entries()].map(([key, next]) => ({
                    key,
                    old: effective.data?.config[key]?.value,
                    next,
                    secret: byKey[key]?.secret ?? false,
                  }))}
                  byKey={byKey}
                />
              </section>
            </aside>
          </div>
        </>
      )}

      {tab === "Tags" && (
        <ConfigTags entity={entity} id={rawId} deploymentId={branch.deploymentId} />
      )}

      {tab === "Revisions" &&
        (level === "aggregator" || level === "listener" ? (
          <RevisionsPane entity={entity as "aggregators" | "listeners"} id={rawId} />
        ) : (
          <EmptyState title="Revisions are per-device" testId="revisions-not-device">
            Desired config snapshots target aggregators and listeners (spec 6.1) — pick a device in
            the tree to see its drafts.
          </EmptyState>
        ))}
    </div>
  );
}

function descendantsNote(
  level: EntityLevel,
  tree: ReturnType<typeof useHierarchyTree>,
  id: string,
): string | undefined {
  if (level === "pod") {
    const pod = tree.pods.find((row) => row.id === id);
    return pod ? `${pod.listener_count} listeners inherit from here.` : undefined;
  }
  if (level === "deployment") {
    const deployment = tree.deployments.find((row) => row.id === id);
    return deployment
      ? `${deployment.pod_count} pods · ${deployment.listener_count} listeners inherit from here.`
      : undefined;
  }
  if (level === "organization") {
    return "Every deployment inherits from here.";
  }
  return undefined;
}

function ConfigTags({
  entity,
  id,
  deploymentId,
}: {
  entity: ReturnType<typeof String> & string;
  id: string;
  deploymentId: string | null;
}) {
  const tags = useQuery({
    queryKey: ["tags", entity, id],
    queryFn: () => getTags(entity as never, id),
  });
  if (!tags.data) {
    return <div className="skeleton skeleton-row" />;
  }
  return (
    <TagEditor entity={entity as never} id={id} tags={tags.data.tags} deploymentId={deploymentId} />
  );
}

function RevisionsPane({ entity, id }: { entity: "aggregators" | "listeners"; id: string }) {
  const revisions = useQuery({
    queryKey: ["config", "revisions", entity, id],
    queryFn: () => listRevisions(entity, id),
  });
  if (!revisions.data) {
    return <div className="skeleton skeleton-row" />;
  }
  if (revisions.data.total === 0) {
    return (
      <EmptyState title="No revisions yet" testId="revisions-empty">
        Draft revisions appear here when a config change is applied; publication to devices arrives
        with E3.
      </EmptyState>
    );
  }
  return (
    <div className="data-table-wrap">
      <table className="data-table" data-testid="revisions-table">
        <thead>
          <tr>
            <th scope="col">Created</th>
            <th scope="col">State</th>
            <th scope="col">Checksum</th>
            <th scope="col">Schema</th>
          </tr>
        </thead>
        <tbody>
          {revisions.data.items.map((row) => (
            <tr key={row.id}>
              <td>{row.created_at}</td>
              <td>{row.state}</td>
              <td className="mono">{row.checksum.slice(0, 18)}…</td>
              <td className="mono">v{row.schema_version}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
