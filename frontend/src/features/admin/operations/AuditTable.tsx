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
  TextField,
  Button,
  TablePagination,
  Alert,
  CircularProgress,
} from "@mui/material";
import { useQuery } from "@tanstack/react-query";
import { request, ApiError } from "../../../api/client";
import type { components } from "../../../api/schema";

type PageAudit = components["schemas"]["Page_Audit_"];

export function AuditTable() {
  const [page, setPage] = useState(0);
  const [rowsPerPage, setRowsPerPage] = useState(25);
  const [targetIdFilter, setTargetIdFilter] = useState("");
  const [activeFilter, setActiveFilter] = useState("");

  const queryParams = new URLSearchParams({
    limit: String(rowsPerPage),
    offset: String(page * rowsPerPage),
  });
  if (activeFilter.trim()) {
    queryParams.set("target_id", activeFilter.trim());
  }

  const { data, isLoading, error } = useQuery<PageAudit, ApiError>({
    queryKey: ["admin", "audit", { page, rowsPerPage, activeFilter }],
    queryFn: async () => {
      const res = await request<PageAudit>(`/api/v1/admin/audit?${queryParams.toString()}`);
      return res.data;
    },
  });

  const handleApplyFilter = () => {
    setActiveFilter(targetIdFilter.trim());
    setPage(0);
  };

  const handleClearFilter = () => {
    setTargetIdFilter("");
    setActiveFilter("");
    setPage(0);
  };

  return (
    <Box sx={{ width: "100%" }}>
      <Box sx={{ mb: 3 }}>
        <Typography variant="h5" component="h2" fontWeight="bold">
          System Audit Log
        </Typography>
        <Typography variant="body2" color="text.secondary">
          Immutable audit record of administrative mutations, auth failures, and operational events.
        </Typography>
      </Box>

      {/* Target Filter */}
      <Paper variant="outlined" sx={{ p: 2, mb: 3, display: "flex", gap: 2, alignItems: "center", flexWrap: "wrap" }}>
        <TextField
          size="small"
          id="audit-target-id-filter"
          label="Filter by Target ID (UUID)"
          value={targetIdFilter}
          onChange={(e) => setTargetIdFilter(e.target.value)}
          sx={{ minWidth: { xs: "100%", sm: 280 } }}
          placeholder="e.g. 55555555-..."
        />
        <Button
          variant="outlined"
          color="primary"
          onClick={handleApplyFilter}
          id="apply-audit-filter-btn"
        >
          Filter
        </Button>
        {activeFilter && (
          <Button variant="text" color="inherit" onClick={handleClearFilter}>
            Clear
          </Button>
        )}
      </Paper>

      {error && (
        <Alert severity="error" sx={{ mb: 2 }} role="alert">
          {error.message || "Failed to load audit logs."}
        </Alert>
      )}

      {isLoading ? (
        <Box sx={{ display: "flex", justifyContent: "center", py: 8 }}>
          <CircularProgress aria-label="Loading audit records..." />
        </Box>
      ) : (
        <Paper variant="outlined">
          <TableContainer>
            <Table aria-label="Audit log table">
              <TableHead>
                <TableRow>
                  <TableCell><strong>Timestamp (UTC)</strong></TableCell>
                  <TableCell><strong>Action</strong></TableCell>
                  <TableCell><strong>Target Type / ID</strong></TableCell>
                  <TableCell><strong>Actor ID</strong></TableCell>
                  <TableCell><strong>Request ID</strong></TableCell>
                  <TableCell><strong>Details</strong></TableCell>
                </TableRow>
              </TableHead>
              <TableBody>
                {data?.items && data.items.length > 0 ? (
                  data.items.map((item) => (
                    <TableRow key={item.id} hover>
                      <TableCell sx={{ whiteSpace: "nowrap" }}>
                        {new Date(item.created_at).toISOString()}
                      </TableCell>
                      <TableCell>
                        <Typography variant="body2" fontWeight="medium">
                          {item.action}
                        </Typography>
                      </TableCell>
                      <TableCell>
                        <Typography variant="body2">{item.target_type}</Typography>
                        {item.target_id && (
                          <Typography variant="caption" fontFamily="monospace" color="text.secondary" display="block">
                            {item.target_id}
                          </Typography>
                        )}
                      </TableCell>
                      <TableCell>
                        <Typography variant="caption" fontFamily="monospace">
                          {item.actor_id || "system / anonymous"}
                        </Typography>
                      </TableCell>
                      <TableCell>
                        <Typography variant="caption" fontFamily="monospace">
                          {item.request_id}
                        </Typography>
                      </TableCell>
                      <TableCell sx={{ maxWidth: 350 }}>
                        <Box
                          component="pre"
                          sx={{
                            m: 0,
                            p: 1,
                            backgroundColor: "#f5f5f5",
                            borderRadius: 1,
                            fontSize: "0.75rem",
                            overflow: "auto",
                            maxHeight: 120,
                          }}
                        >
                          {JSON.stringify(item.details, null, 2)}
                        </Box>
                      </TableCell>
                    </TableRow>
                  ))
                ) : (
                  <TableRow>
                    <TableCell colSpan={6} align="center" sx={{ py: 4 }}>
                      <Typography variant="body2" color="text.secondary">
                        No audit records found matching the filter.
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
            aria-label="Audit log table pagination"
          />
        </Paper>
      )}
    </Box>
  );
}
