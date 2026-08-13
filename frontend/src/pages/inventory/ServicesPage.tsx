/**
 * Services onboarding — Path A (task E5.12a; spec 16.2, 16.3, screen S5).
 *
 * The operator enters credentials for services that already exist, saves them,
 * and tests each one. Path B — the generated stack — is E5.12b and lands on
 * this same page.
 *
 * **The page nests in the inventory frame rather than building a second app
 * shell.** S5 draws its own top bar because the mock renders the whole
 * application; the real one already has a top bar, a role badge and a
 * hierarchy rail, and a wizard that reproduced them would be a second frame to
 * keep in sync.
 *
 * **Permissions are phase-5 fixed choice 9.** `VIEW_SERVICES` reaches all four
 * roles, so status renders for everyone; `MANAGE_SERVICES` is Owner and
 * Deployment Operator only, so a viewer and a field tech see the state of
 * every service and no Save, no Test and no Generate.
 */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { useParams } from "react-router-dom";

import { EmptyState } from "../../components/EmptyState";
import { PageHeader } from "../../components/PageHeader";
import { ServiceForm } from "../../components/ServiceForm";
import { ServiceResultRow } from "../../components/ServiceResultRow";
import { useCan } from "../../components/Can";
import { getDeployment } from "../../lib/inventory";
import {
  SERVICE_SCHEMA,
  ServiceKey,
  ServiceSettingsIn,
  TestResult,
  getServices,
  getServicesStatus,
  putServices,
  servicesKey,
  servicesStatusKey,
  testServices,
} from "../../lib/services";

export function ServicesPage() {
  const { deploymentId = "" } = useParams();
  const canManage = useCan("manage_services", deploymentId);
  const queryClient = useQueryClient();
  /** This session's test output per service. Not persisted and not refetched:
   * the row's own status is the verdict of record, and these are the checks
   * that produced it. */
  const [results, setResults] = useState<Record<string, TestResult>>({});
  const [busy, setBusy] = useState<ServiceKey | null>(null);
  const [errors, setErrors] = useState<Record<string, string>>({});

  const deployment = useQuery({
    queryKey: ["deployment", deploymentId],
    queryFn: () => getDeployment(deploymentId),
  });
  const services = useQuery({
    queryKey: servicesKey(deploymentId),
    queryFn: () => getServices(deploymentId),
  });
  const status = useQuery({
    queryKey: servicesStatusKey(deploymentId),
    queryFn: () => getServicesStatus(deploymentId),
  });

  const refresh = () =>
    Promise.all([
      queryClient.invalidateQueries({ queryKey: servicesKey(deploymentId) }),
      queryClient.invalidateQueries({ queryKey: servicesStatusKey(deploymentId) }),
    ]);

  const save = useMutation({
    mutationFn: (input: { key: ServiceKey; settings: ServiceSettingsIn }) =>
      putServices(deploymentId, { [input.key]: input.settings }),
    onMutate: (input) => {
      setBusy(input.key);
      setErrors((current) => ({ ...current, [input.key]: "" }));
    },
    onSuccess: () => void refresh(),
    onError: (error: Error, input) =>
      setErrors((current) => ({ ...current, [input.key]: error.message })),
    onSettled: () => setBusy(null),
  });

  const test = useMutation({
    mutationFn: (input: { key: ServiceKey; settings?: ServiceSettingsIn }) =>
      testServices(deploymentId, input.settings ? { [input.key]: input.settings } : {}),
    onMutate: (input) => {
      setBusy(input.key);
      setErrors((current) => ({ ...current, [input.key]: "" }));
    },
    onSuccess: (data) => {
      setResults((current) => {
        const next = { ...current };
        for (const result of data.results) {
          next[result.service_key] = result;
        }
        return next;
      });
      void refresh();
    },
    onError: (error: Error, input) =>
      setErrors((current) => ({ ...current, [input.key]: error.message })),
    onSettled: () => setBusy(null),
  });

  if (services.isLoading || deployment.isLoading) {
    return (
      <div data-testid="services-loading">
        <div className="skeleton skeleton-row" />
        <div className="skeleton skeleton-row" />
        <p className="skeleton-caption">Loading services · layout holds final geometry</p>
      </div>
    );
  }
  if (services.isError || !services.data) {
    return (
      <EmptyState title="Services unavailable" testId="services-missing">
        {(services.error as Error | null)?.message ??
          "This deployment's services could not be loaded."}
      </EmptyState>
    );
  }

  const rows = services.data.services;
  return (
    <>
      <PageHeader eyebrow="Services onboarding" title={deployment.data?.name ?? "Deployment"} />
      <p className="scope-caption">
        Spec 16.2's five services for this deployment. Credentials are <strong>write-only</strong>:
        once saved, the platform will never show one again — not here, not in an API response, not
        in a log line.
      </p>

      <div className="service-list">
        {SERVICE_SCHEMA.map((descriptor) => (
          <section className="card" key={descriptor.key} data-testid={`service-${descriptor.key}`}>
            <header className="service-card-head">
              <h2>{descriptor.label}</h2>
              <p className="muted">{descriptor.blurb}</p>
            </header>

            <ServiceResultRow
              descriptor={descriptor}
              service={rows[descriptor.key]}
              status={status.data?.services[descriptor.key]}
              result={results[descriptor.key]}
              canManage={canManage}
              testing={busy === descriptor.key && test.isPending}
              onRetest={() => test.mutate({ key: descriptor.key })}
            />

            <ServiceForm
              descriptor={descriptor}
              service={rows[descriptor.key]}
              canManage={canManage}
              saving={busy === descriptor.key && save.isPending}
              testing={busy === descriptor.key && test.isPending}
              error={errors[descriptor.key] || null}
              onSave={(settings) => save.mutate({ key: descriptor.key, settings })}
              onTest={(settings) => test.mutate({ key: descriptor.key, settings })}
            />
          </section>
        ))}
      </div>
    </>
  );
}
