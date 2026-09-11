import { useState } from "react";
import {
  Box,
  Typography,
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
  TextField,
  Button,
  TablePagination,
  Alert,
  CircularProgress,
  Grid,
} from "@mui/material";
import { useQuery } from "@tanstack/react-query";
import { request, ApiError } from "../../../api/client";
import type { components } from "../../../api/schema";
import { AdminBookingDetailDialog } from "./AdminBookingDetailDialog";
import { AdminBookingCancelDialog } from "./AdminBookingCancelDialog";

type Booking = components["schemas"]["Booking"];
type PageBooking = components["schemas"]["Page_Booking_"];

export function AdminBookingsTable() {
  const [page, setPage] = useState(0);
  const [rowsPerPage, setRowsPerPage] = useState(25);
  const [announcement, setAnnouncement] = useState("");

  // Filters
  const [statusFilter, setStatusFilter] = useState<string>("all");
  const [resourceFilter, setResourceFilter] = useState<string>("");
  const [userFilter, setUserFilter] = useState<string>("");
  const [startsAtFilter, setStartsAtFilter] = useState<string>("");
  const [endsAtFilter, setEndsAtFilter] = useState<string>("");
  const [dateRangeError, setDateRangeError] = useState<string | null>(null);

  // Active dialogs
  const [detailTarget, setDetailTarget] = useState<Booking | null>(null);
  const [cancelTarget, setCancelTarget] = useState<Booking | null>(null);

  const buildParams = () => {
    const params = new URLSearchParams({
      limit: String(rowsPerPage),
      offset: String(page * rowsPerPage),
    });

    if (statusFilter !== "all") {
      params.set("status", statusFilter);
    }
    if (resourceFilter.trim()) {
      params.set("resource_id", resourceFilter.trim());
    }
    if (userFilter.trim()) {
      params.set("user_id", userFilter.trim());
    }
    // Spec 4.2 E24: starts_at and ends_at must be provided TOGETHER or not at all
    if (startsAtFilter && endsAtFilter) {
      params.set("starts_at", new Date(startsAtFilter).toISOString());
      params.set("ends_at", new Date(endsAtFilter).toISOString());
    }

    return params;
  };

  const { data, isLoading, error, refetch } = useQuery<PageBooking, ApiError>({
    queryKey: [
      "admin",
      "bookings",
      {
        page,
        rowsPerPage,
        statusFilter,
        resourceFilter,
        userFilter,
        startsAtFilter: startsAtFilter && endsAtFilter ? startsAtFilter : "",
        endsAtFilter: startsAtFilter && endsAtFilter ? endsAtFilter : "",
      },
    ],
    queryFn: async () => {
      const res = await request<PageBooking>(`/api/v1/admin/bookings?${buildParams().toString()}`);
      return res.data;
    },
  });

  const handleApplyDateFilter = () => {
    if ((startsAtFilter && !endsAtFilter) || (!startsAtFilter && endsAtFilter)) {
      setDateRangeError("Both start date and end date must be provided together.");
      return;
    }
    if (startsAtFilter && endsAtFilter) {
      const s = new Date(startsAtFilter).getTime();
      const e = new Date(endsAtFilter).getTime();
      if (e <= s) {
        setDateRangeError("End date must be after start date.");
        return;
      }
      const days = (e - s) / (1000 * 60 * 60 * 24);
      if (days > 90) {
        setDateRangeError("Date range span cannot exceed 90 days.");
        return;
      }
    }
    setDateRangeError(null);
    setPage(0);
    refetch();
  };

  const handleResetFilters = () => {
    setStatusFilter("all");
    setResourceFilter("");
    setUserFilter("");
    setStartsAtFilter("");
    setEndsAtFilter("");
    setDateRangeError(null);
    setPage(0);
  };

  return (
    <Box sx={{ width: "100%" }}>
      {/* Live region */}
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

      <Box sx={{ mb: 3 }}>
        <Typography variant="h5" component="h2" fontWeight="bold" gutterBottom>
          Reservations & Bookings
        </Typography>
        <Typography variant="body2" color="text.secondary">
          Filter and manage member reservations across all community resources.
        </Typography>
      </Box>

      {/* Filter Controls */}
      <Paper variant="outlined" sx={{ p: 2, mb: 3 }}>
        <Grid container spacing={2} alignItems="center">
          <Grid item xs={12} sm={6} md={3}>
            <FormControl size="small" fullWidth>
              <InputLabel id="booking-status-filter-label">Status</InputLabel>
              <Select
                labelId="booking-status-filter-label"
                id="booking-status-filter"
                value={statusFilter}
                label="Status"
                onChange={(e) => {
                  setStatusFilter(e.target.value);
                  setPage(0);
                }}
              >
                <MenuItem value="all">All Statuses</MenuItem>
                <MenuItem value="confirmed">Confirmed</MenuItem>
                <MenuItem value="offered">Offered</MenuItem>
                <MenuItem value="cancelled">Cancelled</MenuItem>
                <MenuItem value="expired">Expired</MenuItem>
              </Select>
            </FormControl>
          </Grid>

          <Grid item xs={12} sm={6} md={3}>
            <TextField
              size="small"
              fullWidth
              id="booking-filter-resource"
              label="Resource ID (UUID)"
              value={resourceFilter}
              onChange={(e) => setResourceFilter(e.target.value)}
              placeholder="e.g. 55555555-..."
            />
          </Grid>

          <Grid item xs={12} sm={6} md={3}>
            <TextField
              size="small"
              fullWidth
              id="booking-filter-user"
              label="User ID (UUID)"
              value={userFilter}
              onChange={(e) => setUserFilter(e.target.value)}
              placeholder="e.g. 22222222-..."
            />
          </Grid>

          <Grid item xs={12} sm={6} md={3}>
            <Box sx={{ display: "flex", gap: 1 }}>
              <Button
                variant="outlined"
                color="primary"
                onClick={handleApplyDateFilter}
                size="small"
                id="apply-booking-filters-btn"
              >
                Apply Filters
              </Button>
              <Button
                variant="text"
                color="inherit"
                onClick={handleResetFilters}
                size="small"
              >
                Reset
              </Button>
            </Box>
          </Grid>

          {/* Date range pair */}
          <Grid item xs={12} sm={6} md={4}>
            <TextField
              size="small"
              fullWidth
              id="booking-filter-starts-at"
              label="Start Range (UTC)"
              type="date"
              value={startsAtFilter}
              onChange={(e) => setStartsAtFilter(e.target.value)}
              InputLabelProps={{ shrink: true }}
            />
          </Grid>

          <Grid item xs={12} sm={6} md={4}>
            <TextField
              size="small"
              fullWidth
              id="booking-filter-ends-at"
              label="End Range (UTC)"
              type="date"
              value={endsAtFilter}
              onChange={(e) => setEndsAtFilter(e.target.value)}
              InputLabelProps={{ shrink: true }}
            />
          </Grid>
        </Grid>

        {dateRangeError && (
          <Alert severity="error" sx={{ mt: 2 }} role="alert">
            {dateRangeError}
          </Alert>
        )}
      </Paper>

      {error && (
        <Alert severity="error" sx={{ mb: 2 }} role="alert">
          {error.message || "Failed to load bookings."}
        </Alert>
      )}

      {isLoading ? (
        <Box sx={{ display: "flex", justifyContent: "center", py: 8 }}>
          <CircularProgress aria-label="Loading bookings..." />
        </Box>
      ) : (
        <Paper variant="outlined">
          <TableContainer>
            <Table aria-label="Administrator bookings table">
              <TableHead>
                <TableRow>
                  <TableCell><strong>ID</strong></TableCell>
                  <TableCell><strong>Resource ID</strong></TableCell>
                  <TableCell><strong>User ID</strong></TableCell>
                  <TableCell><strong>Starts At (UTC)</strong></TableCell>
                  <TableCell><strong>Ends At (UTC)</strong></TableCell>
                  <TableCell><strong>Status</strong></TableCell>
                  <TableCell align="right"><strong>Actions</strong></TableCell>
                </TableRow>
              </TableHead>
              <TableBody>
                {data?.items && data.items.length > 0 ? (
                  data.items.map((b) => (
                    <TableRow key={b.id} hover>
                      <TableCell component="th" scope="row">
                        <Typography variant="body2" fontFamily="monospace">
                          {b.id.substring(0, 8)}...
                        </Typography>
                      </TableCell>
                      <TableCell>
                        <Typography variant="body2" fontFamily="monospace">
                          {b.resource_id.substring(0, 8)}...
                        </Typography>
                      </TableCell>
                      <TableCell>
                        <Typography variant="body2" fontFamily="monospace">
                          {b.user_id ? `${b.user_id.substring(0, 8)}...` : "N/A"}
                        </Typography>
                      </TableCell>
                      <TableCell>{new Date(b.starts_at).toLocaleString()}</TableCell>
                      <TableCell>{new Date(b.ends_at).toLocaleString()}</TableCell>
                      <TableCell>
                        <Chip
                          label={b.status}
                          size="small"
                          color={
                            b.status === "confirmed"
                              ? "success"
                              : b.status === "offered"
                              ? "primary"
                              : "default"
                          }
                          variant="outlined"
                        />
                      </TableCell>
                      <TableCell align="right">
                        <Box sx={{ display: "flex", justifyContent: "flex-end", gap: 1 }}>
                          <Button
                            size="small"
                            variant="outlined"
                            onClick={() => setDetailTarget(b)}
                            aria-label={`View booking ${b.id.substring(0, 8)}`}
                          >
                            Details
                          </Button>
                          <Button
                            size="small"
                            color="error"
                            variant="outlined"
                            disabled={b.status !== "confirmed" && b.status !== "offered"}
                            onClick={() => setCancelTarget(b)}
                            aria-label={`Cancel booking ${b.id.substring(0, 8)}`}
                          >
                            Cancel
                          </Button>
                        </Box>
                      </TableCell>
                    </TableRow>
                  ))
                ) : (
                  <TableRow>
                    <TableCell colSpan={7} align="center" sx={{ py: 4 }}>
                      <Typography variant="body2" color="text.secondary">
                        No bookings found matching the selected filters.
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
            aria-label="Bookings table pagination"
          />
        </Paper>
      )}

      {/* Dialogs */}
      <AdminBookingDetailDialog
        open={!!detailTarget}
        booking={detailTarget}
        onClose={() => setDetailTarget(null)}
        onCancelRequested={(b) => setCancelTarget(b)}
      />

      <AdminBookingCancelDialog
        open={!!cancelTarget}
        booking={cancelTarget}
        onClose={() => setCancelTarget(null)}
        onCancelled={() => {
          setAnnouncement("Booking cancelled successfully.");
          refetch();
        }}
      />
    </Box>
  );
}
