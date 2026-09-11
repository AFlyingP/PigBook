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
  TextField,
  Alert,
  CircularProgress,
  Skeleton,
} from "@mui/material";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { request, ApiError } from "../../api/client";
import { formatInNewYork } from "../resources/timeUtils";
import type { components } from "../../api/schema";

export type Booking = components["schemas"]["Booking"];
export type PageBooking = components["schemas"]["Page_Booking_"];
export type BookingStatusFilter = components["schemas"]["BookingStatusFilter"];
export type Resource = components["schemas"]["Resource"];
export type PageResource = components["schemas"]["Page_Resource_"];

export function OwnBookingsTable() {
  const queryClient = useQueryClient();
  const [page, setPage] = useState(1);
  const limit = 10;
  const [statusFilter, setStatusFilter] = useState<string>("all");

  // State for cancel dialog
  const [cancelTarget, setCancelTarget] = useState<Booking | null>(null);
  const [cancelReason, setCancelReason] = useState("");
  const [isCancelling, setIsCancelling] = useState(false);
  const [cancelError, setCancelError] = useState<string | null>(null);
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

  // Load user's bookings
  const offset = (page - 1) * limit;
  const {
    data: bookingsPage,
    isLoading,
    isError,
    error,
    refetch,
  } = useQuery<PageBooking>({
    queryKey: ["bookings", "mine", { status: statusFilter, limit, offset }],
    queryFn: async () => {
      const params = new URLSearchParams({
        limit: String(limit),
        offset: String(offset),
      });
      if (statusFilter !== "all") {
        params.set("status", statusFilter);
      }
      const res = await request<PageBooking>(`/api/v1/bookings?${params.toString()}`);
      return res.data;
    },
  });

  const handleOpenCancel = (booking: Booking) => {
    setCancelTarget(booking);
    setCancelReason("");
    setCancelError(null);
  };

  const handleCloseCancel = () => {
    if (isCancelling) return;
    setCancelTarget(null);
    setCancelReason("");
    setCancelError(null);
  };

  const handleConfirmCancel = async () => {
    if (!cancelTarget) return;

    setIsCancelling(true);
    setCancelError(null);

    try {
      // 1. Fetch latest booking ETag (Spec 4.1, 7.2)
      const detailRes = await request<Booking>(`/api/v1/bookings/${cancelTarget.id}`);
      const etag = detailRes.etag || String(detailRes.data.version);

      // 2. Perform cancel with If-Match (Spec 4.2 E12, 7.2)
      await request<Booking>(`/api/v1/bookings/${cancelTarget.id}/cancel`, {
        method: "POST",
        headers: {
          "If-Match": `"${etag}"`,
          "Content-Type": "application/json",
        },
        body: {
          reason: cancelReason.trim(),
        },
      });

      // Success: invalidate server state and announce
      queryClient.invalidateQueries({ queryKey: ["bookings", "mine"] });
      queryClient.invalidateQueries({ queryKey: ["resources"] });
      queryClient.invalidateQueries({ queryKey: ["waitlist", "mine"] });

      setAnnouncement("Booking cancelled successfully.");
      handleCloseCancel();
    } catch (err) {
      if (err instanceof ApiError) {
        if (err.status === 412) {
          // 412 Precondition Failed / Version Mismatch: tell user another update occurred and refetch
          setCancelError(
            "Another update occurred to this booking. Refreshing latest status..."
          );
          refetch();
          queryClient.invalidateQueries({ queryKey: ["bookings", "mine"] });
        } else if (err.status === 409) {
          if (err.code === "TOO_LATE") {
            setCancelError(
              "This reservation can no longer be cancelled because the cancellation deadline has passed."
            );
          } else {
            setCancelError(
              err.message || "This reservation is in a terminal or invalid state for cancellation."
            );
          }
          refetch();
        } else {
          setCancelError(err.message || "Failed to cancel booking. Please try again.");
        }
      } else {
        setCancelError("Network error while cancelling reservation.");
      }
    } finally {
      setIsCancelling(false);
    }
  };

  const getStatusChip = (status: string) => {
    switch (status) {
      case "confirmed":
        return <Chip label="Confirmed" color="success" size="small" />;
      case "offered":
        return <Chip label="Offered Hold" color="warning" size="small" />;
      case "cancelled":
        return <Chip label="Cancelled" color="default" size="small" />;
      case "expired":
        return <Chip label="Expired" color="error" size="small" />;
      default:
        return <Chip label={status} size="small" />;
    }
  };

  const totalPages = bookingsPage ? Math.ceil(bookingsPage.total / limit) : 1;

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
          <Typography component="h1" variant="h4" fontWeight="bold">
            My Bookings
          </Typography>
          <Typography variant="body2" color="text.secondary">
            View and manage your scheduled reservations. All times shown in America/New_York.
          </Typography>
        </Box>

        <FormControl size="small" sx={{ minWidth: 160 }}>
          <InputLabel id="booking-status-filter-label">Filter by Status</InputLabel>
          <Select
            labelId="booking-status-filter-label"
            id="booking-status-filter"
            value={statusFilter}
            label="Filter by Status"
            onChange={(e) => {
              setStatusFilter(e.target.value);
              setPage(1);
            }}
          >
            <MenuItem value="all">All Bookings</MenuItem>
            <MenuItem value="confirmed">Confirmed</MenuItem>
            <MenuItem value="offered">Offered</MenuItem>
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
          {error instanceof Error ? error.message : "Failed to load bookings."}
        </Alert>
      )}

      {isLoading ? (
        <Box sx={{ py: 4 }}>
          <Skeleton variant="rectangular" height={200} sx={{ borderRadius: 2 }} />
        </Box>
      ) : !bookingsPage || bookingsPage.items.length === 0 ? (
        <Paper sx={{ p: 4, textAlign: "center", borderRadius: 2 }}>
          <Typography variant="h6" color="text.secondary" gutterBottom>
            No reservations found
          </Typography>
          <Typography variant="body2" color="text.secondary">
            {statusFilter !== "all"
              ? `No ${statusFilter} reservations match your filter.`
              : "You do not have any bookings yet. Browse the resources catalog to make a reservation."}
          </Typography>
        </Paper>
      ) : (
        <TableContainer component={Paper} elevation={1} sx={{ borderRadius: 2 }}>
          <Table aria-label="My Bookings table">
            <TableHead>
              <TableRow sx={{ backgroundColor: "#f9fafb" }}>
                <TableCell sx={{ fontWeight: "bold" }}>Resource</TableCell>
                <TableCell sx={{ fontWeight: "bold" }}>Start Time (Local)</TableCell>
                <TableCell sx={{ fontWeight: "bold" }}>End Time (Local)</TableCell>
                <TableCell sx={{ fontWeight: "bold" }}>Status</TableCell>
                <TableCell sx={{ fontWeight: "bold" }}>Booked On</TableCell>
                <TableCell align="right" sx={{ fontWeight: "bold" }}>
                  Actions
                </TableCell>
              </TableRow>
            </TableHead>
            <TableBody>
              {bookingsPage.items.map((b) => {
                const resName = resourceMap.get(b.resource_id) || b.resource_id.slice(0, 8);
                const isCancellable = b.status === "confirmed" || b.status === "offered";

                return (
                  <TableRow key={b.id} hover>
                    <TableCell sx={{ fontWeight: "medium" }}>{resName}</TableCell>
                    <TableCell>
                      {formatInNewYork(b.starts_at, {
                        weekday: "short",
                        month: "short",
                        day: "numeric",
                        hour: "2-digit",
                        minute: "2-digit",
                      })}
                    </TableCell>
                    <TableCell>
                      {formatInNewYork(b.ends_at, {
                        hour: "2-digit",
                        minute: "2-digit",
                      })}
                    </TableCell>
                    <TableCell>{getStatusChip(b.status)}</TableCell>
                    <TableCell sx={{ color: "text.secondary", fontSize: "0.85rem" }}>
                      {formatInNewYork(b.created_at, {
                        month: "short",
                        day: "numeric",
                        hour: "2-digit",
                        minute: "2-digit",
                      })}
                    </TableCell>
                    <TableCell align="right">
                      {isCancellable ? (
                        <Button
                          size="small"
                          color="error"
                          variant="outlined"
                          onClick={() => handleOpenCancel(b)}
                          aria-label={`Cancel reservation for ${resName}`}
                        >
                          Cancel
                        </Button>
                      ) : (
                        <Typography variant="caption" color="text.secondary">
                          No actions
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
                aria-label="Bookings pagination"
              />
            </Box>
          )}
        </TableContainer>
      )}

      {/* Cancel Confirmation Dialog (Spec 4.1, 7.2) */}
      <Dialog
        open={Boolean(cancelTarget)}
        onClose={handleCloseCancel}
        maxWidth="xs"
        fullWidth
        aria-labelledby="cancel-dialog-title"
      >
        <DialogTitle id="cancel-dialog-title" sx={{ fontWeight: "bold" }}>
          Cancel Reservation
        </DialogTitle>
        <DialogContent>
          {cancelError && (
            <Alert severity="error" role="alert" sx={{ mb: 2 }}>
              {cancelError}
            </Alert>
          )}

          <DialogContentText paragraph>
            Are you sure you want to cancel this reservation for{" "}
            <strong>
              {cancelTarget
                ? resourceMap.get(cancelTarget.resource_id) || cancelTarget.resource_id.slice(0, 8)
                : ""}
            </strong>
            ? This will release the slot and allow members on the waitlist to claim it.
          </DialogContentText>

          {cancelTarget && (
            <Box sx={{ mb: 2, p: 1.5, backgroundColor: "#f5f5f5", borderRadius: 1 }}>
              <Typography variant="body2">
                {formatInNewYork(cancelTarget.starts_at, {
                  weekday: "short",
                  month: "short",
                  day: "numeric",
                  hour: "2-digit",
                  minute: "2-digit",
                })}{" "}
                –{" "}
                {formatInNewYork(cancelTarget.ends_at, {
                  hour: "2-digit",
                  minute: "2-digit",
                })}
              </Typography>
            </Box>
          )}

          <TextField
            id="cancel-reason"
            label="Cancellation Reason (Optional)"
            placeholder="e.g. Schedule conflict"
            fullWidth
            size="small"
            value={cancelReason}
            onChange={(e) => setCancelReason(e.target.value)}
            disabled={isCancelling}
            inputProps={{ maxLength: 500 }}
            helperText="Maximum 500 characters"
          />
        </DialogContent>
        <DialogActions sx={{ px: 3, pb: 2 }}>
          <Button onClick={handleCloseCancel} disabled={isCancelling} color="inherit">
            Keep Reservation
          </Button>
          <Button
            onClick={handleConfirmCancel}
            variant="contained"
            color="error"
            disabled={isCancelling}
            startIcon={isCancelling ? <CircularProgress size={16} color="inherit" /> : null}
          >
            {isCancelling ? "Cancelling..." : "Confirm Cancellation"}
          </Button>
        </DialogActions>
      </Dialog>
    </Box>
  );
}
