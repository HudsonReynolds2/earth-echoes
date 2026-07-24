import { useQuery } from "@tanstack/react-query";

import { fetchMe } from "./auth";

export function useMe() {
  return useQuery({ queryKey: ["me"], queryFn: fetchMe, retry: false, staleTime: 30_000 });
}
