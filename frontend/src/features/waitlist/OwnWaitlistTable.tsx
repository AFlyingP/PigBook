import React, { useState } from "react";
import {
  Box,
  Typography,
  Paper,
  Table,
  TableBody,
  TableCell,
  TableContainer,
  TableHead,
  TableRow,
  Chip,
  Button,
  Select,
  MenuItem,
  FormControl,
  InputLabel,
  Pagination,
  Dialog,
  DialogTitle,
  DialogContent,
  DialogContentText,
  DialogActions,
  Alert,
  CircularProgress,
  Skeleton,
} from "@mui/material";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { request, ApiError } from "../../api/client";
import { formatInNewYork } from "../resources/timeUtils";
import { OfferCard } from "./OfferCard";
import type { components } from "../../api/schema";

export type WaitEntry = components["schemas"]["WaitEntry"];
export type PageWaitEntry = components["schemas"]["Page_WaitEntry_"];
export type PageResource = components["schemas"]["Page_Resource_"];

export function OwnWaitlistTable() {
  const queryClient = useQueryClient();
  const [page, setPage] = useState(1);
  const limit = 10;
  const [statusFilter, setStatusFilter] = useState<string>("all");

  const [leaveTarget, setLeaveTarget] = useState<WaitEntry | null>(null);
  const [isLeaving, setIsLeaving] = useState(false);
  const [leaveError, setLeaveError] = useState<string | null>(null);
  const [announcement, setAnnouncement] = useState<string | null>(null);

  // Load resources for naming
  const { data: resourcesPage } = useQuery<PageResource>({
    queryKey: ["resources", "lookup"],
    queryFn: async () => {
      const res = await request<PageResource>("/api/v1/resources?limit=100");
      return res.data;
    },
  });

  const resourceMap = React.useMemo(() => {
    const map = new Map<string, string>();
    if (resourcesPage?.items) {
      for (const r of resourcesPage.items) {
        map.set(r.id, r.name);
      }
    }
    return map;
  }, [resourcesPage]);

  // Load user's waitlist entries
  const offset = (page - 1) * limit;
  const {
    data: waitlistPage,
    isLoading,
    isError,
    error,
    refetch,
  } = useQuery<PageWaitEntry>({
    queryKey: ["waitlist", "mine", { status: statusFilter, limit, offset }],
    queryFn: async () => {
      const params = new URLSearchParams({
        limit: String(limit),
        offset: String(offset),
      });
      if (statusFilter !== "all") {
        params.set("status", statusFilter);
      }
      const res = await request<PageWaitEntry>(`/api/v1/waitlist?${params.toString()}`);
      return res.data;
    },
  });

  // Offered entries to display as OfferCards at top
  const offeredEntries = React.useMemo(() => {
    if (!waitlistPage?.items) return [];
    return waitlistPage.items.filter((e) => e.status === "offered");
  }, [waitlistPage]);

  const handleOpenLeave = (entry: WaitEntry) => {
    setLeaveTarget(entry);
    setLeaveError(null);
  };

  const handleCloseLeave = () => {
    if (isLeaving) return;
    setLeaveTarget(null);
    setLeaveError(null);
  };

  const handleConfirmLeave = async () => {
    if (!leaveTarget) return;

    setIsLeaving(true);
    setLeaveError(null);

    try {
      // DELETE with If-Match from entry version (Spec 4.2 E15)
      await request<WaitEntry>(`/api/v1/waitlist/${leaveTarget.id}`, {
        method: "DELETE",
        headers: {
          "If-Match": `"${leaveTarget.version}"`,
        },
      });

      queryClient.invalidateQueries({ queryKey: ["waitlist", "mine"] });
      queryClient.invalidateQueries({ queryKey: ["resources"] });

      setAnnouncement("You have been removed from the waitlist.");
      handleCloseLeave();
    } catch (err) {
      if (err instanceof ApiError) {
        if (err.status === 412) {
          setLeaveError("This waitlist entry was already updated. Refreshing...");
          refetch();
        } else {
          setLeaveError(err.message || "Failed to remove entry from waitlist.");
        }
      } else {
        setLeaveError("Network error while removing entry.");
      }
    } finally {
      setIsLeaving(false);
    }
  };

  const getStatusChip = (status: string) => {
    switch (status) {
      case "waiting":
        return <Chip label="Waiting" color="primary" size="small" />;
      case "offered":
        return <Chip label="Offer Available" color="warning" size="small" />;
      case "accepted":
        return <Chip label="Accepted" color="success" size="small" />;
      case "cancelled":
        return <Chip label="Cancelled" color="default" size="small" />;
      case "expired":
        return <Chip label="Expired" color="error" size="small" />;
      default:
        return <Chip label={status} size="small" />;
    }
  };

  const totalPages = waitlistPage ? Math.ceil(waitlistPage.total / limit) : 1;

  return (
    <Box sx={{ py: 3 }}>
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
          <Typography
            component="h1"
            variant="h4"
            fontWeight="bold"
            id="my-waitlist-heading"
            tabIndex={-1}
            sx={{ outline: "none" }}
          >
            My Waitlist
          </Typography>
          <Typography variant="body2" color="text.secondary">
            View your waitlisted slots and active offers. All times shown in America/New_York.
          </Typography>
        </Box>

        <FormControl size="small" sx={{ minWidth: 160 }}>
          <InputLabel id="waitlist-status-filter-label">Filter by Status</InputLabel>
          <Select
            labelId="waitlist-status-filter-label"
            id="waitlist-status-filter"
            value={statusFilter}
            label="Filter by Status"
            onChange={(e) => {
              setStatusFilter(e.target.value);
              setPage(1);
            }}
          >
            <MenuItem value="all">All Entries</MenuItem>
            <MenuItem value="waiting">Waiting</MenuItem>
            <MenuItem value="offered">Offered</MenuItem>
            <MenuItem value="accepted">Accepted</MenuItem>
            <MenuItem value="cancelled">Cancelled</MenuItem>
            <MenuItem value="expired">Expired</MenuItem>
          </Select>
        </FormControl>
      </Box>

      {announcement && (
        <Alert severity="success" role="status" sx={{ mb: 3 }} onClose={() => setAnnouncement(null)}>
          {announcement}
        </Alert>
      )}

      {/* Prominent Active Offers Section (Spec 7.1, 7.2) */}
      {offeredEntries.length > 0 && (
        <Box sx={{ mb: 4 }}>
          <Typography variant="h6" fontWeight="bold" sx={{ mb: 1.5 }}>
            Active Offers Awaiting Your Action
          </Typography>
          {offeredEntries.map((offered) => (
            <OfferCard
              key={offered.id}
              entry={offered}
              resourceName={resourceMap.get(offered.resource_id)}
            />
          ))}
        </Box>
      )}

      {isError && (
        <Alert
          severity="error"
          role="alert"
          sx={{ mb: 3 }}
          action={
            <Button color="inherit" size="small" onClick={() => refetch()}>
              Retry
            </Button>
          }
        >
          {error instanceof Error ? error.message : "Failed to load waitlist entries."}
        </Alert>
      )}

      {isLoading ? (
        <Box sx={{ py: 4 }}>
          <Skeleton variant="rectangular" height={200} sx={{ borderRadius: 2 }} />
        </Box>
      ) : !waitlistPage || waitlistPage.items.length === 0 ? (
        <Paper sx={{ p: 4, textAlign: "center", borderRadius: 2 }}>
          <Typography variant="h6" color="text.secondary" gutterBottom>
            No waitlist entries found
          </Typography>
          <Typography variant="body2" color="text.secondary">
            {statusFilter !== "all"
              ? `No ${statusFilter} entries match your filter.`
              : "You are not on any resource waitlists. When an occupied slot opens up, you can join its waitlist."}
          </Typography>
        </Paper>
      ) : (
        <TableContainer component={Paper} elevation={1} sx={{ borderRadius: 2 }}>
          <Table aria-label="My Waitlist table">
            <TableHead>
              <TableRow sx={{ backgroundColor: "#f9fafb" }}>
                <TableCell sx={{ fontWeight: "bold" }}>Resource</TableCell>
                <TableCell sx={{ fontWeight: "bold" }}>Requested Start</TableCell>
                <TableCell sx={{ fontWeight: "bold" }}>Requested End</TableCell>
                <TableCell sx={{ fontWeight: "bold" }}>Status</TableCell>
                <TableCell sx={{ fontWeight: "bold" }}>Joined On</TableCell>
                <TableCell align="right" sx={{ fontWeight: "bold" }}>
                  Actions
                </TableCell>
              </TableRow>
            </TableHead>
            <TableBody>
              {waitlistPage.items.map((w) => {
                const resName = resourceMap.get(w.resource_id) || w.resource_id.slice(0, 8);
                const isWaiting = w.status === "waiting";

                return (
                  <TableRow key={w.id} hover>
                    <TableCell sx={{ fontWeight: "medium" }}>{resName}</TableCell>
                    <TableCell>
                      {formatInNewYork(w.starts_at, {
                        weekday: "short",
                        month: "short",
                        day: "numeric",
                        hour: "2-digit",
                        minute: "2-digit",
                      })}
                    </TableCell>
                    <TableCell>
                      {formatInNewYork(w.ends_at, {
                        hour: "2-digit",
                        minute: "2-digit",
                      })}
                    </TableCell>
                    <TableCell>{getStatusChip(w.status)}</TableCell>
                    <TableCell sx={{ color: "text.secondary", fontSize: "0.85rem" }}>
                      {formatInNewYork(w.created_at, {
                        month: "short",
                        day: "numeric",
                        hour: "2-digit",
                        minute: "2-digit",
                      })}
                    </TableCell>
                    <TableCell align="right">
                      {isWaiting ? (
                        <Button
                          size="small"
                          color="error"
                          variant="outlined"
                          onClick={() => handleOpenLeave(w)}
                          aria-label={`Leave waitlist for ${resName}`}
                        >
                          Leave Waitlist
                        </Button>
                      ) : (
                        <Typography variant="caption" color="text.secondary">
                          {w.status === "offered" ? "Action above" : "None"}
                        </Typography>
                      )}
                    </TableCell>
                  </TableRow>
                );
              })}
            </TableBody>
          </Table>

          {totalPages > 1 && (
            <Box sx={{ display: "flex", justifyContent: "center", p: 2 }}>
              <Pagination
                count={totalPages}
                page={page}
                onChange={(_, val) => setPage(val)}
                color="primary"
                size="small"
                aria-label="Waitlist pagination"
              />
            </Box>
          )}
        </TableContainer>
      )}

      {/* Leave Waitlist Confirmation Dialog */}
      <Dialog
        open={Boolean(leaveTarget)}
        onClose={handleCloseLeave}
        maxWidth="xs"
        fullWidth
        aria-labelledby="leave-waitlist-title"
      >
        <DialogTitle id="leave-waitlist-title" sx={{ fontWeight: "bold" }}>
          Leave Waitlist
        </DialogTitle>
        <DialogContent>
          {leaveError && (
            <Alert severity="error" role="alert" sx={{ mb: 2 }}>
              {leaveError}
            </Alert>
          )}

          <DialogContentText paragraph>
            Are you sure you want to withdraw from the waitlist for{" "}
            <strong>
              {leaveTarget
                ? resourceMap.get(leaveTarget.resource_id) || leaveTarget.resource_id.slice(0, 8)
                : ""}
            </strong>
            ? You will lose your current queue position.
          </DialogContentText>
        </DialogContent>
        <DialogActions sx={{ px: 3, pb: 2 }}>
          <Button onClick={handleCloseLeave} disabled={isLeaving} color="inherit">
            Stay on Waitlist
          </Button>
          <Button
            onClick={handleConfirmLeave}
            variant="contained"
            color="error"
            disabled={isLeaving}
            startIcon={isLeaving ? <CircularProgress size={16} color="inherit" /> : null}
          >
            {isLeaving ? "Removing..." : "Confirm Leave"}
          </Button>
        </DialogActions>
      </Dialog>
    </Box>
  );
}
