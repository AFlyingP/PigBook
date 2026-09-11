import React, { useState } from "react";
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
  TablePagination,
  Alert,
  CircularProgress,
} from "@mui/material";
import { useQuery } from "@tanstack/react-query";
import { request, ApiError } from "../../../api/client";
import type { components } from "../../../api/schema";
import { BlackoutCreateDialog } from "./BlackoutCreateDialog";
import { BlackoutCancelDialog } from "./BlackoutCancelDialog";

type Booking = components["schemas"]["Booking"];
type PageBooking = components["schemas"]["Page_Booking_"];

interface BlackoutTableProps {
  resourceId: string;
  resourceName: string;
}

export function BlackoutTable({ resourceId, resourceName }: BlackoutTableProps) {
  const [page, setPage] = useState(0);
  const [rowsPerPage, setRowsPerPage] = useState(25);
  const [announcement, setAnnouncement] = useState<string>("");

  const [createOpen, setCreateOpen] = useState(false);
  const [cancelTarget, setCancelTarget] = useState<Booking | null>(null);

  const queryParams = new URLSearchParams({
    limit: String(rowsPerPage),
    offset: String(page * rowsPerPage),
  });

  const { data, isLoading, error, refetch } = useQuery<PageBooking, ApiError>({
    queryKey: ["admin", "resources", resourceId, "blackouts", { page, rowsPerPage }],
    queryFn: async () => {
      const res = await request<PageBooking>(
        `/api/v1/admin/resources/${resourceId}/blackouts?${queryParams.toString()}`
      );
      return res.data;
    },
  });

  const handleChangePage = (_: unknown, newPage: number) => {
    setPage(newPage);
  };

  const handleChangeRowsPerPage = (
    event: React.ChangeEvent<HTMLInputElement | HTMLTextAreaElement>
  ) => {
    setRowsPerPage(parseInt(event.target.value, 10));
    setPage(0);
  };

  return (
    <Box sx={{ width: "100%" }}>
      {/* Live region for screen readers */}
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
        <Typography variant="h5" component="h2" fontWeight="bold">
          Blackouts for {resourceName}
        </Typography>

        <Button
          variant="contained"
          color="primary"
          onClick={() => setCreateOpen(true)}
          id="add-blackout-button"
        >
          Add Blackout Window
        </Button>
      </Box>

      {error && (
        <Alert severity="error" sx={{ mb: 2 }} role="alert">
          {error.message || "Failed to load blackouts."}
        </Alert>
      )}

      {isLoading ? (
        <Box sx={{ display: "flex", justifyContent: "center", py: 8 }}>
          <CircularProgress aria-label="Loading blackouts..." />
        </Box>
      ) : (
        <Paper variant="outlined">
          <TableContainer>
            <Table aria-label="Blackout schedule table">
              <TableHead>
                <TableRow>
                  <TableCell><strong>ID</strong></TableCell>
                  <TableCell><strong>Starts At (UTC)</strong></TableCell>
                  <TableCell><strong>Ends At (UTC)</strong></TableCell>
                  <TableCell><strong>Status</strong></TableCell>
                  <TableCell><strong>Version</strong></TableCell>
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
                      <TableCell>{new Date(b.starts_at).toISOString()}</TableCell>
                      <TableCell>{new Date(b.ends_at).toISOString()}</TableCell>
                      <TableCell>
                        {b.status === "confirmed" ? (
                          <Chip label="Active Blackout" size="small" color="primary" variant="outlined" />
                        ) : (
                          <Chip label="Cancelled" size="small" color="default" variant="outlined" />
                        )}
                      </TableCell>
                      <TableCell>v{b.version}</TableCell>
                      <TableCell align="right">
                        <Button
                          size="small"
                          color="error"
                          variant="outlined"
                          disabled={b.status === "cancelled"}
                          onClick={() => setCancelTarget(b)}
                          aria-label={`Cancel blackout ${b.id.substring(0, 8)}`}
                        >
                          Cancel
                        </Button>
                      </TableCell>
                    </TableRow>
                  ))
                ) : (
                  <TableRow>
                    <TableCell colSpan={6} align="center" sx={{ py: 4 }}>
                      <Typography variant="body2" color="text.secondary">
                        No blackout windows scheduled for this resource.
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
            onPageChange={handleChangePage}
            onRowsPerPageChange={handleChangeRowsPerPage}
            aria-label="Blackout table pagination"
          />
        </Paper>
      )}

      <BlackoutCreateDialog
        open={createOpen}
        resourceId={resourceId}
        resourceName={resourceName}
        onClose={() => setCreateOpen(false)}
        onCreated={() => {
          setAnnouncement("Blackout created successfully.");
          refetch();
        }}
      />

      <BlackoutCancelDialog
        open={!!cancelTarget}
        blackout={cancelTarget}
        resourceId={resourceId}
        onClose={() => setCancelTarget(null)}
        onCancelled={() => {
          setAnnouncement("Blackout cancelled successfully.");
          refetch();
        }}
      />
    </Box>
  );
}
