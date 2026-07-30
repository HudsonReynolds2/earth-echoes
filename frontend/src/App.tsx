import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { Route, Routes } from "react-router-dom";

import { Shell } from "./components/Shell";
import { Configuration } from "./pages/Configuration";
import { Inventory } from "./pages/Inventory";
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
          <Route path="inventory" element={<Inventory />} />
          <Route path="configuration" element={<Configuration />} />
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
