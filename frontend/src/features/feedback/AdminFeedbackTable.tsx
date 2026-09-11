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
  Rating,
  TablePagination,
  Alert,
  CircularProgress,
} from "@mui/material";
import { useQuery } from "@tanstack/react-query";
import { request, ApiError } from "../../api/client";
import type { components } from "../../api/schema";

type PageFeedback = components["schemas"]["Page_Feedback_"];

export function AdminFeedbackTable() {
  const [page, setPage] = useState(0);
  const [rowsPerPage, setRowsPerPage] = useState(25);

  const queryParams = new URLSearchParams({
    limit: String(rowsPerPage),
    offset: String(page * rowsPerPage),
  });

  const { data, isLoading, error } = useQuery<PageFeedback, ApiError>({
    queryKey: ["admin", "feedback", { page, rowsPerPage }],
    queryFn: async () => {
      const res = await request<PageFeedback>(
        `/api/v1/admin/feedback?${queryParams.toString()}`
      );
      return res.data;
    },
  });

  return (
    <Box sx={{ width: "100%" }}>
      <Box sx={{ mb: 3 }}>
        <Typography variant="h5" component="h2" fontWeight="bold">
          Participant Feedback Responses
        </Typography>
        <Typography variant="body2" color="text.secondary">
          Consented user survey submissions. In accordance with Spec 4.1, only authorized fields are
          displayed, with no participant identities beyond pseudonymous user IDs.
        </Typography>
      </Box>

      {error && (
        <Alert severity="error" sx={{ mb: 2 }} role="alert">
          {error.message || "Failed to load feedback responses."}
        </Alert>
      )}

      {isLoading ? (
        <Box sx={{ display: "flex", justifyContent: "center", py: 8 }}>
          <CircularProgress aria-label="Loading feedback responses..." />
        </Box>
      ) : (
        <Paper variant="outlined">
          <TableContainer>
            <Table aria-label="Participant feedback survey responses">
              <TableHead>
                <TableRow>
                  <TableCell><strong>Submitted (UTC)</strong></TableCell>
                  <TableCell><strong>User ID</strong></TableCell>
                  <TableCell><strong>Rating</strong></TableCell>
                  <TableCell><strong>Task Done?</strong></TableCell>
                  <TableCell><strong>Difficulty Reported</strong></TableCell>
                  <TableCell><strong>Improvement Suggestion</strong></TableCell>
                  <TableCell><strong>Consent</strong></TableCell>
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
                        <Typography variant="caption" fontFamily="monospace">
                          {item.user_id}
                        </Typography>
                      </TableCell>
                      <TableCell>
                        <Box sx={{ display: "flex", alignItems: "center", gap: 1 }}>
                          <Rating value={item.rating} readOnly size="small" />
                          <Typography variant="caption">({item.rating}/5)</Typography>
                        </Box>
                      </TableCell>
                      <TableCell>
                        {item.task_completed ? (
                          <Chip label="Yes" size="small" color="success" variant="outlined" />
                        ) : (
                          <Chip label="No" size="small" color="warning" variant="outlined" />
                        )}
                      </TableCell>
                      <TableCell sx={{ maxWidth: 250 }}>
                        {/* Always rendered as plain text, never markup */}
                        {item.difficulty ? (
                          <Typography variant="body2" sx={{ wordBreak: "break-word" }}>
                            {item.difficulty}
                          </Typography>
                        ) : (
                          <em style={{ color: "#757575" }}>None noted</em>
                        )}
                      </TableCell>
                      <TableCell sx={{ maxWidth: 250 }}>
                        {/* Always rendered as plain text, never markup */}
                        {item.improvement ? (
                          <Typography variant="body2" sx={{ wordBreak: "break-word" }}>
                            {item.improvement}
                          </Typography>
                        ) : (
                          <em style={{ color: "#757575" }}>None noted</em>
                        )}
                      </TableCell>
                      <TableCell>
                        <Typography variant="caption" color="text.secondary">
                          {item.consent_version}
                        </Typography>
                      </TableCell>
                    </TableRow>
                  ))
                ) : (
                  <TableRow>
                    <TableCell colSpan={7} align="center" sx={{ py: 4 }}>
                      <Typography variant="body2" color="text.secondary">
                        No feedback submissions found.
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
            aria-label="Feedback table pagination"
          />
        </Paper>
      )}
    </Box>
  );
}
