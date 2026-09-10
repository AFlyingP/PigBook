import { useState, useMemo } from "react";
import { useQuery } from "@tanstack/react-query";
import {
  Box,
  Typography,
  TextField,
  Card,
  CardContent,
  CardActions,
  Button,
  Grid,
  Chip,
  Pagination,
  Skeleton,
  Alert,
  InputAdornment,
  IconButton,
} from "@mui/material";
import { Link as RouterLink } from "react-router-dom";
import { request } from "../../api/client";
import type { components } from "../../api/schema";

type Resource = components["schemas"]["Resource"];
type PageResource = components["schemas"]["Page_Resource_"];

const PAGE_SIZE = 12;

export function ResourceList() {
  const [page, setPage] = useState(1);
  const [searchQuery, setSearchQuery] = useState("");

  const offset = (page - 1) * PAGE_SIZE;

  const { data, isLoading, isError, error, refetch } = useQuery<PageResource>({
    queryKey: ["resources", { limit: PAGE_SIZE, offset }],
    queryFn: async () => {
      const res = await request<PageResource>(
        `/api/v1/resources?limit=${PAGE_SIZE}&offset=${offset}`
      );
      return res.data;
    },
  });

  // Client-side search within the fetched page (Spec 7.1)
  const filteredItems = useMemo(() => {
    if (!data?.items) return [];
    if (!searchQuery.trim()) return data.items;
    const q = searchQuery.toLowerCase().trim();
    return data.items.filter(
      (r: Resource) =>
        r.name.toLowerCase().includes(q) ||
        r.location.toLowerCase().includes(q) ||
        (r.description && r.description.toLowerCase().includes(q))
    );
  }, [data?.items, searchQuery]);

  const totalPages = data ? Math.ceil(data.total / PAGE_SIZE) : 1;

  return (
    <Box sx={{ py: 3 }}>
      <Box
        sx={{
          display: "flex",
          justifyContent: "space-between",
          alignItems: "flex-start",
          flexWrap: "wrap",
          gap: 2,
          mb: 4,
        }}
      >
        <Box>
          <Typography component="h1" variant="h4" fontWeight="bold" gutterBottom>
            Resource Catalog
          </Typography>
          <Typography variant="body1" color="text.secondary">
            Browse and check availability for shared community spaces and equipment.
          </Typography>
        </Box>

        <TextField
          id="resource-search"
          label="Search page resources"
          placeholder="Filter by name, location..."
          size="small"
          value={searchQuery}
          onChange={(e) => setSearchQuery(e.target.value)}
          sx={{ minWidth: 260 }}
          InputProps={{
            endAdornment: searchQuery ? (
              <InputAdornment position="end">
                <IconButton
                  aria-label="Clear search query"
                  size="small"
                  onClick={() => setSearchQuery("")}
                >
                  ✕
                </IconButton>
              </InputAdornment>
            ) : null,
          }}
        />
      </Box>

      {/* Screen-reader announcement of search filter count */}
      <Box
        aria-live="polite"
        role="status"
        sx={{
          position: "absolute",
          width: "1px",
          height: "1px",
          padding: 0,
          margin: "-1px",
          overflow: "hidden",
          clip: "rect(0, 0, 0, 0)",
          border: 0,
        }}
      >
        {searchQuery && !isLoading && `Showing ${filteredItems.length} filtered resources`}
      </Box>

      {isError && (
        <Alert
          severity="error"
          role="alert"
          sx={{ mb: 4 }}
          action={
            <Button color="inherit" size="small" onClick={() => refetch()}>
              Retry
            </Button>
          }
        >
          {error instanceof Error
            ? error.message
            : "Failed to load resource catalog. Please try again."}
        </Alert>
      )}

      {isLoading ? (
        <Grid container spacing={3}>
          {Array.from({ length: 6 }).map((_, i) => (
            <Grid item xs={12} sm={6} md={4} key={i}>
              <Card sx={{ height: "100%", p: 2 }}>
                <Skeleton variant="text" width="60%" height={32} />
                <Skeleton variant="text" width="40%" height={20} sx={{ mb: 2 }} />
                <Skeleton variant="rectangular" height={60} sx={{ mb: 2 }} />
                <Skeleton variant="rectangular" height={36} width={100} />
              </Card>
            </Grid>
          ))}
        </Grid>
      ) : filteredItems.length === 0 ? (
        <Box sx={{ textAlign: "center", py: 8 }}>
          <Typography variant="h6" color="text.secondary" gutterBottom>
            {searchQuery
              ? "No resources matched your search query on this page."
              : "No resources available at this time."}
          </Typography>
          {searchQuery && (
            <Button variant="outlined" onClick={() => setSearchQuery("")} sx={{ mt: 1 }}>
              Reset Search
            </Button>
          )}
        </Box>
      ) : (
        <>
          <Grid container spacing={3}>
            {filteredItems.map((resource: Resource) => (
              <Grid item xs={12} sm={6} md={4} key={resource.id}>
                <Card
                  variant="outlined"
                  sx={{
                    height: "100%",
                    display: "flex",
                    flexDirection: "column",
                    borderRadius: 2,
                    transition: "box-shadow 0.2s",
                    "&:hover": {
                      boxShadow: 2,
                    },
                  }}
                >
                  <CardContent sx={{ flexGrow: 1 }}>
                    <Box
                      sx={{
                        display: "flex",
                        justifyContent: "space-between",
                        alignItems: "flex-start",
                        gap: 1,
                        mb: 1,
                      }}
                    >
                      <Typography variant="h6" component="h2" fontWeight="bold">
                        {resource.name}
                      </Typography>
                      <Chip
                        label={resource.active ? "Active" : "Inactive"}
                        color={resource.active ? "success" : "default"}
                        size="small"
                        variant={resource.active ? "filled" : "outlined"}
                      />
                    </Box>

                    <Typography
                      variant="body2"
                      color="primary.main"
                      fontWeight="medium"
                      gutterBottom
                    >
                      📍 {resource.location}
                    </Typography>

                    <Typography
                      variant="body2"
                      color="text.secondary"
                      sx={{
                        display: "-webkit-box",
                        WebkitLineClamp: 3,
                        WebkitBoxOrient: "vertical",
                        overflow: "hidden",
                        textOverflow: "ellipsis",
                        minHeight: 40,
                      }}
                    >
                      {resource.description || "No description provided."}
                    </Typography>
                  </CardContent>

                  <CardActions sx={{ p: 2, pt: 0 }}>
                    <Button
                      component={RouterLink}
                      to={`/resources/${resource.id}`}
                      variant="contained"
                      size="small"
                      fullWidth
                    >
                      View Availability & Details
                    </Button>
                  </CardActions>
                </Card>
              </Grid>
            ))}
          </Grid>

          {data && data.total > PAGE_SIZE && (
            <Box sx={{ display: "flex", justifyContent: "center", mt: 5 }}>
              <Pagination
                count={totalPages}
                page={page}
                onChange={(_, p) => setPage(p)}
                color="primary"
                showFirstButton
                showLastButton
                aria-label="Resource catalog pagination"
              />
            </Box>
          )}
        </>
      )}
    </Box>
  );
}
