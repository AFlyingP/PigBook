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
  FormControl,
  InputLabel,
  Select,
  MenuItem,
  TablePagination,
  Alert,
  CircularProgress,
} from "@mui/material";
import { Link as RouterLink } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { request, ApiError } from "../../../api/client";
import type { components } from "../../../api/schema";
import { ResourceCreateDialog } from "./ResourceCreateDialog";
import { ResourceEditDialog } from "./ResourceEditDialog";
import { ResourceArchiveDialog } from "./ResourceArchiveDialog";

type Resource = components["schemas"]["Resource"];
type PageResource = components["schemas"]["Page_Resource_"];

export function ResourceTable() {
  const [page, setPage] = useState(0);
  const [rowsPerPage, setRowsPerPage] = useState(25);
  const [activeFilter, setActiveFilter] = useState<string>("all");
  const [announcement, setAnnouncement] = useState<string>("");

  const [createOpen, setCreateOpen] = useState(false);
  const [editTarget, setEditTarget] = useState<Resource | null>(null);
  const [archiveTarget, setArchiveTarget] = useState<Resource | null>(null);

  const queryParams = new URLSearchParams({
    limit: String(rowsPerPage),
    offset: String(page * rowsPerPage),
  });
  if (activeFilter === "active") {
    queryParams.set("active", "true");
  } else if (activeFilter === "inactive") {
    queryParams.set("active", "false");
  }

  const { data, isLoading, error, refetch } = useQuery<PageResource, ApiError>({
    queryKey: ["admin", "resources", { page, rowsPerPage, activeFilter }],
    queryFn: async () => {
      const res = await request<PageResource>(
        `/api/v1/admin/resources?${queryParams.toString()}`
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
      {/* Screen reader live region for polite announcements */}
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
          Resource Inventory
        </Typography>

        <Box sx={{ display: "flex", alignItems: "center", gap: 2 }}>
          <FormControl size="small" sx={{ minWidth: 160 }}>
            <InputLabel id="resource-active-filter-label">Status Filter</InputLabel>
            <Select
              labelId="resource-active-filter-label"
              id="resource-active-filter"
              value={activeFilter}
              label="Status Filter"
              onChange={(e) => {
                setActiveFilter(e.target.value);
                setPage(0);
              }}
            >
              <MenuItem value="all">All Resources</MenuItem>
              <MenuItem value="active">Active Only</MenuItem>
              <MenuItem value="inactive">Archived Only</MenuItem>
            </Select>
          </FormControl>

          <Button
            variant="contained"
            color="primary"
            onClick={() => setCreateOpen(true)}
            id="add-resource-button"
          >
            Add Resource
          </Button>
        </Box>
      </Box>

      {error && (
        <Alert severity="error" sx={{ mb: 2 }} role="alert">
          {error.message || "Failed to load resources."}
        </Alert>
      )}

      {isLoading ? (
        <Box sx={{ display: "flex", justifyContent: "center", py: 8 }}>
          <CircularProgress aria-label="Loading resources..." />
        </Box>
      ) : (
        <Paper variant="outlined">
          <TableContainer>
            <Table aria-label="Resource inventory table">
              <TableHead>
                <TableRow>
                  <TableCell><strong>Name</strong></TableCell>
                  <TableCell><strong>Location</strong></TableCell>
                  <TableCell><strong>Description</strong></TableCell>
                  <TableCell><strong>Status</strong></TableCell>
                  <TableCell><strong>Version</strong></TableCell>
                  <TableCell align="right"><strong>Actions</strong></TableCell>
                </TableRow>
              </TableHead>
              <TableBody>
                {data?.items && data.items.length > 0 ? (
                  data.items.map((r) => (
                    <TableRow key={r.id} hover>
                      <TableCell component="th" scope="row">
                        <Typography variant="body2" fontWeight="medium">
                          {r.name}
                        </Typography>
                      </TableCell>
                      <TableCell>{r.location}</TableCell>
                      <TableCell sx={{ maxWidth: 300, overflow: "hidden", textOverflow: "ellipsis" }}>
                        {r.description || <em style={{ color: "#757575" }}>No description</em>}
                      </TableCell>
                      <TableCell>
                        {r.active ? (
                          <Chip
                            label="Active"
                            size="small"
                            color="success"
                            variant="outlined"
                          />
                        ) : (
                          <Chip
                            label="Archived"
                            size="small"
                            color="default"
                            variant="outlined"
                          />
                        )}
                      </TableCell>
                      <TableCell>v{r.version}</TableCell>
                      <TableCell align="right">
                        <Box sx={{ display: "flex", justifyContent: "flex-end", gap: 1 }}>
                          <Button
                            size="small"
                            variant="outlined"
                            component={RouterLink}
                            to={`/admin/resources/${r.id}/blackouts`}
                            aria-label={`Manage blackouts for ${r.name}`}
                          >
                            Blackouts
                          </Button>
                          <Button
                            size="small"
                            variant="outlined"
                            onClick={() => setEditTarget(r)}
                            aria-label={`Edit ${r.name}`}
                          >
                            Edit
                          </Button>
                          <Button
                            size="small"
                            color="warning"
                            variant="outlined"
                            onClick={() => setArchiveTarget(r)}
                            disabled={!r.active}
                            aria-label={`Archive ${r.name}`}
                          >
                            Archive
                          </Button>
                        </Box>
                      </TableCell>
                    </TableRow>
                  ))
                ) : (
                  <TableRow>
                    <TableCell colSpan={6} align="center" sx={{ py: 4 }}>
                      <Typography variant="body2" color="text.secondary">
                        No resources found matching the criteria.
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
            aria-label="Resource table pagination"
          />
        </Paper>
      )}

      {/* Dialogs */}
      <ResourceCreateDialog
        open={createOpen}
        onClose={() => setCreateOpen(false)}
        onCreated={(newRes) => {
          setAnnouncement(`Resource "${newRes.name}" created successfully.`);
          refetch();
        }}
      />

      <ResourceEditDialog
        open={!!editTarget}
        resource={editTarget}
        onClose={() => setEditTarget(null)}
        onUpdated={(updated) => {
          setAnnouncement(`Resource "${updated.name}" updated successfully.`);
          refetch();
        }}
      />

      <ResourceArchiveDialog
        open={!!archiveTarget}
        resource={archiveTarget}
        onClose={() => setArchiveTarget(null)}
        onArchived={() => {
          setAnnouncement(`Resource "${archiveTarget?.name}" archived successfully.`);
          refetch();
        }}
      />
    </Box>
  );
}
