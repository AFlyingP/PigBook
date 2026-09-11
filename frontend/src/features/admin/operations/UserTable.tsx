import { useState } from "react";
import {
  Box,
  Typography,
  Button,
  Table,
  TableBody,
  TableCell,
  TableContainer,
  TableHead,
  TableRow,
  Paper,
  Chip,
  FormControl,
  InputLabel,
  Select,
  MenuItem,
  TablePagination,
  Alert,
  CircularProgress,
} from "@mui/material";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { request, ApiError } from "../../../api/client";
import type { components } from "../../../api/schema";
import { InvitationDialog } from "./InvitationDialog";

type User = components["schemas"]["User"];
type PageUser = components["schemas"]["Page_User_"];
type UserPatch = components["schemas"]["UserPatch"];

export function UserTable() {
  const queryClient = useQueryClient();
  const [page, setPage] = useState(0);
  const [rowsPerPage, setRowsPerPage] = useState(25);
  const [enabledFilter, setEnabledFilter] = useState<string>("all");
  const [announcement, setAnnouncement] = useState("");
  const [actionError, setActionError] = useState<string | null>(null);
  const [actionWarning, setActionWarning] = useState<string | null>(null);
  const [inFlightUserId, setInFlightUserId] = useState<string | null>(null);

  const [inviteOpen, setInviteOpen] = useState(false);

  const queryParams = new URLSearchParams({
    limit: String(rowsPerPage),
    offset: String(page * rowsPerPage),
  });
  if (enabledFilter === "enabled") {
    queryParams.set("enabled", "true");
  } else if (enabledFilter === "disabled") {
    queryParams.set("enabled", "false");
  }

  const { data, isLoading, error, refetch } = useQuery<PageUser, ApiError>({
    queryKey: ["admin", "users", { page, rowsPerPage, enabledFilter }],
    queryFn: async () => {
      const res = await request<PageUser>(`/api/v1/admin/users?${queryParams.toString()}`);
      return res.data;
    },
  });

  const handleToggleEnabled = async (targetUser: User) => {
    setInFlightUserId(targetUser.id);
    setActionError(null);
    setActionWarning(null);

    const payload: UserPatch = {
      enabled: !targetUser.enabled,
    };

    try {
      await request<User>(`/api/v1/admin/users/${targetUser.id}`, {
        method: "PATCH",
        headers: {
          "If-Match": `"${targetUser.version}"`,
          "Content-Type": "application/json",
        },
        body: payload,
      });

      queryClient.invalidateQueries({ queryKey: ["admin", "users"] });
      setAnnouncement(
        `User ${targetUser.display_name} has been ${!targetUser.enabled ? "enabled" : "disabled"}.`
      );
    } catch (err) {
      if (err instanceof ApiError) {
        if (err.status === 409 && err.code === "LAST_ADMIN") {
          // Keep previous state visible and show accurate error
          setActionError(
            "Cannot disable the last active administrator in the system (LAST_ADMIN)."
          );
        } else if (err.status === 412) {
          setActionWarning(
            "Another update occurred to this user account. Reloading latest user details..."
          );
          refetch();
        } else {
          setActionError(err.message || "Failed to update user status.");
        }
      } else {
        setActionError("A network or unexpected error occurred.");
      }
    } finally {
      setInFlightUserId(null);
    }
  };

  const handleToggleRole = async (targetUser: User) => {
    setInFlightUserId(targetUser.id);
    setActionError(null);
    setActionWarning(null);

    const newRole = targetUser.role === "admin" ? "member" : "admin";
    const payload: UserPatch = {
      role: newRole,
    };

    try {
      await request<User>(`/api/v1/admin/users/${targetUser.id}`, {
        method: "PATCH",
        headers: {
          "If-Match": `"${targetUser.version}"`,
          "Content-Type": "application/json",
        },
        body: payload,
      });

      queryClient.invalidateQueries({ queryKey: ["admin", "users"] });
      setAnnouncement(`User ${targetUser.display_name} role updated to ${newRole}.`);
    } catch (err) {
      if (err instanceof ApiError) {
        if (err.status === 409 && err.code === "LAST_ADMIN") {
          // Keep previous role visible and show accurate error
          setActionError(
            "Cannot demote the last active administrator in the system (LAST_ADMIN)."
          );
        } else if (err.status === 412) {
          setActionWarning(
            "Another update occurred to this user account. Reloading latest user details..."
          );
          refetch();
        } else {
          setActionError(err.message || "Failed to update user role.");
        }
      } else {
        setActionError("A network or unexpected error occurred.");
      }
    } finally {
      setInFlightUserId(null);
    }
  };

  return (
    <Box sx={{ width: "100%" }}>
      {/* Polite live region */}
      <Box
        role="status"
        aria-live="polite"
        sx={{
          position: "absolute",
          width: "1px",
          height: "1px",
          margin: "-1px",
          padding: 0,
          overflow: "hidden",
          clip: "rect(0, 0, 0, 0)",
          border: 0,
        }}
      >
        {announcement}
      </Box>

      <Box
        sx={{
          display: "flex",
          justifyContent: "space-between",
          alignItems: "center",
          flexWrap: "wrap",
          gap: 2,
          mb: 3,
        }}
      >
        <Box>
          <Typography variant="h5" component="h2" fontWeight="bold">
            User Accounts & Roles
          </Typography>
          <Typography variant="body2" color="text.secondary">
            Manage member accounts, permissions, role assignments, and invitations.
          </Typography>
        </Box>

        <Box sx={{ display: "flex", alignItems: "center", gap: 2 }}>
          <FormControl size="small" sx={{ minWidth: 160 }}>
            <InputLabel id="user-enabled-filter-label">Status Filter</InputLabel>
            <Select
              labelId="user-enabled-filter-label"
              id="user-enabled-filter"
              value={enabledFilter}
              label="Status Filter"
              onChange={(e) => {
                setEnabledFilter(e.target.value);
                setPage(0);
              }}
            >
              <MenuItem value="all">All Users</MenuItem>
              <MenuItem value="enabled">Enabled Only</MenuItem>
              <MenuItem value="disabled">Disabled Only</MenuItem>
            </Select>
          </FormControl>

          <Button
            variant="contained"
            color="primary"
            onClick={() => setInviteOpen(true)}
            id="invite-user-button"
          >
            Invite User
          </Button>
        </Box>
      </Box>

      {actionError && (
        <Alert severity="error" sx={{ mb: 2 }} role="alert">
          {actionError}
        </Alert>
      )}

      {actionWarning && (
        <Alert severity="warning" sx={{ mb: 2 }} role="alert">
          {actionWarning}
        </Alert>
      )}

      {error && (
        <Alert severity="error" sx={{ mb: 2 }} role="alert">
          {error.message || "Failed to load users."}
        </Alert>
      )}

      {isLoading ? (
        <Box sx={{ display: "flex", justifyContent: "center", py: 8 }}>
          <CircularProgress aria-label="Loading users..." />
        </Box>
      ) : (
        <Paper variant="outlined">
          <TableContainer>
            <Table aria-label="User directory table">
              <TableHead>
                <TableRow>
                  <TableCell><strong>Display Name</strong></TableCell>
                  <TableCell><strong>Email</strong></TableCell>
                  <TableCell><strong>Role</strong></TableCell>
                  <TableCell><strong>Status</strong></TableCell>
                  <TableCell><strong>Version</strong></TableCell>
                  <TableCell align="right"><strong>Actions</strong></TableCell>
                </TableRow>
              </TableHead>
              <TableBody>
                {data?.items && data.items.length > 0 ? (
                  data.items.map((u) => {
                    const isBusy = inFlightUserId === u.id;
                    return (
                      <TableRow key={u.id} hover>
                        <TableCell component="th" scope="row">
                          <Typography variant="body2" fontWeight="medium">
                            {u.display_name}
                          </Typography>
                          <Typography variant="caption" fontFamily="monospace" color="text.secondary">
                            {u.id}
                          </Typography>
                        </TableCell>
                        <TableCell>{u.email}</TableCell>
                        <TableCell>
                          <Chip
                            label={u.role === "admin" ? "Administrator" : "Member"}
                            size="small"
                            color={u.role === "admin" ? "secondary" : "default"}
                            variant="outlined"
                          />
                        </TableCell>
                        <TableCell>
                          <Chip
                            label={u.enabled ? "Enabled" : "Disabled"}
                            size="small"
                            color={u.enabled ? "success" : "default"}
                            variant="outlined"
                          />
                        </TableCell>
                        <TableCell>v{u.version}</TableCell>
                        <TableCell align="right">
                          <Box sx={{ display: "flex", justifyContent: "flex-end", gap: 1 }}>
                            <Button
                              size="small"
                              variant="outlined"
                              onClick={() => handleToggleRole(u)}
                              disabled={isBusy}
                              aria-label={`Change role for ${u.display_name}`}
                            >
                              {u.role === "admin" ? "Demote to Member" : "Promote to Admin"}
                            </Button>
                            <Button
                              size="small"
                              color={u.enabled ? "warning" : "success"}
                              variant="outlined"
                              onClick={() => handleToggleEnabled(u)}
                              disabled={isBusy}
                              aria-label={`${u.enabled ? "Disable" : "Enable"} ${u.display_name}`}
                            >
                              {u.enabled ? "Disable" : "Enable"}
                            </Button>
                          </Box>
                        </TableCell>
                      </TableRow>
                    );
                  })
                ) : (
                  <TableRow>
                    <TableCell colSpan={6} align="center" sx={{ py: 4 }}>
                      <Typography variant="body2" color="text.secondary">
                        No users found.
                      </Typography>
                    </TableCell>
                  </TableRow>
                )}
              </TableBody>
            </Table>
          </TableContainer>

          <TablePagination
            rowsPerPageOptions={[10, 25, 50]}
            component="div"
            count={data?.total || 0}
            rowsPerPage={rowsPerPage}
            page={page}
            onPageChange={(_, newPage) => setPage(newPage)}
            onRowsPerPageChange={(e) => {
              setRowsPerPage(parseInt(e.target.value, 10));
              setPage(0);
            }}
            aria-label="User table pagination"
          />
        </Paper>
      )}

      <InvitationDialog
        open={inviteOpen}
        onClose={() => setInviteOpen(false)}
        onInvited={() => {
          setAnnouncement("Invitation link created successfully.");
          refetch();
        }}
      />
    </Box>
  );
}
