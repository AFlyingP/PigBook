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
  Button,
  TablePagination,
  Alert,
  CircularProgress,
  Dialog,
  DialogTitle,
  DialogContent,
  DialogContentText,
  DialogActions,
} from "@mui/material";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { request, ApiError } from "../../../api/client";
import type { components } from "../../../api/schema";

type OutboxView = components["schemas"]["OutboxView"];
type PageOutbox = components["schemas"]["Page_OutboxView_"];

export function OutboxTable() {
  const queryClient = useQueryClient();
  const [page, setPage] = useState(0);
  const [rowsPerPage, setRowsPerPage] = useState(25);
  const [statusFilter, setStatusFilter] = useState<string>("all");
  const [announcement, setAnnouncement] = useState("");

  const [retryTarget, setRetryTarget] = useState<OutboxView | null>(null);
  const [isRetrying, setIsRetrying] = useState(false);
  const [retryError, setRetryError] = useState<string | null>(null);

  const queryParams = new URLSearchParams({
    limit: String(rowsPerPage),
    offset: String(page * rowsPerPage),
  });
  if (statusFilter !== "all") {
    queryParams.set("status", statusFilter);
  }

  const { data, isLoading, error, refetch } = useQuery<PageOutbox, ApiError>({
    queryKey: ["admin", "outbox", { page, rowsPerPage, statusFilter }],
    queryFn: async () => {
      const res = await request<PageOutbox>(`/api/v1/admin/outbox?${queryParams.toString()}`);
      return res.data;
    },
  });

  const handleConfirmRetry = async () => {
    if (!retryTarget) return;

    setIsRetrying(true);
    setRetryError(null);

    try {
      await request<OutboxView>(`/api/v1/admin/outbox/${retryTarget.id}/retry`, {
        method: "POST",
        body: {},
      });

      queryClient.invalidateQueries({ queryKey: ["admin", "outbox"] });
      setAnnouncement(`Outbox event ${retryTarget.id.substring(0, 8)} reset to pending.`);
      setRetryTarget(null);
      refetch();
    } catch (err) {
      if (err instanceof ApiError) {
        if (err.status === 409 && err.code === "INVALID_STATE") {
          setRetryError(
            "This event is no longer in dead state and cannot be retried (INVALID_STATE)."
          );
        } else {
          setRetryError(err.message || "Failed to retry outbox event.");
        }
      } else {
        setRetryError("A network or unexpected error occurred.");
      }
    } finally {
      setIsRetrying(false);
    }
  };

  const getStatusChip = (status: string) => {
    switch (status) {
      case "delivered":
        return <Chip label="Delivered" size="small" color="success" variant="outlined" />;
      case "pending":
        return <Chip label="Pending" size="small" color="info" variant="outlined" />;
      case "processing":
        return <Chip label="Processing" size="small" color="warning" variant="outlined" />;
      case "dead":
        return <Chip label="Dead" size="small" color="error" variant="outlined" />;
      default:
        return <Chip label={status} size="small" variant="outlined" />;
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
            Transactional Outbox Queue
          </Typography>
          <Typography variant="body2" color="text.secondary">
            Inspect asynchronous notification events, delivery status, and retry dead-lettered entries.
          </Typography>
        </Box>

        <FormControl size="small" sx={{ minWidth: 160 }}>
          <InputLabel id="outbox-status-filter-label">Status Filter</InputLabel>
          <Select
            labelId="outbox-status-filter-label"
            id="outbox-status-filter"
            value={statusFilter}
            label="Status Filter"
            onChange={(e) => {
              setStatusFilter(e.target.value);
              setPage(0);
            }}
          >
            <MenuItem value="all">All Events</MenuItem>
            <MenuItem value="dead">Dead (Failed)</MenuItem>
            <MenuItem value="pending">Pending</MenuItem>
            <MenuItem value="processing">Processing</MenuItem>
            <MenuItem value="delivered">Delivered</MenuItem>
          </Select>
        </FormControl>
      </Box>

      {error && (
        <Alert severity="error" sx={{ mb: 2 }} role="alert">
          {error.message || "Failed to load outbox events."}
        </Alert>
      )}

      {isLoading ? (
        <Box sx={{ display: "flex", justifyContent: "center", py: 8 }}>
          <CircularProgress aria-label="Loading outbox events..." />
        </Box>
      ) : (
        <Paper variant="outlined">
          <TableContainer>
            <Table aria-label="Outbox events table">
              <TableHead>
                <TableRow>
                  <TableCell><strong>ID</strong></TableCell>
                  <TableCell><strong>Event Type</strong></TableCell>
                  <TableCell><strong>Aggregate ID</strong></TableCell>
                  <TableCell><strong>Status</strong></TableCell>
                  <TableCell><strong>Attempts</strong></TableCell>
                  <TableCell><strong>Occurred At (UTC)</strong></TableCell>
                  <TableCell><strong>Last Error</strong></TableCell>
                  <TableCell align="right"><strong>Actions</strong></TableCell>
                </TableRow>
              </TableHead>
              <TableBody>
                {data?.items && data.items.length > 0 ? (
                  data.items.map((event) => (
                    <TableRow key={event.id} hover>
                      <TableCell component="th" scope="row">
                        <Typography variant="caption" fontFamily="monospace">
                          {event.id.substring(0, 8)}...
                        </Typography>
                      </TableCell>
                      <TableCell>
                        <Typography variant="body2" fontWeight="medium">
                          {event.event_type}
                        </Typography>
                      </TableCell>
                      <TableCell>
                        <Typography variant="caption" fontFamily="monospace">
                          {event.aggregate_id.substring(0, 8)}...
                        </Typography>
                      </TableCell>
                      <TableCell>{getStatusChip(event.status)}</TableCell>
                      <TableCell>{event.attempts}</TableCell>
                      <TableCell sx={{ whiteSpace: "nowrap" }}>
                        {new Date(event.occurred_at).toISOString()}
                      </TableCell>
                      <TableCell sx={{ maxWidth: 200, overflow: "hidden", textOverflow: "ellipsis" }}>
                        {event.last_error ? (
                          <Typography variant="caption" color="error">
                            {event.last_error}
                          </Typography>
                        ) : (
                          <Typography variant="caption" color="text.secondary">
                            None
                          </Typography>
                        )}
                      </TableCell>
                      <TableCell align="right">
                        {/* Spec: Dead retry must ONLY be offered for dead items and requires confirmation */}
                        <Button
                          size="small"
                          color="error"
                          variant="outlined"
                          disabled={event.status !== "dead"}
                          onClick={() => {
                            setRetryError(null);
                            setRetryTarget(event);
                          }}
                          aria-label={`Retry dead event ${event.id.substring(0, 8)}`}
                        >
                          Retry Event
                        </Button>
                      </TableCell>
                    </TableRow>
                  ))
                ) : (
                  <TableRow>
                    <TableCell colSpan={8} align="center" sx={{ py: 4 }}>
                      <Typography variant="body2" color="text.secondary">
                        No outbox events found.
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
            aria-label="Outbox table pagination"
          />
        </Paper>
      )}

      {/* Confirmation Dialog for Retrying Dead Outbox Event */}
      <Dialog
        open={!!retryTarget}
        onClose={() => {
          if (!isRetrying) {
            setRetryTarget(null);
            setRetryError(null);
          }
        }}
        aria-labelledby="retry-outbox-dialog-title"
        maxWidth="xs"
        fullWidth
      >
        <DialogTitle id="retry-outbox-dialog-title">Retry Dead Outbox Event</DialogTitle>
        <DialogContent>
          {retryError && (
            <Alert severity="error" sx={{ mb: 2 }} role="alert">
              {retryError}
            </Alert>
          )}
          <DialogContentText>
            Are you sure you want to retry event <code>{retryTarget?.id.substring(0, 8)}...</code>?
          </DialogContentText>
          <DialogContentText variant="body2" sx={{ mt: 1 }} color="text.secondary">
            This will reset the event status to <strong>pending</strong> and clear its attempt count
            to allow the notification dispatcher to re-process delivery.
          </DialogContentText>
        </DialogContent>
        <DialogActions>
          <Button
            onClick={() => {
              setRetryTarget(null);
              setRetryError(null);
            }}
            disabled={isRetrying}
            color="inherit"
          >
            Cancel
          </Button>
          <Button
            onClick={handleConfirmRetry}
            variant="contained"
            color="primary"
            disabled={isRetrying}
            startIcon={isRetrying ? <CircularProgress size={18} /> : null}
            id="confirm-outbox-retry-btn"
          >
            {isRetrying ? "Retrying..." : "Confirm Retry"}
          </Button>
        </DialogActions>
      </Dialog>
    </Box>
  );
}
