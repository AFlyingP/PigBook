import { Box, Typography, Tabs, Tab, Paper } from "@mui/material";
import { Link as RouterLink, Outlet, useLocation } from "react-router-dom";

export function AdminLayout() {
  const location = useLocation();

  // Determine active tab based on path
  const getActiveTab = () => {
    const path = location.pathname;
    if (path.startsWith("/admin/resources")) return 0;
    if (path.startsWith("/admin/bookings")) return 1;
    if (path.startsWith("/admin/users")) return 2;
    if (path.startsWith("/admin/audit")) return 3;
    if (path.startsWith("/admin/outbox")) return 4;
    if (path.startsWith("/admin/feedback")) return 5;
    return 0;
  };

  return (
    <Box sx={{ width: "100%" }}>
      <Box sx={{ mb: 3 }}>
        <Typography variant="h4" component="h1" gutterBottom fontWeight="bold">
          Administrator Console
        </Typography>
        <Typography variant="body1" color="text.secondary">
          Manage resources, blackouts, reservations, user roles, system audit logs, and outbox queue.
        </Typography>
      </Box>

      <Paper sx={{ mb: 3, maxWidth: "100%" }} elevation={0} variant="outlined">
        <Tabs
          value={getActiveTab()}
          indicatorColor="primary"
          textColor="primary"
          variant="scrollable"
          scrollButtons="auto"
          aria-label="Administrator navigation tabs"
          sx={{ maxWidth: "100%" }}
        >
          <Tab
            label="Resources & Inventory"
            component={RouterLink}
            to="/admin/resources"
            id="admin-tab-resources"
            aria-controls="admin-tabpanel-resources"
          />
          <Tab
            label="Bookings"
            component={RouterLink}
            to="/admin/bookings"
            id="admin-tab-bookings"
            aria-controls="admin-tabpanel-bookings"
          />
          <Tab
            label="Users & Invitations"
            component={RouterLink}
            to="/admin/users"
            id="admin-tab-users"
            aria-controls="admin-tabpanel-users"
          />
          <Tab
            label="Audit Log"
            component={RouterLink}
            to="/admin/audit"
            id="admin-tab-audit"
            aria-controls="admin-tabpanel-audit"
          />
          <Tab
            label="Outbox Queue"
            component={RouterLink}
            to="/admin/outbox"
            id="admin-tab-outbox"
            aria-controls="admin-tabpanel-outbox"
          />
          <Tab
            label="Feedback"
            component={RouterLink}
            to="/admin/feedback"
            id="admin-tab-feedback"
            aria-controls="admin-tabpanel-feedback"
          />
        </Tabs>
      </Paper>

      <Box component="section" role="region" aria-label="Administrator management area">
        <Outlet />
      </Box>
    </Box>
  );
}
