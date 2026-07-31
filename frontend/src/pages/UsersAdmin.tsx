/**
 * User administration page (task E0.9), owner-only behind <Can>. Token-styled.
 */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { FormEvent, useState } from "react";

import { Can } from "../components/Can";
import { EmptyState } from "../components/EmptyState";
import { PageHeader } from "../components/PageHeader";
import { createUser, listUsers, setUserActive } from "../lib/users";

const ROLES = ["owner", "deployment_operator", "field_tech", "viewer"] as const;

function UsersTable() {
  const queryClient = useQueryClient();
  const { data, isPending, isError } = useQuery({ queryKey: ["users"], queryFn: listUsers });
  const toggle = useMutation({
    mutationFn: ({ id, active }: { id: string; active: boolean }) => setUserActive(id, active),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["users"] }),
  });

  if (isPending) {
    return <p data-testid="users-loading">Loading users...</p>;
  }
  if (isError) {
    return (
      <p className="status-bad" data-testid="users-error">
        Could not load users
      </p>
    );
  }
  return (
    <table className="admin-table" data-testid="users-table">
      <thead>
        <tr>
          <th>Email</th>
          <th>Roles</th>
          <th>Status</th>
          <th></th>
        </tr>
      </thead>
      <tbody>
        {data.items.map((user) => (
          <tr key={user.id}>
            <td>{user.email}</td>
            <td className="muted">
              {user.assignments
                .map((a) => (a.deployment_id ? `${a.role} @ ${a.deployment_id}` : a.role))
                .join(", ") || "none"}
            </td>
            <td>
              <span className={user.is_active ? "status-ok" : "status-bad"}>
                {user.is_active ? "active" : "inactive"}
              </span>
            </td>
            <td>
              <button
                type="button"
                onClick={() => toggle.mutate({ id: user.id, active: !user.is_active })}
              >
                {user.is_active ? "Deactivate" : "Activate"}
              </button>
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function CreateUserForm() {
  const queryClient = useQueryClient();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [role, setRole] = useState<(typeof ROLES)[number]>("viewer");
  const [deploymentId, setDeploymentId] = useState("");
  const [error, setError] = useState<string | null>(null);

  const create = useMutation({
    mutationFn: createUser,
    onSuccess: () => {
      setEmail("");
      setPassword("");
      setError(null);
      queryClient.invalidateQueries({ queryKey: ["users"] });
    },
    onError: (cause) => setError(cause instanceof Error ? cause.message : "Create failed"),
  });

  function onSubmit(event: FormEvent) {
    event.preventDefault();
    create.mutate({
      email,
      password,
      role,
      deployment_id: deploymentId.trim() === "" ? null : deploymentId.trim(),
    });
  }

  return (
    <form onSubmit={onSubmit} className="auth-form" data-testid="create-user-form">
      <h2>Create user</h2>
      <label>
        Email
        <input
          type="email"
          value={email}
          onChange={(event) => setEmail(event.target.value)}
          required
        />
      </label>
      <label>
        Password
        <input
          type="password"
          value={password}
          onChange={(event) => setPassword(event.target.value)}
          required
        />
      </label>
      <label>
        Role
        <select
          value={role}
          onChange={(event) => setRole(event.target.value as (typeof ROLES)[number])}
        >
          {ROLES.map((value) => (
            <option key={value} value={value}>
              {value}
            </option>
          ))}
        </select>
      </label>
      <label>
        Deployment scope (blank = organization-wide)
        <input value={deploymentId} onChange={(event) => setDeploymentId(event.target.value)} />
      </label>
      {error && (
        <p className="status-bad" data-testid="create-user-error">
          {error}
        </p>
      )}
      <button type="submit" disabled={create.isPending}>
        {create.isPending ? "Creating..." : "Create user"}
      </button>
    </form>
  );
}

export function UsersAdmin() {
  return (
    <div className="page">
      <PageHeader eyebrow="Administration" title="Users" />
      <Can
        permission="manage_users"
        fallback={
          <EmptyState title="Not permitted" testId="users-denied">
            You do not have permission to administer users.
          </EmptyState>
        }
      >
        <section className="card">
          <UsersTable />
        </section>
        <section className="card">
          <CreateUserForm />
        </section>
      </Can>
    </div>
  );
}
