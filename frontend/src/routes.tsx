import { createBrowserRouter, RouteObject } from "react-router-dom";
import { Layout } from "./components/Layout";
import { RequireAuth } from "./components/RequireAuth";
import { LandingPage } from "./components/LandingPage";
import { PrivacyPage } from "./components/PrivacyPage";
import { NotFoundPage } from "./components/NotFoundPage";
import { LoginPage } from "./features/auth/LoginPage";
import { RegisterPage } from "./features/auth/RegisterPage";
import { ResourceList } from "./features/resources/ResourceList";
import { ResourceDetail } from "./features/resources/ResourceDetail";
/* eslint-disable react-refresh/only-export-components */
import { Typography, Paper } from "@mui/material";

function PlaceholderPanel({ title, description }: { title: string; description: string }) {
  return (
    <Paper sx={{ p: 4, my: 4, textAlign: "center", borderRadius: 2 }}>
      <Typography variant="h5" fontWeight="bold" gutterBottom>
        {title}
      </Typography>
      <Typography variant="body2" color="text.secondary">
        {description}
      </Typography>
    </Paper>
  );
}

export const routes: RouteObject[] = [
  {
    path: "/",
    element: <Layout />,
    children: [
      {
        index: true,
        element: <LandingPage />,
      },
      {
        path: "login",
        element: <LoginPage />,
      },
      {
        path: "register",
        element: <RegisterPage />,
      },
      {
        path: "privacy",
        element: <PrivacyPage />,
      },
      // Authenticated routes
      {
        element: <RequireAuth />,
        children: [
          {
            path: "resources",
            element: <ResourceList />,
          },
          {
            path: "resources/:id",
            element: <ResourceDetail />,
          },
          {
            path: "my-bookings",
            element: (
              <PlaceholderPanel
                title="My Bookings"
                description="Personal reservation management interface scheduled for ticket T-030."
              />
            ),
          },
          {
            path: "waitlist",
            element: (
              <PlaceholderPanel
                title="My Waitlist"
                description="Waitlist queue and offer management scheduled for ticket T-031."
              />
            ),
          },
          {
            path: "feedback",
            element: (
              <PlaceholderPanel
                title="Participant Feedback"
                description="Consented feedback survey interface scheduled for future tickets."
              />
            ),
          },
        ],
      },
      // Admin authenticated routes
      {
        element: <RequireAuth adminOnly />,
        children: [
          {
            path: "admin/resources",
            element: (
              <PlaceholderPanel
                title="Admin Inventory Management"
                description="Administrative resource catalog, blackout controls, and archival operations."
              />
            ),
          },
          {
            path: "admin/resources/:id/blackouts",
            element: (
              <PlaceholderPanel
                title="Admin Blackout Management"
                description="Resource blackout creation and cancellation interface."
              />
            ),
          },
          {
            path: "admin/bookings",
            element: (
              <PlaceholderPanel
                title="Admin Reservation Oversight"
                description="Global reservation list, audit review, and administrative cancellation."
              />
            ),
          },
          {
            path: "admin/users",
            element: (
              <PlaceholderPanel
                title="Admin User Management"
                description="User invitation, role assignment, and account enabling/disabling."
              />
            ),
          },
          {
            path: "admin/audit",
            element: (
              <PlaceholderPanel
                title="Admin Audit Log"
                description="Immutable administrative mutation audit trail."
              />
            ),
          },
          {
            path: "admin/outbox",
            element: (
              <PlaceholderPanel
                title="Admin Notification Outbox"
                description="Transactional notification delivery logs and dead-letter retry controls."
              />
            ),
          },
          {
            path: "admin/feedback",
            element: (
              <PlaceholderPanel
                title="Admin Feedback Review"
                description="Aggregated and anonymized participant feedback responses."
              />
            ),
          },
        ],
      },
      // Accessible catch-all
      {
        path: "*",
        element: <NotFoundPage />,
      },
    ],
  },
];

export const router = createBrowserRouter(routes);
