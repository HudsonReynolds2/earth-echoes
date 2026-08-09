import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { Route, Routes } from "react-router-dom";

import { Shell } from "./components/Shell";
import { ConfigEditorPage } from "./pages/configuration/ConfigEditorPage";
import { ConfigurationLayout } from "./pages/configuration/ConfigurationLayout";
import { DeploymentLevel } from "./pages/inventory/DeploymentLevel";
import { ImportPage } from "./pages/inventory/ImportPage";
import { InventoryLayout } from "./pages/inventory/InventoryLayout";
import { ListenerDetail } from "./pages/inventory/ListenerDetail";
import { OrganizationLevel } from "./pages/inventory/OrganizationLevel";
import { PodLevel } from "./pages/inventory/PodLevel";
import { Login } from "./pages/Login";
import { Map } from "./pages/Map";
import { NotFound } from "./pages/NotFound";
import { Overview } from "./pages/Overview";
import { Provisioning } from "./pages/Provisioning";
import { SystemStatus } from "./pages/SystemStatus";
import { UsersAdmin } from "./pages/UsersAdmin";

export function App({ queryClient }: { queryClient?: QueryClient }) {
  const client = queryClient ?? new QueryClient();
  return (
    <QueryClientProvider client={client}>
      <Routes>
        <Route element={<Shell />}>
          <Route index element={<Overview />} />
          <Route path="map" element={<Map />} />
          <Route path="inventory" element={<InventoryLayout />}>
            <Route index element={<OrganizationLevel />} />
            <Route path="deployments/:deploymentId" element={<DeploymentLevel />} />
            <Route path="pods/:podId" element={<PodLevel />} />
            <Route path="listeners/:mac" element={<ListenerDetail />} />
            <Route path="import" element={<ImportPage />} />
          </Route>
          <Route path="configuration" element={<ConfigurationLayout />}>
            <Route index element={<ConfigEditorPage level="organization" />} />
            <Route
              path="deployments/:deploymentId"
              element={<ConfigEditorPage level="deployment" />}
            />
            <Route path="pods/:podId" element={<ConfigEditorPage level="pod" />} />
            <Route
              path="aggregators/:aggregatorId"
              element={<ConfigEditorPage level="aggregator" />}
            />
            <Route path="listeners/:mac" element={<ConfigEditorPage level="listener" />} />
          </Route>
          <Route path="provisioning" element={<Provisioning />} />
          <Route path="system" element={<SystemStatus />} />
          <Route path="users" element={<UsersAdmin />} />
          <Route path="login" element={<Login />} />
          <Route path="*" element={<NotFound />} />
        </Route>
      </Routes>
    </QueryClientProvider>
  );
}
