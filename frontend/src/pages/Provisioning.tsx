/**
 * Provisioning: the generation wizard and the bundle tracking board (task
 * E4.10; spec 8; screen S6). Replaces the E0 `EmptyState` placeholder.
 *
 * A viewer sees the deployment's bundles and their boards, and nothing that
 * changes anything (no wizard, no download, no "mark flashed"). That is
 * `useCan`, the same gate the API applies. A field tech gets the whole page
 * for the deployments their role is scoped to.
 */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useMemo, useState } from "react";

import { BundleBoard, FillIn } from "../components/BundleBoard";
import { BundleWizard } from "../components/BundleWizard";
import { useCan } from "../components/Can";
import { EmptyState } from "../components/EmptyState";
import { PageHeader } from "../components/PageHeader";
import { listDeployments, listListeners, listPods, patchListener } from "../lib/inventory";
import {
  BOARD_REFRESH_MS,
  BundleDetail,
  bundleKey,
  bundlesKey,
  downloadBundle,
  getBundle,
  listBundles,
  markFlashed,
} from "../lib/provisioning";

export function Provisioning() {
  const queryClient = useQueryClient();
  const deployments = useQuery({
    queryKey: ["deployments", "provisioning"],
    queryFn: () => listDeployments({ limit: 500, sort: "name" }),
  });
  const [deploymentId, setDeploymentId] = useState("");
  const [wizardOpen, setWizardOpen] = useState(false);
  const [bundleId, setBundleId] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  useEffect(() => {
    const first = deployments.data?.items[0];
    if (!deploymentId && first) setDeploymentId(first.id);
  }, [deployments.data, deploymentId]);

  const canManage = useCan("manage_provisioning", deploymentId || null);
  // Filling in name and GPS writes inventory through E1's endpoint, so it is
  // E1's permission that decides who may (spec 8.6; phase-4 E4.11).
  const canEditDevices = useCan("manage_devices", deploymentId || null);
  const deployment = deployments.data?.items.find((d) => d.id === deploymentId);

  const pods = useQuery({
    queryKey: ["pods", "deployment", deploymentId],
    queryFn: () => listPods({ deployment_id: deploymentId, limit: 500, sort: "name" }),
    enabled: deploymentId !== "",
  });
  const listeners = useQuery({
    queryKey: ["listeners", "deployment", deploymentId],
    queryFn: () => listListeners({ deployment_id: deploymentId, limit: 500 }),
    enabled: deploymentId !== "",
  });
  const bundles = useQuery({
    queryKey: bundlesKey(deploymentId),
    queryFn: () => listBundles(deploymentId),
    enabled: deploymentId !== "",
  });

  useEffect(() => {
    const newest = bundles.data?.items[0];
    if (!newest) return;
    if (!bundleId || !bundles.data?.items.some((b) => b.id === bundleId)) setBundleId(newest.id);
  }, [bundles.data, bundleId]);

  const bundle = useQuery({
    queryKey: bundleKey(bundleId ?? ""),
    queryFn: () => getBundle(bundleId ?? ""),
    enabled: bundleId !== null,
    // The board fills in by itself as devices come online (spec 8.5).
    refetchInterval: BOARD_REFRESH_MS,
  });

  const podNames = useMemo(
    () => Object.fromEntries((pods.data?.items ?? []).map((pod) => [pod.id, pod.name])),
    [pods.data],
  );
  const listenersByMac = useMemo(
    () => Object.fromEntries((listeners.data?.items ?? []).map((l) => [l.mac, l])),
    [listeners.data],
  );

  const mark = useMutation({
    mutationFn: async (recordIds: string[]) => {
      for (const id of recordIds) await markFlashed(id);
      return recordIds.length;
    },
    onSuccess: async (count) => {
      setNotice(`${count} card${count === 1 ? "" : "s"} marked flashed.`);
      await queryClient.invalidateQueries({ queryKey: bundleKey(bundleId ?? "") });
    },
    onError: (error) => setNotice(error instanceof Error ? error.message : String(error)),
  });

  const download = useMutation({
    mutationFn: () => downloadBundle(bundleId ?? ""),
    onSuccess: ({ blob, filename }) => {
      const url = URL.createObjectURL(blob);
      const link = document.createElement("a");
      link.href = url;
      link.download = filename;
      link.click();
      URL.revokeObjectURL(url);
    },
    onError: (error) => setNotice(error instanceof Error ? error.message : String(error)),
  });

  async function fillIn(mac: string, input: FillIn) {
    await patchListener(mac, input);
    await queryClient.invalidateQueries({ queryKey: ["listeners", "deployment", deploymentId] });
    setNotice(`Saved ${input.name}.`);
  }

  async function generated(created: BundleDetail) {
    setWizardOpen(false);
    setNotice("Bundle generated. Download it, write the cards, then mark them flashed.");
    queryClient.setQueryData(bundleKey(created.id), created);
    await queryClient.invalidateQueries({ queryKey: bundlesKey(deploymentId) });
    setBundleId(created.id);
  }

  if (deployments.data && deployments.data.items.length === 0) {
    return (
      <div className="page">
        <PageHeader eyebrow="Field operations" title="Provisioning" />
        <EmptyState title="No deployments yet" testId="provisioning-empty">
          Create a deployment and verify its services under Inventory before provisioning cards.
        </EmptyState>
      </div>
    );
  }

  return (
    <div className="page provisioning-page">
      <PageHeader eyebrow="Field operations" title="Provisioning" />
      <div className="provisioning-toolbar">
        <div className="form-field">
          <label htmlFor="provisioning-deployment">Deployment</label>
          <select
            id="provisioning-deployment"
            value={deploymentId}
            onChange={(event) => {
              setDeploymentId(event.target.value);
              setBundleId(null);
              setWizardOpen(false);
              setNotice(null);
            }}
          >
            {deployments.data?.items.map((d) => (
              <option key={d.id} value={d.id}>
                {d.name}
              </option>
            ))}
          </select>
        </div>
        {canManage && !wizardOpen && (
          <button type="button" onClick={() => setWizardOpen(true)} disabled={!pods.data}>
            New bundle
          </button>
        )}
      </div>

      {wizardOpen && deployment && pods.data && (
        <BundleWizard
          deploymentId={deployment.id}
          deploymentName={deployment.name}
          pods={pods.data.items}
          onGenerated={generated}
          onCancel={() => setWizardOpen(false)}
        />
      )}

      {bundles.data && bundles.data.items.length === 0 && !wizardOpen && (
        <EmptyState title="No bundles yet" testId="provisioning-empty">
          {canManage
            ? "Generate one with New bundle. Its tracking board appears here."
            : "No bundles have been generated for this deployment."}
        </EmptyState>
      )}

      {bundles.data && bundles.data.items.length > 1 && (
        <div className="form-field bundle-picker">
          <label htmlFor="provisioning-bundle">Bundle</label>
          <select
            id="provisioning-bundle"
            value={bundleId ?? ""}
            onChange={(event) => {
              setBundleId(event.target.value);
              setNotice(null);
            }}
          >
            {bundles.data.items.map((b) => (
              <option key={b.id} value={b.id}>
                {new Date(b.generated_at).toLocaleString()} ·{" "}
                {b.scope_type === "pod" ? (podNames[b.scope_id] ?? "Pod") : "Whole deployment"} ·{" "}
                {b.record_count} cards
              </option>
            ))}
          </select>
        </div>
      )}

      {bundle.data && (
        <BundleBoard
          bundle={bundle.data}
          podNames={podNames}
          listeners={listenersByMac}
          canManage={canManage}
          canEditDevices={canEditDevices}
          marking={mark.isPending}
          downloading={download.isPending}
          notice={notice}
          onMarkFlashed={(ids) => mark.mutate(ids)}
          onDownload={() => download.mutate()}
          onFillIn={fillIn}
        />
      )}
    </div>
  );
}
