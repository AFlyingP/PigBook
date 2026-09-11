import { useParams, Link as RouterLink } from "react-router-dom";
import { Box, Typography, Breadcrumbs, Link, CircularProgress } from "@mui/material";
import { useQuery } from "@tanstack/react-query";
import { request } from "../../../api/client";
import type { components } from "../../../api/schema";
import { BlackoutTable } from "./BlackoutTable";

type Resource = components["schemas"]["Resource"];

export function AdminBlackoutsPage() {
  const { id } = useParams<{ id: string }>();

  const { data: resource, isLoading } = useQuery<Resource>({
    queryKey: ["admin", "resource", id],
    queryFn: async () => {
      // Try admin list or direct resource fetch
      try {
        const res = await request<Resource>(`/api/v1/resources/${id}`);
        return res.data;
      } catch {
        const adminRes = await request<{ items: Resource[] }>(`/api/v1/admin/resources?limit=100`);
        const found = adminRes.data.items.find((r) => r.id === id);
        if (found) return found;
        throw new Error("Resource not found");
      }
    },
    enabled: !!id,
  });

  if (!id) {
    return <Typography color="error">Resource ID is missing from route.</Typography>;
  }

  return (
    <Box sx={{ py: 1 }}>
      <Box sx={{ mb: 2 }}>
        <Breadcrumbs aria-label="breadcrumb">
          <Link component={RouterLink} to="/admin/resources" color="inherit">
            Resources
          </Link>
          <Typography color="text.primary">
            {resource ? resource.name : "Resource"} Blackouts
          </Typography>
        </Breadcrumbs>
      </Box>

      {isLoading ? (
        <Box sx={{ display: "flex", justifyContent: "center", py: 4 }}>
          <CircularProgress aria-label="Loading resource details..." />
        </Box>
      ) : (
        <BlackoutTable resourceId={id} resourceName={resource?.name || "Resource"} />
      )}
    </Box>
  );
}
